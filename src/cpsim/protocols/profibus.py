"""PROFIBUS DP telegrams (SD1 request, SD2 response, short acknowledgement) over a TCP stream.

A real bus is RS-485 at up to 12 Mbit/s; this project carries the same telegrams over TCP, as a serial-to-Ethernet bridge
would. The master (the gateway) polls slave addresses 0..125 with a Data_Exchange request; a slave answers with its input
data. Function codes are simplified: request 0x4D (SRD, high priority), response 0x08. Not conformance-tested.
"""
import asyncio

from .image import Device

SD1, SD2, SC, ED = 0x10, 0x68, 0xE5, 0x16
FC_REQUEST, FC_RESPONSE = 0x4D, 0x08
MASTER = 0x02
MAX_DU = 244


class ProfibusError(Exception):
    pass


def fcs(data: bytes) -> int:
    return sum(data) & 0xFF


def sd1(da: int, sa: int, fc: int) -> bytes:
    body = bytes([da, sa, fc])
    return bytes([SD1]) + body + bytes([fcs(body), ED])


def sd2(da: int, sa: int, fc: int, du: bytes) -> bytes:
    if len(du) > MAX_DU:
        raise ProfibusError("data unit too long")
    body = bytes([da, sa, fc]) + du
    return bytes([SD2, len(body), len(body), SD2]) + body + bytes([fcs(body), ED])


def parse(buf: bytes) -> tuple[tuple[str, int, int, int, bytes] | None, int]:
    """One telegram from the front of `buf`: ((kind, da, sa, fc, du), bytes consumed), or (None, 0) if incomplete.
    Raises ProfibusError for a corrupt telegram."""
    if not buf:
        return None, 0
    start = buf[0]
    if start == SC:
        return ("SC", 0, 0, 0, b""), 1
    if start == SD1:
        if len(buf) < 6:
            return None, 0
        da, sa, fc, check, end = buf[1:6]
        if end != ED or check != fcs(buf[1:4]):
            raise ProfibusError("bad SD1 telegram")
        return ("SD1", da, sa, fc, b""), 6
    if start == SD2:
        if len(buf) < 4:
            return None, 0
        if buf[1] != buf[2] or buf[3] != SD2 or buf[1] < 3:
            raise ProfibusError("bad SD2 header")
        total = 4 + buf[1] + 2
        if len(buf) < total:
            return None, 0
        body = buf[4:4 + buf[1]]
        if buf[total - 2] != fcs(body) or buf[total - 1] != ED:
            raise ProfibusError("bad SD2 checksum or end delimiter")
        return ("SD2", body[0], body[1], body[2], body[3:]), total
    raise ProfibusError(f"unknown start delimiter {start:#04x}")


class ProfibusServer:
    def __init__(self, devices: dict[int, Device]):
        self.devices, self.server = devices, None

    async def start(self, host: str, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = b""
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                buf += chunk
                while buf:
                    try:
                        telegram, used = parse(buf)
                    except ProfibusError:
                        buf = b""                      # resynchronise: a real slave ignores line noise
                        break
                    if telegram is None:
                        break
                    buf = buf[used:]
                    kind, da, sa, fc, _du = telegram
                    device = self.devices.get(da)
                    if kind == "SD1" and fc == FC_REQUEST and device and device.online:
                        writer.write(sd2(sa, da, FC_RESPONSE, device.snapshot()))
                        await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


async def data_exchange(host: str, port: int, slave: int, timeout: float = 1.5) -> bytes:
    async def go() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(sd1(slave, MASTER, FC_REQUEST))
            await writer.drain()
            buf = b""
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    raise ProfibusError("connection closed before the slave answered")
                buf += chunk
                telegram, used = parse(buf)
                if telegram is None:
                    continue
                kind, da, sa, fc, du = telegram
                if kind != "SD2" or da != MASTER or sa != slave or fc != FC_RESPONSE:
                    raise ProfibusError("unexpected telegram from the slave")
                return du
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(go(), timeout)
    except (TimeoutError, OSError) as exc:
        raise ProfibusError(f"no answer from slave {slave}: {type(exc).__name__}") from exc
