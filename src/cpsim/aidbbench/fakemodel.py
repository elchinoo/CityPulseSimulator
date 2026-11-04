"""A scripted OpenAI-compatible model server for the agent scenarios (no model, no key, no network beyond the machine).

Derived from the kit's tests/aidbfake.py, changed in three ways a load test needs: it is threaded (the kit's serves one request at a
time, which would turn N parallel conversations into a queue), its answers are stateless (decided from the request, not from a FIFO
script, so concurrent conversations do not steal each other's replies) and it counts requests and the highest number in flight.

What it does: when asked to continue a conversation that has no tool result yet, it answers with a tool call for `tool` (AIDB simulates
tool calling in the prompt: the call is the JSON text `{"tool_calls": [...]}`); once the transcript contains the tool's result it answers
with plain text. `delay` seconds are added to every answer to stand in for model latency. Answers are canned: this measures AIDB's agent
machinery, the database work around it and the model hop, NOT any real model.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_RESULT_MARKER = "[tool_response:"      # how AIDB 7.9 writes a tool's result into the prompt it sends (checked against 7.9.0)


def has_tool_result(body: dict) -> bool:
    """True when the prompt the model is asked to continue already holds a tool's result."""
    for m in body.get("messages", []):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str) and TOOL_RESULT_MARKER in c:
            return True
    return False


class FakeModel:
    def __init__(self, *, host: str = "0.0.0.0", port: int = 0, delay: float = 0.2, tool: str = "", tool_args: dict | None = None):  # noqa: S104
        self.delay, self.tool, self.tool_args = delay, tool, tool_args or {}
        self.lock = threading.Lock()
        self.requests = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.tool_calls_issued = 0
        self.last_body: dict | None = None
        self.bodies: list[dict] = []          # kept only while `keep_bodies` is true (debugging)
        self.keep_bodies = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                with outer.lock:
                    outer.requests += 1
                    outer.in_flight += 1
                    outer.max_in_flight = max(outer.max_in_flight, outer.in_flight)
                    outer.last_body = body
                    if outer.keep_bodies:
                        outer.bodies.append(body)
                try:
                    if outer.delay:
                        time.sleep(outer.delay)
                    if self.path.endswith("/chat/completions"):
                        if outer.tool and not has_tool_result(body):
                            text = json.dumps({"tool_calls": [{"name": outer.tool, "arguments": outer.tool_args}]})
                            with outer.lock:
                                outer.tool_calls_issued += 1
                        else:
                            text = "The latest observations were checked; the sensors named in the results look as reported."
                        out = {"id": "cpsim", "object": "chat.completion", "created": 0, "model": body.get("model"),
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                    else:
                        inp = body.get("input")
                        inp = inp if isinstance(inp, list) else [inp]
                        out = {"object": "list", "model": body.get("model"), "usage": {"prompt_tokens": 1, "total_tokens": 1},
                               "data": [{"object": "embedding", "index": i, "embedding": [0.1] * 8} for i, _ in enumerate(inp)]}
                    data = json.dumps(out).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    with outer.lock:
                        outer.in_flight -= 1

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stats(self) -> dict:
        with self.lock:
            return {"requests": self.requests, "tool_calls_issued": self.tool_calls_issued, "max_in_flight": self.max_in_flight,
                    "delay_s": self.delay}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
