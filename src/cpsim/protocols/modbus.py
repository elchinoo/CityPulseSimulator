"""Modbus TCP: MBAP header + function codes 0x03/0x04 (read holding / input registers).

One TCP port hosts many devices, selected by the unit identifier, the way a Modbus gateway does.
"""
import asyncio
import struct

from .image import Device

EXC_FUNCTION, EXC_ADDRESS, EXC_VALUE, EXC_GATEWAY_TARGET = 0x01, 0x02, 0x03, 0x0B


class ModbusError(Exception):
    pass


def request(tid: int, unit: int, fc: int, address: int, count: int) -> bytes:
    pdu = struct.pack(">BHH", fc, address, count)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def respond(devices: dict[int, Device], frame: bytes) -> bytes | None:
    """The reply for one request frame (MBAP + PDU), or None when the device does not answer."""
    tid, pid, _length, unit = struct.unpack(">HHHB", frame[:7])
    pdu = frame[7:]
    if pid != 0 or not pdu:
        return None

    def reply(body: bytes) -> bytes:
        return struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body

    fc = pdu[0]
    device = devices.get(unit)
    if device is None:
        return reply(bytes([fc | 0x80, EXC_GATEWAY_TARGET]))
    if not device.online:
        return None                                    # a dead link: the master times out
    if fc not in (0x03, 0x04):
        return reply(bytes([fc | 0x80, EXC_FUNCTION]))
    if len(pdu) != 5:
        return reply(bytes([fc | 0x80, EXC_VALUE]))
    address, count = struct.unpack(">HH", pdu[1:])
    words = len(device.image) // 2
    if not 1 <= count <= 125:
        return reply(bytes([fc | 0x80, EXC_VALUE]))
    if address + count > words:
        return reply(bytes([fc | 0x80, EXC_ADDRESS]))
    data = bytes(device.image[address * 2:(address + count) * 2])
    return reply(bytes([fc, len(data)]) + data)


class ModbusServer:
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
        try:
            while True:
                head = await reader.readexactly(7)
                length = struct.unpack(">H", head[4:6])[0]
                if not 2 <= length <= 254:
                    break                                  # malformed: drop the connection
                frame = head + await reader.readexactly(length - 1)
                out = respond(self.devices, frame)
                if out:
                    writer.write(out)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def read_input_registers(host: str, port: int, unit: int, count: int, timeout: float = 1.5, address: int = 0) -> bytes:
    async def go() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(request(1, unit, 0x04, address, count))
            await writer.drain()
            head = await reader.readexactly(7)
            length = struct.unpack(">H", head[4:6])[0]
            body = await reader.readexactly(length - 1)
            if head[6] != unit or head[0:2] != b"\x00\x01":
                raise ModbusError("response does not match the request")
            if body[0] & 0x80:
                raise ModbusError(f"modbus exception {body[1]:#04x}")
            if body[0] != 0x04 or body[1] != count * 2 or len(body) != 2 + count * 2:
                raise ModbusError("malformed response")
            return body[2:]
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(go(), timeout)
    except (TimeoutError, asyncio.IncompleteReadError, OSError) as exc:
        raise ModbusError(f"no answer from unit {unit}: {type(exc).__name__}") from exc
