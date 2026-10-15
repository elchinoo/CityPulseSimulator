"""EtherCAT over UDP (port 0x88A4 encapsulation): a frame of datagrams with the logical read command (LRD).

Device `n` owns logical addresses [n*0x100, n*0x100 + image size). A datagram that lies wholly inside one device's range
is answered with that device's image and its working counter raised by one; anything else comes back with counter 0,
which is how a real master sees "nobody handled this".
"""
import asyncio
import struct

from .image import Device

PORT = 0x88A4
CMD_NOP, CMD_BRD, CMD_LRD, CMD_LWR, CMD_LRW = 0x00, 0x07, 0x0A, 0x0B, 0x0C
STRIDE = 0x100


class EtherCatError(Exception):
    pass


def logical_address(address: int) -> int:
    return address * STRIDE


def build(datagrams: list[tuple[int, int, int, bytes]]) -> bytes:
    """datagrams: (command, index, logical address, data). Returns the UDP payload."""
    body = b""
    for n, (cmd, idx, addr, data) in enumerate(datagrams):
        more = 0x8000 if n < len(datagrams) - 1 else 0
        body += struct.pack("<BBIHH", cmd, idx, addr, len(data) | more, 0) + data + b"\x00\x00"
    return struct.pack("<H", len(body) | 0x1000) + body


def parse(frame: bytes) -> list[tuple[int, int, int, bytes, int]]:
    """Returns (command, index, address, data, working counter) per datagram."""
    if len(frame) < 2:
        raise EtherCatError("frame too short")
    head = struct.unpack("<H", frame[:2])[0]
    length, kind = head & 0x7FF, head >> 12
    if kind != 1 or len(frame) < 2 + length:
        raise EtherCatError("not an EtherCAT command frame")
    out, i, end = [], 2, 2 + length
    while i < end:
        if i + 10 > end:
            raise EtherCatError("truncated datagram header")
        cmd, idx, addr, lenflags, _irq = struct.unpack("<BBIHH", frame[i:i + 10])
        size = lenflags & 0x7FF
        if i + 10 + size + 2 > end:
            raise EtherCatError("truncated datagram")
        data = frame[i + 10:i + 10 + size]
        wkc = struct.unpack("<H", frame[i + 10 + size:i + 12 + size])[0]
        out.append((cmd, idx, addr, data, wkc))
        i += 12 + size
        if not lenflags & 0x8000:
            break
    return out


def respond(devices: dict[int, Device], frame: bytes) -> bytes | None:
    try:
        datagrams = parse(frame)
    except EtherCatError:
        return None
    answers = []
    for cmd, idx, addr, data, wkc in datagrams:
        out = bytes(len(data))
        if cmd == CMD_LRD:
            device = devices.get(addr // STRIDE)
            offset = addr % STRIDE
            if device and device.online and offset + len(data) <= len(device.image):
                out, wkc = bytes(device.image[offset:offset + len(data)]), wkc + 1
        answers.append((cmd, idx, addr, out if cmd == CMD_LRD else data, wkc))
    reply = build([(c, i, a, d) for c, i, a, d, _ in answers])
    # build() writes zero counters: patch in the real ones
    buf, pos = bytearray(reply), 2
    for _c, _i, _a, d, wkc in answers:
        pos += 10 + len(d)
        struct.pack_into("<H", buf, pos, wkc)
        pos += 2
    return bytes(buf)


class _Server(asyncio.DatagramProtocol):
    def __init__(self, devices):
        self.devices, self.transport = devices, None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        out = respond(self.devices, data)
        if out and self.transport:
            self.transport.sendto(out, addr)


class EtherCatServer:
    def __init__(self, devices: dict[int, Device]):
        self.devices, self.transport = devices, None

    async def start(self, host: str, port: int = 0) -> int:
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Server(self.devices), local_addr=(host, port))
        return self.transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self.transport:
            self.transport.close()


class _Client(asyncio.DatagramProtocol):
    def __init__(self, future):
        self.future = future

    def datagram_received(self, data, addr):
        if not self.future.done():
            self.future.set_result(data)

    def error_received(self, exc):
        if not self.future.done():
            self.future.set_exception(exc)


async def read_logical(host: str, port: int, address: int, size: int, timeout: float = 1.5) -> bytes:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Client(future), remote_addr=(host, port))
    try:
        transport.sendto(build([(CMD_LRD, 1, logical_address(address), bytes(size))]))
        reply = await asyncio.wait_for(future, timeout)
        (_cmd, idx, _addr, data, wkc), = parse(reply)[:1]
        if idx != 1 or wkc < 1:
            raise EtherCatError("no slave handled the datagram (working counter 0)")
        return data
    except (TimeoutError, OSError) as exc:
        raise EtherCatError(f"no answer from slave {address}: {type(exc).__name__}") from exc
    finally:
        transport.close()
