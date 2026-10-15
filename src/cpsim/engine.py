"""A running city: sensors -> field devices (protocol servers) -> edge gateway (protocol clients) -> CityPulse.

Per protocol the city gets one server hosting its devices of up to 32 channels each. A scheduler updates device images as
sensors come due; the gateway polls every device with the protocol's own client (PROFINET devices push cyclic frames
instead) and turns every channel whose update counter moved into a reading. Sensors in `direct` groups skip the devices
and go straight to the sender. The sender keeps a bounded retry buffer and backs off when CityPulse pushes back.
"""
import asyncio
import math
import random
import sys
import time
from collections import Counter, deque

from . import __version__
from .generator import SensorSim
from .model import City
from .protocols import PROTOCOLS, enip, ethercat, modbus, profibus, profinet
from .protocols.image import CHANNEL_BYTES, MAX_CHANNELS, Device, unpack
from .sinks import Fatal, NullSink, Retry

TICK, BATCH, MAX_SENDS_PER_TICK, BUFFER_MAX, MAX_POLLS_IN_FLIGHT = 0.25, 100, 8, 100_000, 32
BIND_LOOPBACK = "127.0.0.1"


class Channel:
    __slots__ = ("sensor", "seq", "seen")

    def __init__(self, sensor: SensorSim):
        self.sensor, self.seq, self.seen = sensor, 0, 0


class CityRunner:
    def __init__(self, city: City, sink=None, rng_now=time.time):
        self.city, self.sink, self.now = city, sink or NullSink(), rng_now
        t0 = self.now()
        self.sensors: list[SensorSim] = []
        for g in city.groups:
            lo, hi = g.span()
            for _ in range(g.count):
                sid = f"{g.type.replace('_', '-')}-{g.district}-{city.name}-{len(self.sensors):05d}"
                if len(sid) > 64:   # long names: shorten the descriptive parts, never the unique index
                    sid = f"{g.type.replace('_', '-')[:12]}-{g.district[:6]}-{city.name[:20]}-{len(self.sensors):05d}"
                self.sensors.append(SensorSim(sid, g.type, g.district, g.protocol, lo, hi, g.frequency, g.burst_pct, city.seed, t0))
        self.devices: dict[str, dict[int, Device]] = {p: {} for p in PROTOCOLS}
        self.channels: dict[str, dict[int, list[Channel]]] = {p: {} for p in PROTOCOLS}
        self.channel_of: dict[str, tuple[Channel, str, int]] = {}      # sensor id -> channel, protocol, device address
        for proto in PROTOCOLS:
            members = [s for s in self.sensors if s.protocol == proto]
            for n in range(0, len(members), MAX_CHANNELS):
                address = n // MAX_CHANNELS + 1
                chunk = members[n:n + MAX_CHANNELS]
                self.devices[proto][address] = Device(proto, address, len(chunk))
                self.channels[proto][address] = [Channel(s) for s in chunk]
                for ch in self.channels[proto][address]:
                    self.channel_of[ch.sensor.id] = (ch, proto, address)
        self.endpoints: dict[str, dict] = {}
        self.servers: list = []
        self.tasks: list[asyncio.Task] = []
        self.buffer: deque[dict] = deque()
        self.stats = Counter()
        self.poll_ok, self.poll_fail = Counter(), Counter()
        self.device_fail: Counter = Counter()
        self.last_error = ""
        self.backoff, self.resume_at, self.fatal = 0.0, 0.0, ""
        self.boost_until, self.boost_factor = 0.0, 1.0
        self.started_at = 0.0
        self._rate: deque[tuple[float, int]] = deque(maxlen=60)
        self._pn_receiver = None
        self._pn_images: dict[int, bytes] = {}

    # ---- lifecycle ----------------------------------------------------------------------------
    async def start(self) -> None:
        host = "0.0.0.0" if self.city.expose else BIND_LOOPBACK  # noqa: S104 - opt-in per city
        connect = BIND_LOOPBACK
        for proto, server_cls in (("modbus_tcp", modbus.ModbusServer), ("ethernet_ip", enip.EnipServer),
                                  ("ethercat", ethercat.EtherCatServer), ("profibus", profibus.ProfibusServer)):
            if self.devices[proto]:
                server = server_cls(self.devices[proto])
                port = await server.start(host, 0)
                self.servers.append(server)
                self.endpoints[proto] = {"host": host if self.city.expose else connect, "port": port, "devices": len(self.devices[proto])}
        if self.devices["profinet"]:
            sizes = {a: d.channels * CHANNEL_BYTES for a, d in self.devices["profinet"].items()}
            self._pn_receiver, pn_port = await profinet.ProfinetReceiver.listen(connect, sizes, self._on_pn_image)
            sender = profinet.ProfinetSender(self.devices["profinet"], (connect, pn_port), cycle=max(0.5, self.city.poll_interval))
            await sender.start()
            self.servers.append(sender)
            self.endpoints["profinet"] = {"host": connect, "port": pn_port, "devices": len(self.devices["profinet"]),
                                          "note": "cyclic frames are pushed to the gateway's UDP port"}
        self.started_at = self.now()
        self.tasks = [asyncio.create_task(self._guard(self._scheduler())), asyncio.create_task(self._guard(self._gateway())),
                      asyncio.create_task(self._guard(self._sender()))]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for s in self.servers:
            await s.stop()
        if self._pn_receiver:
            self._pn_receiver.close()
        await self.sink.close()
        self.tasks, self.servers = [], []

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failing loop must not take the city down silently
            self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            print(f"cpsim[{self.city.name}]: loop crashed: {self.last_error}", file=sys.stderr, flush=True)

    def boost(self, seconds: float, factor: float = 25.0) -> None:
        self.boost_until, self.boost_factor = self.now() + seconds, factor

    # ---- sensors -> devices -------------------------------------------------------------------
    def update_sensors(self, now: float) -> int:
        boost = self.boost_factor if now < self.boost_until else 1.0
        n = 0
        for s in self.sensors:
            if s.due > now:
                continue
            tick = s.step(now, boost)
            s.due = now + s.interval()
            if tick.value is None:
                self.stats["silenced"] += 1
                continue
            n += 1
            self.stats["generated"] += 1
            if s.protocol == "direct":
                self._emit(s, tick.value)
            else:
                ch, proto, address = self.channel_of[s.id]
                ch.seq = (ch.seq + 1) & 0xFFFF or 1
                self.devices[proto][address].set(self.channels[proto][address].index(ch), tick.value, ch.seq)
        return n

    async def _scheduler(self) -> None:
        while True:
            self.update_sensors(self.now())
            await asyncio.sleep(TICK)

    # ---- devices -> gateway -------------------------------------------------------------------
    def _emit(self, s: SensorSim, value: float) -> None:
        bad = not math.isfinite(value)
        if bad:
            self.stats["bad_values"] += 1
        self.stats["readings"] += 1
        self.buffer.append({"sensor": s.id, "type": s.type, "district": s.district, "protocol": s.protocol,
                            "value": None if bad else round(value, 4), "ts": round(self.now(), 3)})
        if len(self.buffer) > BUFFER_MAX:
            self.buffer.popleft()
            self.stats["dropped"] += 1

    def process_image(self, proto: str, address: int, data: bytes) -> None:
        channels = self.channels[proto][address]
        for ch, (value, seq) in zip(channels, unpack(data, len(channels)), strict=True):
            if seq != 0 and seq != ch.seen:
                ch.seen = seq
                self._emit(ch.sensor, value)

    def _on_pn_image(self, address: int, data: bytes) -> None:
        if address in self.channels["profinet"]:
            self.poll_ok["profinet"] += 1
            self.process_image("profinet", address, data)

    async def _read(self, proto: str, address: int, device: Device) -> bytes:
        ep, size = self.endpoints[proto], device.channels * CHANNEL_BYTES
        if proto == "modbus_tcp":
            return await modbus.read_input_registers(ep["host"], ep["port"], address, size // 2)
        if proto == "ethernet_ip":
            return (await enip.read_assembly(ep["host"], ep["port"], enip.INSTANCE_BASE + address))[:size]
        if proto == "ethercat":
            return await ethercat.read_logical(ep["host"], ep["port"], address, size)
        return (await profibus.data_exchange(ep["host"], ep["port"], address))[:size]

    async def _poll(self, proto: str, address: int, device: Device, gate: asyncio.Semaphore) -> None:
        async with gate:
            try:
                data = await self._read(proto, address, device)
                self.process_image(proto, address, data)
                self.poll_ok[proto] += 1
                self.device_fail[(proto, address)] = 0
            except (modbus.ModbusError, enip.EnipError, ethercat.EtherCatError, profibus.ProfibusError, ValueError) as exc:
                self.poll_fail[proto] += 1
                self.device_fail[(proto, address)] += 1
                self.last_error = f"{proto} device {address}: {exc}"

    async def _gateway(self) -> None:
        gate = asyncio.Semaphore(MAX_POLLS_IN_FLIGHT)
        while True:
            started = time.monotonic()
            jobs = [self._poll(p, a, d, gate) for p in ("modbus_tcp", "ethernet_ip", "ethercat", "profibus")
                    for a, d in self.devices[p].items()]
            if jobs:
                await asyncio.gather(*jobs)
            await asyncio.sleep(max(0.05, self.city.poll_interval - (time.monotonic() - started)))

    # ---- sender -------------------------------------------------------------------------------
    async def deliver(self, now: float) -> None:
        if now < self.resume_at:
            return
        chunks = []
        while self.buffer and len(chunks) < MAX_SENDS_PER_TICK:
            chunks.append([self.buffer.popleft() for _ in range(min(BATCH, len(self.buffer)))])
        if not chunks:
            return
        results = await asyncio.gather(*(self.sink.send(c) for c in chunks), return_exceptions=True)
        failed, wait = [], 0.0
        for chunk, res in zip(chunks, results, strict=True):
            if res is None:
                self.stats["sent"] += len(chunk)
            elif isinstance(res, Retry):
                failed.append(chunk)
                wait = max(wait, res.after)
                self.stats["send_failures"] += 1
            elif isinstance(res, Fatal):
                failed.append(chunk)
                self.fatal, wait = str(res), max(wait, 30.0)
                self.stats["send_failures"] += 1
            else:
                failed.append(chunk)
                self.last_error = f"send: {res!r}"[:160]
                self.stats["send_failures"] += 1
        for chunk in reversed(failed):
            self.buffer.extendleft(reversed(chunk))
        if failed:
            self.backoff = min(20.0, max(self.backoff * 2, 1.0))
            self.resume_at = now + max(self.backoff, wait) * random.uniform(0.8, 1.2)
        else:
            self.backoff, self.fatal = 0.0, ""

    async def _sender(self) -> None:
        while True:
            await self.deliver(self.now())
            self._rate.append((time.monotonic(), self.stats["sent"]))
            await asyncio.sleep(1.0)

    # ---- reporting ----------------------------------------------------------------------------
    def snapshot(self) -> dict:
        first, last = (self._rate[0], self._rate[-1]) if len(self._rate) > 1 else ((0, 0), (1, 0))
        rate = (last[1] - first[1]) / max(last[0] - first[0], 1e-9) if len(self._rate) > 1 else 0.0
        episodes = Counter(s.episode for s in self.sensors if s.episode)
        devices = {p: len(d) for p, d in self.devices.items() if d}
        down = sum(1 for k, v in self.device_fail.items() if v >= 3)
        return {"name": self.city.name, "version": __version__, "sensors": len(self.sensors), "devices": devices,
                "endpoints": self.endpoints, "uptime": round(self.now() - self.started_at) if self.started_at else 0,
                "generated": self.stats["generated"], "readings": self.stats["readings"], "sent": self.stats["sent"],
                "dropped": self.stats["dropped"], "bad_values": self.stats["bad_values"], "silenced": self.stats["silenced"],
                "send_failures": self.stats["send_failures"], "buffer": len(self.buffer), "backoff": round(self.backoff, 1),
                "send_rate": round(rate, 1), "polls_ok": dict(self.poll_ok), "polls_failed": dict(self.poll_fail),
                "devices_unreachable": down, "bursts": dict(episodes), "boost": max(0, round(self.boost_until - self.now())),
                "problem": self.fatal or self.last_error}
