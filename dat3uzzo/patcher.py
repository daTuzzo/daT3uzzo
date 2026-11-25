"""Keep one marked loader block in each installed T3 Code version's client/index.html.

T3 Code installs every update as a new folder under <t3 home>/runtime/versions/<version>/, and
its server reads client/index.html from disk on every request. So the watcher only has to find
new or replaced index files and add the block again. Nothing else in T3 Code is touched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

START = "<!-- dat3uzzo:start -->"
END = "<!-- dat3uzzo:end -->"
# The markers of the first release, when the project was called t3-read-aloud. A block with them is
# replaced like the current block, so an old install moves to the new markers by itself and no old
# block stays behind (migrate.py).
LEGACY_MARKERS = (("<!-- t3-read-aloud:start -->", "<!-- t3-read-aloud:end -->"),)
MARKERS = ((START, END), *LEGACY_MARKERS)
ANY_BLOCK = "(?:" + "|".join(re.escape(s) + r".*?" + re.escape(e) for s, e in MARKERS) + ")"
BLOCK_RE = re.compile(ANY_BLOCK + r"\r?\n?", re.S)
log = logging.getLogger("dat3uzzo.patcher")


def has_block(text: str) -> bool:
    """A loader block with the current or an old pair of markers."""
    return any(s in text for s, _ in MARKERS)


def has_legacy(text: str) -> bool:
    """Any trace of an old marker, start or end."""
    return any(s in text or e in text for s, e in LEGACY_MARKERS)


def one_block(text: str, pattern: re.Pattern, first: str, rest=lambda m: "") -> str:
    """Replace the first match of pattern with `first` and every later match with rest(match)."""
    seen = []

    def swap(m):
        seen.append(m)
        return first if len(seen) == 1 else rest(m)
    return pattern.sub(swap, text)


def loader_block(port: int, token: str) -> str:
    # The desktop window's Content-Security-Policy allows inline scripts and http: connections, but
    # no script files from other origins. So the loader fetches the script text and runs it inline.
    # If the helper is down, the fetch fails quietly and T3 Code works as before.
    # The helper can start after T3 Code (at logon it often does), so a failed fetch retries:
    # after 2, 4, 8, 16 s, then every 30 s, for about 20 minutes.
    return (
        f"{START}<script>(function(){{window.__DAT3UZZO__={{port:{port},token:\"{token}\"}};var n=0;"
        f"function go(){{fetch(\"http://127.0.0.1:{port}/dat3uzzo.js\",{{cache:\"no-store\"}})"
        f".then(function(r){{if(!r.ok)throw 0;return r.text()}}).then(function(c){{"
        f"var s=document.createElement(\"script\");s.textContent=c;document.head.appendChild(s)}})"
        f".catch(function(){{if(++n<44)setTimeout(go,Math.min(2000*Math.pow(2,n-1),30000))}})}}"
        f"try{{go()}}catch(e){{}}}})();</script>{END}"
    )


def index_files(t3_home: Path) -> list[Path]:
    # Skip folders starting with "." such as ".staging-xyz": the updater is still building them.
    return sorted(p for p in (t3_home / "runtime" / "versions").glob("*/client/index.html")
                  if not p.parent.parent.name.startswith("."))


def _read(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".dat3uzzo-tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def patched_text(html: str, block: str) -> str | None:
    """Return the new text, or None when the file has no </head> to anchor on."""
    newline = "\r\n" if "\r\n" in html else "\n"
    if has_block(html):  # one block stays, with the current markers
        return one_block(html, BLOCK_RE, block + newline)
    at = html.lower().rfind("</head>")
    if at < 0:
        return None
    return html[:at] + block + newline + html[at:]


def unpatched_text(html: str) -> str:
    return BLOCK_RE.sub("", html)


def patch_file(path: Path, block: str) -> str:
    html = _read(path)
    new = patched_text(html, block)
    if new is None:
        return "skipped"
    if new == html:
        return "current"
    _write_atomic(path, new)
    return "patched"


def unpatch_file(path: Path) -> str:
    html = _read(path)
    new = unpatched_text(html)
    if new == html:
        return "clean"
    _write_atomic(path, new)
    return "removed"


def patch_all(t3_home: Path, block: str) -> dict[str, str]:
    result = {}
    for path in index_files(t3_home):
        try:
            result[path.parent.parent.name] = patch_file(path, block)
        except OSError as e:
            result[path.parent.parent.name] = f"error: {e.strerror or e}"
    return result


def unpatch_all(t3_home: Path) -> dict[str, str]:
    result = {}
    for path in index_files(t3_home):
        try:
            result[path.parent.parent.name] = unpatch_file(path)
        except OSError as e:
            result[path.parent.parent.name] = f"error: {e.strerror or e}"
    return result


async def watch(t3_home: Path, block: str, interval: float = 1.0, desktop_asars=None) -> None:
    """Re-check every page whose size or mtime changed: the server's copies and the desktop app's
    archive. Cheap: a directory glob and a few stat calls per tick."""
    from . import desktop  # desktop imports this module

    find_asars = desktop_asars or desktop.find_asars
    seen: dict[Path, tuple[int, int]] = {}
    while True:
        try:
            for asar in find_asars():
                try:
                    st = asar.stat()
                except OSError:
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                if seen.get(asar) == sig:
                    continue
                outcome = desktop.patch_asar(asar, block)
                if outcome != "current":
                    log.info("desktop %s: %s", asar.parent.parent.name, outcome)
                if outcome.startswith("error: the archive is locked"):
                    continue  # the updater may still hold it; retry next tick
                st = asar.stat()
                seen[asar] = (st.st_mtime_ns, st.st_size)
            for path in index_files(t3_home):
                try:
                    st = path.stat()
                except OSError:
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                if seen.get(path) == sig:
                    continue
                try:
                    outcome = patch_file(path, block)
                except OSError as e:  # the updater may still be writing the folder; retry next tick
                    log.warning("patch failed for %s: %s", path.parent.parent.name, e)
                    continue
                if outcome == "patched":
                    log.info("patched %s", path.parent.parent.name)
                st = path.stat()
                seen[path] = (st.st_mtime_ns, st.st_size)
        except Exception:  # never let the watcher die
            log.exception("watch tick failed")
        await asyncio.sleep(interval)
