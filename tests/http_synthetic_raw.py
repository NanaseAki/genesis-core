"""Raw loopback wire fixture; deliberately bypass HTTP server normalization."""
import socket
import threading


class RawHTTPServer:
    def __init__(self, response: bytes, *, hold_open: bool = False) -> None:
        self.response = response
        self.hold_open = hold_open
        self.release = threading.Event()
        self.requests = 0
        self.request_headers = b""
        self.errors: list[BaseException] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(4)
        self.endpoint = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(3)
                packet = b""
                while b"\r\n\r\n" not in packet:
                    part = connection.recv(4096)
                    if not part or len(packet) > 65536:
                        raise RuntimeError("invalid synthetic request headers")
                    packet += part
                head, body = packet.split(b"\r\n\r\n", 1)
                self.request_headers = head
                length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:")))
                if not 0 <= length <= 2 * 1024 * 1024:
                    raise RuntimeError("oversized synthetic request")
                while len(body) < length:
                    part = connection.recv(4096)
                    if not part:
                        raise RuntimeError("incomplete synthetic request")
                    body += part
                self.requests += 1
                connection.sendall(self.response)
                if self.hold_open:
                    self.release.wait(3)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # A correct client can reject without draining the wire body.
        except BaseException as error:
            self.errors.append(error)
        finally:
            self.listener.close()

    def __enter__(self) -> "RawHTTPServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.listener.close()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("raw synthetic server cleanup timeout")
        if self.errors and exc[0] is None:
            raise RuntimeError("raw synthetic server failed") from self.errors[0]
