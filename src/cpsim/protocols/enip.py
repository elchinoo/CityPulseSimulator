"""EtherNet/IP explicit messaging over TCP 44818: encapsulation + CIP Get_Attribute_Single on an Assembly object.

Device `n` is Assembly instance 100+n; attribute 3 is its data (the process image).
"""
import asyncio
import struct

from .image import Device

REGISTER_SESSION, UNREGISTER_SESSION, SEND_RR_DATA = 0x0065, 0x0066, 0x006F
CIP_GET_ATTRIBUTE_SINGLE, CLASS_ASSEMBLY, ATTR_DATA = 0x0E, 0x04, 0x03
INSTANCE_BASE = 100
ST_OK, ST_PATH_UNKNOWN, ST_SERVICE_UNSUPPORTED, ST_ATTR_UNSUPPORTED = 0x00, 0x05, 0x08, 0x14


class EnipError(Exception):
    pass


def encapsulate(command: int, session: int, data: bytes = b"", status: int = 0, context: bytes = b"\x00" * 8) -> bytes:
    return struct.pack("<HHII8sI", command, len(data), session, status, context, 0) + data


def _segments(instance: int) -> bytes:
    inst = bytes([0x24, instance]) if instance < 256 else b"\x25\x00" + struct.pack("<H", instance)
    return bytes([0x20, CLASS_ASSEMBLY]) + inst + bytes([0x30, ATTR_DATA])


def cip_get(instance: int) -> bytes:
    path = _segments(instance)
    return bytes([CIP_GET_ATTRIBUTE_SINGLE, len(path) // 2]) + path


def rr_data(cip: bytes) -> bytes:
    items = struct.pack("<HH", 0x0000, 0) + struct.pack("<HH", 0x00B2, len(cip)) + cip
    return struct.pack("<IHH", 0, 10, 2) + items


def _parse_path(path: bytes) -> tuple[int | None, int | None, int | None]:
    cls = inst = attr = None
    i = 0
    while i < len(path):
        seg = path[i]
        if seg == 0x20 and i + 1 < len(path):
            cls, i = path[i + 1], i + 2
        elif seg == 0x24 and i + 1 < len(path):
            inst, i = path[i + 1], i + 2
        elif seg == 0x25 and i + 3 < len(path):
            inst, i = struct.unpack("<H", path[i + 2:i + 4])[0], i + 4
        elif seg == 0x30 and i + 1 < len(path):
            attr, i = path[i + 1], i + 2
        else:
            return None, None, None
    return cls, inst, attr


def handle_cip(devices: dict[int, Device], cip: bytes) -> bytes | None:
    """The CIP reply, or None if the device is unreachable."""
    def fail(service: int, status: int) -> bytes:
        return bytes([service | 0x80, 0, status, 0])

    if len(cip) < 2:
        return fail(0, ST_SERVICE_UNSUPPORTED)
    service, words = cip[0], cip[1]
    if service != CIP_GET_ATTRIBUTE_SINGLE:
        return fail(service, ST_SERVICE_UNSUPPORTED)
    cls, inst, attr = _parse_path(cip[2:2 + words * 2])
    device = devices.get((inst or 0) - INSTANCE_BASE)
    if cls != CLASS_ASSEMBLY or device is None:
        return fail(service, ST_PATH_UNKNOWN)
    if not device.online:
        return None
    if attr != ATTR_DATA:
        return fail(service, ST_ATTR_UNSUPPORTED)
    return bytes([service | 0x80, 0, ST_OK, 0]) + device.snapshot()


class EnipServer:
    def __init__(self, devices: dict[int, Device]):
        self.devices, self.server, self._next_session = devices, None, 1

    async def start(self, host: str, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = 0
        try:
            while True:
                head = await reader.readexactly(24)
                command, length, sess, _status, context, _opts = struct.unpack("<HHII8sI", head)
                if length > 1024:
                    break
                data = await reader.readexactly(length) if length else b""
                if command == REGISTER_SESSION:
                    session, self._next_session = self._next_session, self._next_session + 1
                    writer.write(encapsulate(command, session, data[:4], context=context))
                elif command == UNREGISTER_SESSION:
                    break
                elif command == SEND_RR_DATA and sess == session and session:
                    cip = self._unconnected_data(data)
                    out = handle_cip(self.devices, cip) if cip is not None else None
                    if out is not None:
                        writer.write(encapsulate(command, session, rr_data(out), context=context))
                else:
                    writer.write(encapsulate(command, sess, b"", status=0x65, context=context))   # invalid command/session
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _unconnected_data(data: bytes) -> bytes | None:
        if len(data) < 8:
            return None
        count = struct.unpack("<H", data[6:8])[0]
        i = 8
        for _ in range(count):
            if i + 4 > len(data):
                return None
            kind, size = struct.unpack("<HH", data[i:i + 4])
            if kind == 0x00B2:
                return data[i + 4:i + 4 + size]
            i += 4 + size
        return None


async def read_assembly(host: str, port: int, instance: int, timeout: float = 1.5) -> bytes:
    async def read_frame(reader):
        head = await reader.readexactly(24)
        _cmd, length, _sess, status, _ctx, _opt = struct.unpack("<HHII8sI", head)
        return status, (await reader.readexactly(length) if length else b"")

    async def go() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(encapsulate(REGISTER_SESSION, 0, struct.pack("<HH", 1, 0)))
            await writer.drain()
            head = await reader.readexactly(24)
            session = struct.unpack("<I", head[4:8])[0]
            await reader.readexactly(struct.unpack("<H", head[2:4])[0])
            writer.write(encapsulate(SEND_RR_DATA, session, rr_data(cip_get(instance))))
            await writer.drain()
            status, data = await read_frame(reader)
            if status:
                raise EnipError(f"encapsulation status {status:#x}")
            cip = _cip_reply(data)
            if len(cip) < 4 or cip[2] != 0:
                raise EnipError(f"CIP general status {cip[2] if len(cip) > 2 else '?'}")
            return cip[4:]
        finally:
            writer.close()

    try:
        return await asyncio.wait_for(go(), timeout)
    except (TimeoutError, asyncio.IncompleteReadError, OSError, struct.error) as exc:
        raise EnipError(f"no answer from assembly {instance}: {type(exc).__name__}") from exc


def _cip_reply(data: bytes) -> bytes:
    """The CIP reply inside a SendRRData response."""
    cip = EnipServer._unconnected_data(data)
    if cip is None:
        raise EnipError("no CIP data in the response")
    return cip
