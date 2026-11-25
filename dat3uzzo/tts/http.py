"""HTTP for the providers: stdlib urllib in a worker thread, bridged to asyncio.

Redirects are refused, as T3 Code does (`redirect: "error"`): urllib copies the Authorization header
to a redirect target, so a redirect could send a key to another host. A streamed body is read with
read1(), so each chunk reaches the caller as soon as it arrives.
"""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from email.message import Message

CHUNK = 16384


class HttpError(Exception):
    """A non-2xx answer. `body` is at most 4 KB of text, for error mapping only, never for logs."""

    def __init__(self, status: int, body: str, headers: Message | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = headers

    def json(self) -> dict:
        try:
            data = json.loads(self.body)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class NetworkError(Exception):
    """The request did not reach the service or the answer stopped early."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ARG002
        return None  # urllib then raises HTTPError for the 3xx answer


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, headers: dict, body: bytes | None) -> urllib.request.Request:
    return urllib.request.Request(url, data=body, method=method, headers=headers)


def _http_error(e: urllib.error.HTTPError) -> HttpError:
    try:
        text = e.read(4096).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        text = ""
    return HttpError(e.code, text, e.headers)


def _fetch_sync(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    try:
        with _OPENER.open(_request(method, url, headers, body), timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except (urllib.error.URLError, OSError) as e:
        raise NetworkError(type(e).__name__) from None


async def fetch(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
                timeout: float = 60.0) -> tuple[int, Message, bytes]:
    return await asyncio.to_thread(_fetch_sync, method, url, dict(headers or {}), body, timeout)


async def get_json(url: str, *, headers: dict | None = None, timeout: float = 20.0) -> dict:
    _, _, data = await fetch("GET", url, headers=headers, timeout=timeout)
    try:
        out = json.loads(data)
    except ValueError:
        raise NetworkError("not JSON") from None
    return out if isinstance(out, dict) else {}


def json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Stream:
    """An open streamed answer: `status`, `headers`, then `async for chunk in stream`."""

    def __init__(self) -> None:
        self.status = 0
        self.headers: Message = Message()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            while True:
                kind, value = await self._queue.get()
                if kind == "data":
                    yield value
                elif kind == "end":
                    return
                else:
                    raise value
        finally:
            self._stop.set()

    async def read_all(self) -> bytes:
        out = bytearray()
        async for chunk in self:
            out += chunk
        return bytes(out)


async def open_stream(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
                      timeout: float = 60.0) -> Stream:
    """Send the request and wait for the status line. Raises HttpError for a non-2xx answer."""
    loop = asyncio.get_running_loop()
    stream = Stream()
    opened: asyncio.Future = loop.create_future()

    def put(kind: str, value=None) -> None:
        loop.call_soon_threadsafe(stream._queue.put_nowait, (kind, value))

    def settle(value=None, error: BaseException | None = None) -> None:
        def apply() -> None:
            if opened.done():
                return
            if error is not None:
                opened.set_exception(error)
            else:
                opened.set_result(value)
        loop.call_soon_threadsafe(apply)

    def work() -> None:
        try:
            r = _OPENER.open(_request(method, url, dict(headers or {}), body), timeout=timeout)
        except urllib.error.HTTPError as e:
            settle(error=_http_error(e))
            return
        except (urllib.error.URLError, OSError) as e:
            settle(error=NetworkError(type(e).__name__))
            return
        settle((r.status, r.headers))
        try:
            with r:
                while not stream._stop.is_set():
                    chunk = r.read1(CHUNK)
                    if not chunk:
                        break
                    put("data", chunk)
            put("end")
        except Exception as e:  # noqa: BLE001  # a reset or a timeout mid-body
            put("error", NetworkError(type(e).__name__))

    threading.Thread(target=work, name="t3ra-http", daemon=True).start()
    stream.status, stream.headers = await opened
    return stream


async def lines(stream: Stream) -> AsyncIterator[bytes]:
    """Newline-delimited records from a stream; a record split across chunks is joined."""
    buf = b""
    async for chunk in stream:
        buf += chunk
        *done, buf = buf.split(b"\n")
        for line in done:
            if line.strip():
                yield line
    if buf.strip():
        yield buf
