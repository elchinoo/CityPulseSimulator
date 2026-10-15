"""PROFINET IO RT_CLASS_1 cyclic frames, device -> controller, carried in UDP datagrams.

Frame (the part after the Ethernet header): FrameID(2) | C_SDU (IO data, IOPS byte, padding to 40 bytes) |
CycleCounter(2, units of 31.25 us) | DataStatus(1) | TransferStatus(1). Device `n` uses FrameID 0xC000 + n.
Real PROFINET puts this in raw Ethernet frames (EtherType 0x8892); this project tunnels it in UDP because raw frames
need privileges. Discovery (DCP) and acyclic services are not implemented.
"""
import asyncio
import struct
import time

from .image import Device

FRAME_ID_BASE = 0xC000
IOPS_GOOD = 0x80
DATA_STATUS_OK = 0x35        # primary, data valid, provider running, no problem
MIN_CSDU = 40


class ProfinetError(Exception):
    pass


def build_frame(device_address: int, data: bytes, cycle_ticks: int, data_status: int = DATA_STATUS_OK) -> bytes:
    csdu = data + bytes([IOPS_GOOD])
    csdu += bytes(max(0, MIN_CSDU - len(csdu)))
    return struct.pack(">H", FRAME_ID_BASE + device_address) + csdu + struct.pack(">HBB", cycle_ticks & 0xFFFF, data_status, 0)


def parse_frame(frame: bytes, data_len: int) -> tuple[int, bytes, int, int]:
    """Returns (device address, IO data, cycle counter, data status)."""
    csdu_len = max(MIN_CSDU, data_len + 1)
    if len(frame) != 2 + csdu_len + 4:
        raise ProfinetError("unexpected frame length")
    frame_id = struct.unpack(">H", frame[:2])[0]
    if not FRAME_ID_BASE <= frame_id <= 0xFBFF:
        raise ProfinetError("not an RT_CLASS_1 frame id")
    cycle, status, _transfer = struct.unpack(">HBB", frame[2 + csdu_len:])
    if frame[2 + data_len] != IOPS_GOOD:
        raise ProfinetError("IO provider status is not good")
    return frame_id - FRAME_ID_BASE, frame[2:2 + data_len], cycle, status


class ProfinetSender:
    """The IO devices: each sends its cyclic frame to the controller every `cycle` seconds."""

    def __init__(self, devices: dict[int, Device], target: tuple[str, int], cycle: float = 1.0):
        self.devices, self.target, self.cycle = devices, target, cycle
        self.transport, self.task = None, None

    async def start(self) -> None:
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=self.target)
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.transport:
            self.transport.close()

    async def _run(self) -> None:
        while True:
            ticks = int(time.monotonic() * 32000)
            for address, device in self.devices.items():
                if device.online:
                    self.transport.sendto(build_frame(address, device.snapshot(), ticks))
            await asyncio.sleep(self.cycle)


class ProfinetReceiver(asyncio.DatagramProtocol):
    """The IO controller side (the gateway): decodes frames and hands each image to `on_image(address, data)`."""

    def __init__(self, sizes: dict[int, int], on_image):
        self.sizes, self.on_image, self.transport = sizes, on_image, None
        self.bad_frames = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            frame_id = struct.unpack(">H", data[:2])[0]
            address = frame_id - FRAME_ID_BASE
            if address not in self.sizes:
                raise ProfinetError("unknown device")
            address, io, _cycle, status = parse_frame(data, self.sizes[address])
            if not status & 0x04:
                raise ProfinetError("data not valid")
        except (ProfinetError, struct.error):
            self.bad_frames += 1
            return
        self.on_image(address, io)

    @classmethod
    async def listen(cls, host: str, sizes: dict[int, int], on_image) -> tuple["ProfinetReceiver", int]:
        transport, proto = await asyncio.get_running_loop().create_datagram_endpoint(lambda: cls(sizes, on_image), local_addr=(host, 0))
        return proto, transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        if self.transport:
            self.transport.close()
