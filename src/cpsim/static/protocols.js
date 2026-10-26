import { $, fill, h } from "/kit/ui.js";
import { flushCard } from "/static/cards.js";

const ROWS = [
  ["Modbus TCP", "TCP", "Real framing (MBAP, function 0x03/0x04). A third-party client (pymodbus) is part of the test suite.", "One port, many unit ids; reads input registers."],
  ["EtherNet/IP", "TCP 44818 style", "Encapsulation + CIP Get_Attribute_Single on an Assembly object (instance 100 + device).", "Explicit messaging only; no implicit (UDP I/O) connections."],
  ["EtherCAT", "UDP (0x88A4)", "Frame + datagram layout with the logical read command and working counter.", "EtherCAT-over-UDP, not raw Ethernet; one logical read per device."],
  ["PROFINET", "UDP tunnel", "RT_CLASS_1 cyclic frames (frame id, IO data, IOPS, cycle counter, data status) pushed by the devices.", "Raw layer-2 frames need privileges, so the frame is carried in UDP; no DCP discovery or acyclic services."],
  ["PROFIBUS DP", "TCP stream", "SD1/SD2/SC telegrams with FCS and end delimiter; the master polls slave addresses.", "A serial-to-Ethernet bridge style; function codes simplified."],
];

export function initProtocols() {
  fill($("view-protocols"), h("h1", {}, "Protocols"),
    h("p", { class: "muted" }, "Every sensor group can use any of these. The simulator hosts the field devices and an edge gateway polls them with the protocol's own client, then forwards readings to CityPulse over HTTPS, as a plant gateway would. Industrial protocols are terminated at the edge; CityPulse never needs a raw Modbus or PROFINET port, which is also what lets this run on a platform that only exposes HTTP."),
    flushCard("Protocols", h("table", {}, h("thead", {}, h("tr", {}, ...["Protocol", "Transport here", "What is implemented", "Limits"].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...ROWS.map((r) => h("tr", {}, h("td", { class: "title" }, r[0]), h("td", {}, r[1]), h("td", { class: "wrap" }, r[2]), h("td", { class: "wrap" }, r[3])))))),
    h("div", { class: "banner" }, h("strong", {}, "Honest scope: "), "the telegram layouts follow the public specifications as far as this project needs. They are not certified for conformance and have not been tested against real PLCs. Only Modbus TCP is checked against a third-party implementation."),
    h("h2", { class: "section-title" }, "Read a simulated device yourself"),
    h("p", { class: "muted" }, "Enable “Let other machines reach the protocol endpoints” on a city (or stay on the same machine), start it, and read the address shown on the city page. For example with Python and pymodbus (unit 1 is the first device; 6 bytes per sensor: a float32 and a counter):"),
    h("pre", { class: "code" }, h("code", {}, "from pymodbus.client import ModbusTcpClient\nc = ModbusTcpClient('127.0.0.1', port=<port from the city page>)\nprint(c.read_input_registers(0, count=6, device_id=1).registers)")));
}
