"""Industrial protocol stacks for the simulator's field devices and edge gateway.

Every protocol carries the same *process image* (see image.py): a block of bytes holding, per channel, a float32
value and a 16-bit update counter. A field device serves its image; the gateway reads it back with the protocol's own
client and turns changed channels into readings.

Honest scope: Modbus TCP and EtherNet/IP run on their real TCP transports. EtherCAT telegrams run over UDP (port
0x88A4, the EtherCAT-over-UDP encapsulation). PROFINET RT cyclic frames are carried in UDP datagrams and PROFIBUS DP
telegrams over a TCP stream (like a serial-to-Ethernet bridge), because the real transports (raw Ethernet frames,
RS-485) need privileges and hardware. The telegram layouts follow the public specifications as far as this project
needs; they are not certified for conformance and have not been tested against real PLCs.
"""
PROTOCOLS = ("modbus_tcp", "ethernet_ip", "ethercat", "profinet", "profibus")
