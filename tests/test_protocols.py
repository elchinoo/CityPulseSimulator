"""Each protocol stack against itself, and Modbus TCP against a third-party client (pymodbus)."""
import asyncio
import struct

import pytest

from cpsim.protocols import enip, ethercat, modbus, profibus, profinet
from cpsim.protocols.image import CHANNEL_BYTES, Device, unpack


def device(proto="modbus_tcp", address=1, values=(21.5, -3.25, 0.0)) -> Device:
    d = Device(proto, address, len(values))
    for i, v in enumerate(values):
        d.set(i, v, i + 7)
    return d


def check(data: bytes, values=(21.5, -3.25, 0.0)):
    got = unpack(data, len(values))
    assert [round(v, 3) for v, _ in got] == list(values) and [s for _, s in got] == [7, 8, 9]


def test_image_round_trips_values_and_counters():
    d = device()
    assert len(d.snapshot()) == 3 * CHANNEL_BYTES
    check(d.snapshot())
    d.set(0, 1.0, 65536 + 5)                       # the counter wraps at 16 bits
    assert unpack(d.snapshot(), 3)[0][1] == 5
    with pytest.raises(ValueError):
        Device("x", 1, 33)


# ---- Modbus TCP -----------------------------------------------------------------------------------
async def test_modbus_own_client_and_server():
    devices = {1: device(), 2: device(values=(1.0,))}
    server = modbus.ModbusServer(devices)
    port = await server.start("127.0.0.1")
    try:
        check(await modbus.read_input_registers("127.0.0.1", port, 1, 9))
        assert len(await modbus.read_input_registers("127.0.0.1", port, 2, 3)) == 6
        with pytest.raises(modbus.ModbusError, match="exception 0x02"):
            await modbus.read_input_registers("127.0.0.1", port, 1, 10)            # past the image
        with pytest.raises(modbus.ModbusError, match="exception 0x0b"):
            await modbus.read_input_registers("127.0.0.1", port, 99, 1)            # no such unit
        devices[1].online = False
        with pytest.raises(modbus.ModbusError, match="no answer"):
            await modbus.read_input_registers("127.0.0.1", port, 1, 9, timeout=0.3)
    finally:
        await server.stop()


async def test_modbus_interoperates_with_pymodbus():
    """A third-party client must read our server: this is the check that the framing is real Modbus TCP."""
    from pymodbus.client import AsyncModbusTcpClient

    server = modbus.ModbusServer({5: device()})
    port = await server.start("127.0.0.1")
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    try:
        assert await client.connect()
        try:
            rr = await client.read_input_registers(0, count=9, device_id=5)
        except TypeError:
            rr = await client.read_input_registers(0, count=9, slave=5)
        assert not rr.isError()
        check(struct.pack(">9H", *rr.registers))
        holding = await client.read_holding_registers(0, count=2, device_id=5)
        assert not holding.isError()
        bad = await client.write_register(0, 1, device_id=5)                       # writes are not supported
        assert bad.isError()
    finally:
        client.close()
        await server.stop()


def test_modbus_rejects_malformed_requests():
    devices = {1: device()}
    assert modbus.respond(devices, struct.pack(">HHHB", 1, 9, 2, 1) + b"\x04") is None            # protocol id != 0
    out = modbus.respond(devices, modbus.request(1, 1, 0x04, 0, 0))                                 # zero registers
    assert out[7] == 0x84 and out[8] == modbus.EXC_VALUE
    out = modbus.respond(devices, modbus.request(1, 1, 0x04, 0, 126))
    assert out[8] == modbus.EXC_VALUE


# ---- EtherNet/IP ----------------------------------------------------------------------------------
async def test_enip_reads_an_assembly_and_reports_errors():
    devices = {1: device("ethernet_ip"), 300: device("ethernet_ip")}
    server = enip.EnipServer(devices)
    port = await server.start("127.0.0.1")
    try:
        check(await enip.read_assembly("127.0.0.1", port, enip.INSTANCE_BASE + 1))
        check(await enip.read_assembly("127.0.0.1", port, enip.INSTANCE_BASE + 300))           # 16-bit instance segment
        with pytest.raises(enip.EnipError, match="status 5"):
            await enip.read_assembly("127.0.0.1", port, enip.INSTANCE_BASE + 77)
        devices[1].online = False
        with pytest.raises(enip.EnipError, match="no answer"):
            await enip.read_assembly("127.0.0.1", port, enip.INSTANCE_BASE + 1, timeout=0.3)
    finally:
        await server.stop()


def test_enip_cip_errors_and_framing():
    devices = {1: device("ethernet_ip")}
    assert enip.handle_cip(devices, bytes([0x10, 0]))[2] == enip.ST_SERVICE_UNSUPPORTED            # not Get_Attribute_Single
    wrong_attr = bytes([0x0E, 3, 0x20, 0x04, 0x24, 101, 0x30, 9])
    assert enip.handle_cip(devices, wrong_attr)[2] == enip.ST_ATTR_UNSUPPORTED
    wrong_class = bytes([0x0E, 3, 0x20, 0x05, 0x24, 101, 0x30, 3])
    assert enip.handle_cip(devices, wrong_class)[2] == enip.ST_PATH_UNKNOWN
    assert enip.handle_cip(devices, b"\x0e")[2] == enip.ST_SERVICE_UNSUPPORTED                     # truncated request
    frame = enip.encapsulate(enip.REGISTER_SESSION, 0, struct.pack("<HH", 1, 0))
    assert len(frame) == 24 + 4 and struct.unpack("<H", frame[:2])[0] == 0x65
    assert enip._parse_path(b"\x20\x04\x25\x00\x2c\x01\x30\x03") == (4, 300, 3)
    assert enip._parse_path(b"\x99") == (None, None, None)


async def test_enip_refuses_requests_without_a_session():
    server = enip.EnipServer({1: device("ethernet_ip")})
    port = await server.start("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(enip.encapsulate(enip.SEND_RR_DATA, 12345, enip.rr_data(enip.cip_get(101))))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(24), 1)
        assert struct.unpack("<I", head[8:12])[0] == 0x65                                       # invalid session
        writer.close()
    finally:
        await server.stop()


# ---- EtherCAT --------------------------------------------------------------------------------------
def test_ethercat_frame_round_trip_and_chained_datagrams():
    frame = ethercat.build([(ethercat.CMD_LRD, 1, 0x100, b"\x00" * 6), (ethercat.CMD_LRD, 2, 0x200, b"\x00" * 4)])
    parsed = ethercat.parse(frame)
    assert [(c, i, a, len(d)) for c, i, a, d, _ in parsed] == [(0x0A, 1, 0x100, 6), (0x0A, 2, 0x200, 4)]
    assert struct.unpack("<H", frame[:2])[0] >> 12 == 1
    for broken in (b"", b"\x05", frame[:-3], b"\x10\x20" + frame[2:]):
        with pytest.raises(ethercat.EtherCatError):
            ethercat.parse(broken)


async def test_ethercat_logical_read_and_working_counter():
    devices = {1: device("ethercat"), 2: device("ethercat", 2, (9.0,))}
    server = ethercat.EtherCatServer(devices)
    port = await server.start("127.0.0.1")
    try:
        check(await ethercat.read_logical("127.0.0.1", port, 1, 18))
        assert len(await ethercat.read_logical("127.0.0.1", port, 2, 6)) == 6
        with pytest.raises(ethercat.EtherCatError, match="working counter 0"):
            await ethercat.read_logical("127.0.0.1", port, 9, 6)                              # nobody owns it
        with pytest.raises(ethercat.EtherCatError, match="working counter 0"):
            await ethercat.read_logical("127.0.0.1", port, 2, 600)                            # runs past the image
        devices[1].online = False
        with pytest.raises(ethercat.EtherCatError, match="working counter 0"):                  # an offline slave never counts
            await ethercat.read_logical("127.0.0.1", port, 1, 18, timeout=0.3)
    finally:
        await server.stop()


# ---- PROFINET --------------------------------------------------------------------------------------
def test_profinet_frame_round_trip_and_validation():
    d = device("profinet", 3)
    frame = profinet.build_frame(3, d.snapshot(), 1234)
    assert struct.unpack(">H", frame[:2])[0] == 0xC003 and len(frame) == 2 + 40 + 4          # padded to the 40-byte minimum
    address, data, cycle, status = profinet.parse_frame(frame, len(d.snapshot()))
    assert (address, cycle, status) == (3, 1234, 0x35)
    check(data)
    for broken in (frame[:-1], b"\x00\x01" + frame[2:], frame[:2 + 18] + b"\x00" + frame[2 + 19:]):
        with pytest.raises(profinet.ProfinetError):
            profinet.parse_frame(broken, 18)


async def test_profinet_devices_push_cyclic_frames_to_the_controller():
    devices = {1: device("profinet", 1), 2: device("profinet", 2, (5.5,))}
    got: dict[int, bytes] = {}
    receiver, port = await profinet.ProfinetReceiver.listen("127.0.0.1", {1: 18, 2: 6}, lambda a, d: got.__setitem__(a, d))
    sender = profinet.ProfinetSender(devices, ("127.0.0.1", port), cycle=0.1)
    await sender.start()
    try:
        for _ in range(30):
            if len(got) == 2:
                break
            await asyncio.sleep(0.05)
        check(got[1])
        assert len(got[2]) == 6
        devices[1].online = False
        got.clear()
        await asyncio.sleep(0.35)
        assert 1 not in got and 2 in got                                                       # a dead device goes silent
        receiver.datagram_received(b"\xc0\x09" + bytes(46), None)                              # unknown device id
        receiver.datagram_received(b"junk", None)
        assert receiver.bad_frames == 2
    finally:
        await sender.stop()
        receiver.close()


# ---- PROFIBUS DP -----------------------------------------------------------------------------------
def test_profibus_telegrams_and_checksums():
    t = profibus.sd2(2, 5, 0x08, b"\x01\x02\x03")
    assert t[0] == 0x68 and t[1] == t[2] == 6 and t[3] == 0x68 and t[-1] == 0x16
    assert profibus.parse(t) == (("SD2", 2, 5, 8, b"\x01\x02\x03"), len(t))
    s = profibus.sd1(5, 2, 0x4D)
    assert profibus.parse(s) == (("SD1", 5, 2, 0x4D, b""), 6)
    assert profibus.parse(t[:5]) == (None, 0)                                                   # incomplete: wait for more
    assert profibus.parse(b"\xe5")[0][0] == "SC"
    for broken in (t[:-2] + b"\x00\x16", t[:-1] + b"\x17", s[:-2] + b"\x00\x16", b"\x99", b"\x68\x05\x06\x68" + bytes(8)):
        with pytest.raises(profibus.ProfibusError):
            profibus.parse(broken)
    with pytest.raises(profibus.ProfibusError):
        profibus.sd2(1, 2, 3, bytes(245))


async def test_profibus_master_polls_slaves_over_the_stream():
    devices = {4: device("profibus", 4), 5: device("profibus", 5, (7.0,))}
    server = profibus.ProfibusServer(devices)
    port = await server.start("127.0.0.1")
    try:
        check(await profibus.data_exchange("127.0.0.1", port, 4))
        assert len(await profibus.data_exchange("127.0.0.1", port, 5)) == 6
        with pytest.raises(profibus.ProfibusError, match="no answer"):                          # a missing slave: silence
            await profibus.data_exchange("127.0.0.1", port, 99, timeout=0.3)
        devices[4].online = False
        with pytest.raises(profibus.ProfibusError, match="no answer"):
            await profibus.data_exchange("127.0.0.1", port, 4, timeout=0.3)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)                       # line noise, then a good request
        writer.write(b"\x00\xff\x13" + profibus.sd1(5, 2, profibus.FC_REQUEST))
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(profibus.sd1(5, 2, profibus.FC_REQUEST))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), 1)
        assert head[0] == 0x68
        writer.close()
    finally:
        await server.stop()
