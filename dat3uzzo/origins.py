"""Which web pages may use the helper, judged by the Origin header the browser sends.

Four cases, checked in this order:
1. No Origin header: a program on this PC (the CLI, the tests), a plain <script> tag, or the T3 Code
   desktop window, which strips its own Origin (t3code://app) from every request it sends (T3 Code's
   apps/desktop/src/window/RemoteRequestHeaders.ts). Served as before, and never with the token.
2. An allowed origin: one the person added with `allow-origin`, kept in origins.json in the helper's data
   folder. Only these get the token, inside the bundle. The userscript and the browser extension load the
   bundle on these pages, because a T3 server on another machine has no patched page.
3. A local page: the desktop window's origin, or a loopback origin (127.0.0.0/8, ::1, localhost). The
   patcher writes the token into these pages, so the bundle carries none for them.
4. Anything else, "null" included (sandboxed previews, file:// pages): refused with 403 and no CORS
   headers, on every path, WebSockets too.

The helper reads origins.json on each request, so a change applies with no restart.
"""
from __future__ import annotations

import ipaddress
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config

DESKTOP_ORIGINS = frozenset({"t3code://app", "t3code-dev://app"})  # ElectronProtocol.ts: <scheme>://app
DEFAULT_PORTS = {"http": 80, "https": 443}
NO_MATCH = "https://example.invalid/*"  # a reserved name that never resolves: matches no real page
USERSCRIPT_PATH = "/dat3uzzo.user.js"
EXTENSION_DIR = "browser-extension"


def state_file() -> Path:
    return config.data_dir() / "origins.json"


def normalize(url: str) -> str:
    """The origin of an http(s) URL, as browsers write it: scheme://host[:port], lower case, no default port.
    Raises ValueError for anything else."""
    text = (url or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in DEFAULT_PORTS or not parts.netloc:
        raise ValueError(f"expected an http:// or https:// address, got {text!r}")
    if "@" in parts.netloc:
        raise ValueError(f"an origin has no user name or password: {text!r}")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"bad port in {text!r}") from None
    if not host:
        raise ValueError(f"no host in {text!r}")
    host = f"[{host}]" if ":" in host else host
    suffix = f":{port}" if port and port != DEFAULT_PORTS[parts.scheme] else ""
    return f"{parts.scheme}://{host}{suffix}"


def _is_loopback(origin: str) -> bool:
    host = (urlsplit(origin).hostname or "").rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load(path: Path | None = None) -> list[str]:
    """The allowed origins. A missing or invalid file, or a bad entry, allows nothing extra."""
    try:
        data = json.loads((path or state_file()).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    items = data.get("origins") if isinstance(data, dict) else None
    found = []
    for item in items if isinstance(items, list) else []:
        try:
            origin = normalize(item) if isinstance(item, str) else None
        except ValueError:
            continue
        if origin and origin not in found:
            found.append(origin)
    return found


def save(items: list[str], path: Path | None = None) -> None:
    path = path or state_file()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"origins": sorted(set(items))}, indent=2) + "\n", encoding="utf-8")
    try:
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: another process has the file open for a moment
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def allow(url: str, path: Path | None = None) -> tuple[str, bool]:
    """Add one origin. Returns (origin, True when it was new)."""
    origin = normalize(url)
    items = load(path)
    if origin in items:
        return origin, False
    save(items + [origin], path)
    return origin, True


def deny(url: str, path: Path | None = None) -> tuple[str, bool]:
    """Remove one origin. Returns (origin, True when it was in the list)."""
    origin = normalize(url)
    items = load(path)
    if origin not in items:
        return origin, False
    save([o for o in items if o != origin], path)
    return origin, True


def classify(origin: str | None, path: Path | None = None) -> str:
    """'none', 'allowed', 'local' or 'denied' for the request's Origin header (see the module text)."""
    if not origin:
        return "none"
    if origin in DESKTOP_ORIGINS:
        return "local"
    try:
        clean = normalize(origin)
    except ValueError:
        return "denied"  # "null", other schemes, junk
    if clean != origin.lower():
        return "denied"  # a browser never sends a path, a default port or user info in Origin
    if clean in load(path):
        return "allowed"
    return "local" if _is_loopback(clean) else "denied"


def cors_headers(origin: str | None, verdict: str) -> dict[str, str]:
    """CORS headers for a response. A request with no Origin keeps the old "*": the desktop window strips
    its Origin, but its browser still checks the answer against t3code://app."""
    if verdict == "none":
        return {"Access-Control-Allow-Origin": "*", "Vary": "Origin"}
    if verdict == "local":
        return {"Access-Control-Allow-Origin": origin or "", "Vary": "Origin"}
    if verdict == "allowed":  # Private Network Access: only older Chromium builds read it (README)
        return {"Access-Control-Allow-Origin": origin or "", "Vary": "Origin",
                "Access-Control-Allow-Private-Network": "true"}
    return {}


def token_prefix(port: int, token: str) -> str:
    """The first line of the bundle for an allowed origin: the settings the patched loader sets elsewhere."""
    return f"window.__DAT3UZZO__=window.__DAT3UZZO__||{json.dumps({'port': port, 'token': token})};\n"


def loader_js(port: int) -> str:
    """The userscript's and the extension's code: fetch the bundle from the helper and run it inline, like
    the patched loader, with no token. It retries for about 20 minutes while the helper starts. A refused
    origin looks the same to the page as a stopped helper, because the 403 carries no CORS headers."""
    return (
        "(function(){if(window.__T3UI_REMOTE__)return;window.__T3UI_REMOTE__=1;var n=0;"
        f"function go(){{fetch(\"http://127.0.0.1:{port}/dat3uzzo.js\",{{cache:\"no-store\"}})"
        ".then(function(r){if(!r.ok)throw 0;return r.text()})"
        ".then(function(c){var s=document.createElement(\"script\");s.textContent=c;"
        "(document.head||document.documentElement).appendChild(s)})"
        ".catch(function(){if(++n<44)setTimeout(go,Math.min(2000*Math.pow(2,n-1),30000))})}"
        "try{go()}catch(e){}})();\n"
    )


def match_patterns(items: list[str]) -> list[str]:
    """Browser match patterns for the origins: scheme and host, any port (the helper checks the port)."""
    out = []
    for origin in items:
        parts = urlsplit(origin)
        host = parts.hostname or ""
        pattern = f"{parts.scheme}://{f'[{host}]' if ':' in host else host}/*"
        if pattern not in out:
            out.append(pattern)
    return out or [NO_MATCH]


def _version(path: Path | None = None) -> int:
    try:
        return int((path or state_file()).stat().st_mtime)
    except OSError:
        return 0


def userscript(port: int, path: Path | None = None) -> str:
    """A userscript for Tampermonkey or Violentmonkey. Its version is the list's change time, so the
    manager offers an update after allow-origin or deny-origin."""
    url = f"http://127.0.0.1:{port}{USERSCRIPT_PATH}"
    lines = [
        "// ==UserScript==",
        "// @name         daT3uzzo add-ons for T3 Code",
        "// @namespace    dat3uzzo",
        f"// @version      1.{_version(path)}",
        "// @description  Loads the T3 Code add-ons from the helper on this PC, only on the origins you allowed.",
        *[f"// @match        {p}" for p in match_patterns(load(path))],
        "// @grant        none",
        "// @run-at       document-end",
        "// @noframes",
        f"// @downloadURL  {url}",
        f"// @updateURL    {url}",
        "// ==/UserScript==",
        "// The helper on 127.0.0.1 checks the page's exact origin. Change the list with",
        "// python -m dat3uzzo allow-origin <url> or deny-origin <url>, then update this script.",
    ]
    return "\n".join(lines) + "\n" + loader_js(port)


def extension_files(port: int, path: Path | None = None) -> dict[str, str]:
    """A minimal Manifest V3 extension: one content script in the page's own world, so its requests carry
    the page's Origin. It needs no permissions."""
    manifest = {
        "manifest_version": 3,
        "name": "daT3uzzo add-ons for T3 Code",
        "version": "1.0.0",
        "description": "Loads the T3 Code add-ons from the helper on this PC, only on the origins you allowed.",
        "content_scripts": [{"matches": match_patterns(load(path)), "js": ["loader.js"],
                             "run_at": "document_end", "world": "MAIN"}],
    }
    return {"manifest.json": json.dumps(manifest, indent=2) + "\n", "loader.js": loader_js(port)}


def extension_dir() -> Path:
    return config.data_dir() / EXTENSION_DIR


def write_extension(port: int, folder: Path | None = None, path: Path | None = None) -> Path:
    folder = folder or extension_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in extension_files(port, path).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder
