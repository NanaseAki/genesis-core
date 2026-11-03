"""Test-only loopback protocol stub, not an LLM or an Ollama installation."""
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from socketserver import TCPServer
import threading

Response = tuple[int, dict[str, str], bytes]


def synthetic_response(request: dict) -> Response:
    content = "合成 HTTP 回复：" + request["messages"][-1]["content"]
    body = json.dumps({"model": request["model"], "message": {"role": "assistant", "content": content},
                       "done": True, "done_reason": "stop"}, ensure_ascii=False).encode("utf-8")
    return 200, {}, body


class SyntheticOllamaServer:
    """Bounded-lifetime local fixture with observable requests and error reporting.

    Custom responders used by tests must themselves have bounded waits. No
    forwarding, model loading, personal input discovery or outbound sockets.
    """
    def __init__(self, responder: Callable[[dict], Response] = synthetic_response) -> None:
        self.requests: list[dict] = []
        self.errors: list[Exception] = []
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._active = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.connection.settimeout(2)
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.path != "/api/chat" or not 0 < size <= 2 * 1024 * 1024:
                        self.send_error(400)
                        return
                    request = json.loads(self.rfile.read(size))
                    with owner._lock:
                        owner.requests.append(request)
                    status, headers, body = responder(request)
                    self.send_response(status)
                    self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
                    if "Transfer-Encoding" not in headers and "X-No-Length" not in headers:
                        self.send_header("Content-Length", headers.get("Content-Length", str(len(body))))
                    for key, value in headers.items():
                        if key not in {"Content-Type", "Content-Length", "X-No-Length"}:
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when a bounded client rejects/closes a response.
                except Exception as error:
                    with owner._lock:
                        owner.errors.append(error)

            def log_message(self, format: str, *args: object) -> None:
                pass  # Never log request bodies or machine paths.

        class LoopbackServer(ThreadingHTTPServer):
            def process_request(self, request, client_address) -> None:
                # Register before starting the worker: shutdown must also wait
                # for threads that were accepted but have not been scheduled.
                with owner._idle:
                    owner._active += 1
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    with owner._idle:
                        owner._active -= 1
                        owner._idle.notify_all()
                    raise

            def process_request_thread(self, request, client_address) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with owner._idle:
                        owner._active -= 1
                        owner._idle.notify_all()

            def handle_error(self, request, client_address) -> None:
                with owner._lock:
                    owner.errors.append(RuntimeError("synthetic connection handler failed"))

            def server_bind(self) -> None:
                # HTTPServer normally calls getfqdn/gethostbyaddr here. The
                # fixture needs no reverse DNS or host discovery whatsoever.
                TCPServer.server_bind(self)
                self.server_name = "synthetic-loopback"
                self.server_port = self.server_address[1]

        self._server = LoopbackServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "SyntheticOllamaServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(3)
        if self._thread.is_alive():
            raise RuntimeError("synthetic HTTP server did not stop")
        with self._idle:
            if not self._idle.wait_for(lambda: self._active == 0, timeout=5):
                raise RuntimeError("synthetic HTTP handler did not stop")
        if self.errors and exc[0] is None:
            raise RuntimeError("synthetic HTTP handler failed") from self.errors[0]
