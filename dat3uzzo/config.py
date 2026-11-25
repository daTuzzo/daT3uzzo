"""Paths, port, model and the per-install token. Nothing here reads the API key's value."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

APP_ID = "dat3uzzo"  # the package, the state folder, /health's "name"
HOST = "127.0.0.1"
PORT = int(os.environ.get("DAT3UZZO_PORT", "47613"))
# Chosen by a measured test (docs/test-results.md): the mini model misread digits and table rows.
MODEL = os.environ.get("DAT3UZZO_MODEL", "gpt-realtime-2.1")
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
DEFAULT_VOICE = "marin"
MODES = ("adapt", "exact")
MAX_TEXT_CHARS = 60_000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_SCRIPT = PROJECT_ROOT / "web" / "t3-read-aloud.js"


class KeyMissing(RuntimeError):
    """T3 Code has no OpenAI dictation key saved as a value."""


def t3_home() -> Path:
    """T3 Code's home folder. Stable and Nightly share it: ~/.t3, or T3CODE_HOME when it is set
    (T3 Code's apps/desktop/src/app/DesktopStatePaths.ts). DAT3UZZO_T3_HOME overrides both (tests)."""
    for name in ("DAT3UZZO_T3_HOME", "T3CODE_HOME"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value)
    return Path.home() / ".t3"


def key_file() -> Path:
    # T3 Code saves the OpenAI dictation key here when it is entered as a value (Settings > Voice).
    return t3_home() / "userdata" / "secrets" / "voice-cloud-openai.bin"


def read_key() -> str:
    """Read the key for one request. Read fresh each time, so a key changed in T3 Code applies at once."""
    path = key_file()
    try:
        key = path.read_bytes().decode("utf-8").strip()
    except FileNotFoundError:
        raise KeyMissing(
            "No OpenAI key found. In T3 Code, open Settings > Voice, choose OpenAI and save the key as a value."
        ) from None
    except UnicodeDecodeError:
        raise KeyMissing("The saved OpenAI key file is not plain text. Save the key again in T3 Code.") from None
    if not key:
        raise KeyMissing("The saved OpenAI key is empty. Save it again in T3 Code under Settings > Voice.")
    return key


def data_base() -> Path:
    """%LOCALAPPDATA% on Windows, else ~/.local/share. The state folder and the old one sit in it."""
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share"))


def data_dir() -> Path:
    path = data_base() / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def token() -> str:
    """A random token baked into the injected loader. Only pages served by T3 Code know it."""
    path = data_dir() / "token"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    value = secrets.token_hex(24)
    path.write_text(value, encoding="utf-8")
    return value
