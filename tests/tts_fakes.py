"""A fake HTTP service on 127.0.0.1 for the provider tests. No test reaches the network.

Each route answers with a scripted Reply. A reply with several chunks is sent with chunked transfer
encoding and a short pause between chunks, as a streaming service sends it. Every request is kept in
`requests` so a test can check its method, path, headers and body.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FAKE_KEY = "sk-test-0000000000000000000000"  # shaped like a key; it is not one


@dataclass
class Reply:
    status: int = 200
    headers: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)  # bytes; one item = one chunk
    delay: float = 0.03


@dataclass
class Req:
    method: str
    path: str
    query: dict
    headers: dict  # lower-case names
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


def json_reply(payload, status: int = 200) -> Reply:
    return Reply(status, {"Content-Type": "application/json"}, [json.dumps(payload).encode()])


class FakeService:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[Req] = []
        self.sent_last_chunk_at = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def _serve(self) -> None:
                url = urlsplit(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                req = Req(self.command, url.path, {k: v[0] for k, v in parse_qs(url.query).items()},
                          {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(size) if size else b"")
                fake.requests.append(req)
                route = fake.routes.get((self.command, url.path))
                reply = route(req) if callable(route) else route
                if reply is None:
                    reply = Reply(404, {"Content-Type": "application/json"}, [b'{"error":"no route"}'])
                self.send_response(reply.status)
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                if len(reply.chunks) <= 1:
                    body = reply.chunks[0] if reply.chunks else b""
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    fake.sent_last_chunk_at = time.perf_counter()
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i, chunk in enumerate(reply.chunks):
                    if i:
                        time.sleep(reply.delay)
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                fake.sent_last_chunk_at = time.perf_counter()
                self.wfile.write(b"0\r\n\r\n")

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method: str, path: str, reply) -> None:
        self.routes[(method, path)] = reply

    def __enter__(self) -> "FakeService":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class Collector:
    """on_audio and on_piece_done for a synthesize() call."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.first_at = 0.0
        self.pieces = []

    async def on_audio(self, pcm: bytes) -> None:
        if not self.chunks:
            self.first_at = time.perf_counter()
        self.chunks.append(pcm)

    async def on_piece_done(self, done) -> None:
        self.pieces.append(done)

    @property
    def audio(self) -> bytes:
        return b"".join(self.chunks)


class FakeKeyring:
    """The three keyring calls the add-on uses, on a dict. `fail` makes every call raise."""

    def __init__(self, fail: bool = False) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("no backend")

    def get_password(self, service, user):
        self._check()
        return self.data.get((service, user))

    def set_password(self, service, user, value):
        self._check()
        self.data[(service, user)] = value

    def delete_password(self, service, user):
        self._check()
        if (service, user) not in self.data:
            raise type("PasswordDeleteError", (Exception,), {})("not found")
        del self.data[(service, user)]


def pcm_tone(seconds: float, rate: int = 24000) -> bytes:
    """A quiet saw wave, 16-bit mono: valid audio of an exact length."""
    n = int(seconds * rate)
    return b"".join(((i % 200) * 50 - 5000).to_bytes(2, "little", signed=True) for i in range(n))
