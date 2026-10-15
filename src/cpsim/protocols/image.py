"""The process image: 6 bytes per channel = float32 value (big endian) + uint16 update counter."""
import struct

CHANNEL_BYTES = 6
MAX_CHANNELS = 32            # 192 bytes: fits a PROFIBUS DP telegram (244), 96 Modbus registers (125), one PROFINET frame


class Device:
    """A field device: an address on its protocol, a process image, and a link that can be cut."""

    def __init__(self, protocol: str, address: int, channels: int):
        if not 1 <= channels <= MAX_CHANNELS:
            raise ValueError(f"a device has 1 to {MAX_CHANNELS} channels")
        self.protocol, self.address, self.channels = protocol, address, channels
        self.image = bytearray(channels * CHANNEL_BYTES)
        self.online = True

    def set(self, index: int, value: float, seq: int) -> None:
        struct.pack_into(">fH", self.image, index * CHANNEL_BYTES, value, seq & 0xFFFF)

    def snapshot(self) -> bytes:
        return bytes(self.image)


def unpack(data: bytes, channels: int) -> list[tuple[float, int]]:
    if len(data) < channels * CHANNEL_BYTES:
        raise ValueError("process image shorter than expected")
    return [struct.unpack_from(">fH", data, i * CHANNEL_BYTES) for i in range(channels)]
