"""daT3uzzo: one master switch for every T3 Code add-on, plus one switch per add-on.

One list holds every add-on. Each has an id, a label, a description and its kinds:
- "browser": a web/*.js file. It joins the list by itself. Its id is the file name without ".js", and
  its label and description come from header comments near the top of the file:
      // @mod-label Read aloud (text to speech)
      // @mod-desc  One line that says what it does.
- "helper": a feature of this Python helper (an endpoint, a background job). It joins through
  HELPER_MODS below, and checks active(<id>) before it acts.
- "client patch": rebuilt T3 Code page files swapped into the desktop app (client_patch.py). It joins
  through an entry in client-patches.json. Its switch acts at the next T3 Code close, not at Ctrl+R.
An id in both places is one add-on with both kinds. The loader, the patcher, the control script and
/health are the switches' own parts: they are not add-ons and are always on.

The switches live in %LOCALAPPDATA%\\dat3uzzo\\ui.json, for example
    {"enabled": true, "mods": {"t3-read-aloud": true}}
A missing or invalid file, or a missing key, means on: only an explicit false turns something off.
The helper reads the file on every bundle request, so a change applies at the next page load (Ctrl+R),
with no helper restart.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import config

NAME = "daT3uzzo"  # the name people see in Settings, the CLI and the desktop shortcuts
CONTROL_SCRIPT = Path(__file__).with_name("ui_control.js")  # outside web/, so never an add-on
_HEAD_BYTES = 4096

# Helper-side add-ons, one line each: id -> (label, description). The only place they are declared.
HELPER_MODS: dict[str, tuple[str, str]] = {
    "t3-read-aloud": ("Read aloud (text to speech)",
                      "The /speak endpoint that turns a reply into speech, and /settings for voice keys."),
}

def client_mods() -> dict[str, tuple[str, str]]:
    """Client patches: rebuilt T3 Code page files that the helper swaps in while T3 Code is closed
    (client_patch.py). They are declared in client-patches.json, one entry each."""
    from . import client_patch  # client_patch imports this module
    try:
        patches = client_patch.load_config()["patches"]
    except (client_patch.ConfigError, OSError):
        return {}
    return {p["id"]: (p.get("label") or p["id"], p.get("desc") or "") for p in patches}


def client_statuses() -> dict[str, dict]:
    from . import client_patch
    try:
        return client_patch.statuses()
    except Exception as e:  # noqa: BLE001 - Settings must always render
        return {"*": {"state": "failed", "text": f"The status is unreadable: {e}", "restart": False}}


@dataclass(frozen=True)
class Mod:
    id: str
    label: str
    desc: str
    kinds: tuple[str, ...]  # "browser", "helper" or both
    path: Path | None = None  # the web/*.js file of a browser add-on


def state_file() -> Path:
    return config.data_dir() / "ui.json"


def web_dir() -> Path:
    return config.WEB_SCRIPT.parent


def _header(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        head = f.read(_HEAD_BYTES)
    meta: dict[str, str] = {}
    for line in head.splitlines():
        text = line.strip()
        if not text.startswith("//"):
            continue
        text = text[2:].strip()
        for key in ("label", "desc"):
            tag = f"@mod-{key}"
            if text.startswith(tag) and key not in meta:
                meta[key] = text[len(tag):].strip()
    return meta


def find_mods(folder: Path | None = None) -> list[Mod]:
    """Every add-on of both kinds, sorted by id. A new web/*.js file shows up with no code change."""
    folder = folder or web_dir()
    files = {}
    for path in sorted(folder.glob("*.js")):
        try:
            files[path.stem] = (path, _header(path))
        except OSError:
            continue
    client = client_mods()
    mods = []
    for mod_id in sorted(set(files) | set(HELPER_MODS) | set(client)):
        path, meta = files.get(mod_id, (None, {}))
        label, desc = HELPER_MODS.get(mod_id) or client.get(mod_id) or (mod_id, "")
        kinds = tuple(k for k, has in (("browser", path), ("helper", mod_id in HELPER_MODS),
                                       ("client patch", mod_id in client)) if has)
        mods.append(Mod(mod_id, meta.get("label") or label, meta.get("desc") or desc, kinds, path))
    return mods


def read_state(path: Path | None = None) -> dict:
    """Return {"enabled": bool, "mods": {id: bool}}. Missing, unreadable or invalid parts count as on."""
    path = path or state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: a BOM from a hand edit is fine
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("mods")
    mods = {str(k): v is not False for k, v in raw.items()} if isinstance(raw, dict) else {}
    return {"enabled": data.get("enabled") is not False, "mods": mods}


def write_state(state: dict, path: Path | None = None) -> None:
    """Write a temporary file next to ui.json, then replace ui.json with it in one step."""
    path = path or state_file()
    data = {"enabled": bool(state.get("enabled", True)),
            "mods": {str(k): bool(v) for k, v in sorted(state.get("mods", {}).items())}}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: another process has ui.json open for a moment
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def set_master(on: bool, path: Path | None = None) -> dict:
    state = read_state(path)
    state["enabled"] = bool(on)
    write_state(state, path)
    return state


def set_mod(mod_id: str, on: bool, path: Path | None = None) -> dict:
    state = read_state(path)
    state["mods"][mod_id] = bool(on)
    write_state(state, path)
    return state


def active(mod_id: str, path: Path | None = None) -> bool:
    """The one check a helper-side add-on makes before it acts: master switch and its own switch on."""
    state = read_state(path)
    return state["enabled"] and state["mods"].get(mod_id, True)


def view(mods: list[Mod], state: dict) -> dict:
    """The state as the settings page and the CLI show it: every add-on found, with its switch, and
    for a client patch its status (active, applies at the next restart, building, failed)."""
    statuses = client_statuses() if any("client patch" in m.kinds for m in mods) else {}
    out = []
    for m in mods:
        item = {"id": m.id, "label": m.label, "desc": m.desc, "kinds": list(m.kinds),
                "on": state["mods"].get(m.id, True)}
        if "client patch" in m.kinds:
            item["status"] = statuses.get(m.id) or statuses.get("*")
        out.append(item)
    return {"enabled": state["enabled"], "mods": out}


def parse_flag(word: str) -> bool:
    w = word.strip().lower()
    if w in ("1", "on", "true", "yes"):
        return True
    if w in ("0", "off", "false", "no"):
        return False
    raise ValueError(f"expected on or off, got {word!r}")


def apply_query(query: dict[str, list[str]], folder: Path | None = None, path: Path | None = None) -> dict:
    """Apply one change from GET /ui (enabled=0|1, or mod=<id>&on=0|1) and return the new view.
    With neither, change nothing. Raises ValueError for a bad value or an unknown add-on."""
    mods = find_mods(folder)
    first = lambda key: (query.get(key) or [None])[0]  # noqa: E731
    if first("enabled") is not None:
        state = set_master(parse_flag(first("enabled")), path)
    elif first("mod") is not None:
        mod_id = first("mod")
        if mod_id not in {m.id for m in mods}:
            raise ValueError(f"unknown add-on {mod_id!r}")
        if first("on") is None:
            raise ValueError("missing on=0|1")
        state = set_mod(mod_id, parse_flag(first("on")), path)
    else:
        state = read_state(path)
    return view(mods, state)


def guarded(mod_id: str, code: str) -> str:
    """One add-on's code inside its own error guard, named for the browser's developer tools."""
    return (f";(function(){{try{{\n{code}\n}}catch(e){{console.warn('daT3uzzo: add-on '+"
            f"{json.dumps(mod_id)}+' failed',e)}}}})();\n//# sourceURL=dat3uzzo/{quote(mod_id)}.js\n")


def bundle(folder: Path | None = None, path: Path | None = None) -> bytes:
    """The control script, always, plus each enabled add-on when the master switch is on.

    The control script runs each add-on as its own <script> element, so an add-on that throws, or
    that does not even parse, stops only itself. The switches in Settings always load.
    """
    mods = find_mods(folder)
    state = read_state(path)
    load = []
    if state["enabled"]:
        for m in mods:
            if m.path is None or not state["mods"].get(m.id, True):
                continue
            try:
                code = m.path.read_text(encoding="utf-8")
            except OSError:
                continue
            load.append({"id": m.id, "code": guarded(m.id, code)})
    data = view(mods, state) | {"root": str(config.PROJECT_ROOT), "load": load}
    head = "window.__T3UI__=" + json.dumps(data) + ";\n"
    return (head + CONTROL_SCRIPT.read_text(encoding="utf-8")).encode("utf-8")
