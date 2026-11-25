"""Where a provider's API key comes from. Nothing here logs, prints or returns a key to the page.

Two sources, as in T3 Code's own speech-to-text settings:
- "t3": the key T3 Code saved for dictation (Settings > Voice). T3 Code keeps it in one of two ways:
    * as a value: userdata/secrets/voice-cloud-<provider>.bin holds the key, and settings.json holds
      a redaction marker in cloudSpeechCredentials.<provider>;
    * as a reference: settings.json holds {"provider": <secret provider id>, "reference": "op://..."}
      and no .bin file exists. The add-on resolves it the way T3 Code's 1Password driver does:
      `op --cache=false read --account=<account> --no-newline <reference>`.
  T3 Code holds keys for openai, xai and openrouter only.
- "own": a separate key for read-aloud, kept by the keyring package (MIT) in the system key store:
  Windows Credential Manager, macOS Keychain or Secret Service. Never in a plain file.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .. import config
from .base import TtsError, message

SERVICE = "t3-read-aloud"          # the keyring service name; the user name is the provider id
T3_PROVIDERS = ("openai", "xai", "openrouter")
SOURCES = ("t3", "own")
MAX_KEY = 4096
OP_TIMEOUT = 45  # seconds, as T3 Code's 1Password driver
_KEY = re.compile(r"^(?![a-z][a-z\d+.-]*://)[!-~]+$", re.I)  # T3 Code's CloudSpeechApiKey pattern
_OP_NAME = r"[A-Za-z0-9._-](?:[A-Za-z0-9 ._-]*[A-Za-z0-9._-])?"
_OP_REFERENCE = re.compile(rf"^op://{_OP_NAME}(?:/{_OP_NAME}){{2,3}}(?:\?[a-z-]+=[A-Za-z0-9_-]+)?$")
_OP_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def valid_key(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_KEY and bool(_KEY.match(value))


# ---------------------------------------------------------------- T3 Code's saved key


def t3_secret_file(provider: str) -> Path:
    return config.t3_home() / "userdata" / "secrets" / f"voice-cloud-{provider}.bin"


def t3_settings_file() -> Path:
    return config.t3_home() / "userdata" / "settings.json"


def _t3_settings() -> dict:
    try:
        data = json.loads(t3_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _t3_reference(provider: str) -> dict | None:
    creds = _t3_settings().get("cloudSpeechCredentials")
    value = creds.get(provider) if isinstance(creds, dict) else None
    if isinstance(value, dict) and isinstance(value.get("provider"), str) and isinstance(value.get("reference"), str):
        return value
    return None


def t3_key_state(provider: str) -> str | None:
    """"value", "reference" or "missing"; None when T3 Code has no key slot for this provider."""
    if provider not in T3_PROVIDERS:
        return None
    if _t3_reference(provider):
        return "reference"
    return "value" if t3_secret_file(provider).is_file() else "missing"


def _op_read(account: str, reference: str, run=subprocess.run, which=shutil.which) -> str:
    exe = which("op")
    if not exe:
        raise TtsError("unreadable-key", "T3 Code keeps this key in 1Password, and the 1Password CLI (op) "
                                         "is not on this PC's PATH. Install it or save a separate key.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = run([exe, "--cache=false", "read", f"--account={account}", "--no-newline", reference],
                capture_output=True, timeout=OP_TIMEOUT, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    value = r.stdout.decode("utf-8", "replace") if r is not None and r.returncode == 0 else ""
    if not value.strip():  # stderr is never read: keep 1Password's text out of logs
        raise TtsError("unreadable-key", "Couldn't read the key from 1Password. Unlock 1Password and try again.")
    return value


def read_t3_key(provider: str, run=subprocess.run, which=shutil.which) -> str:
    """The key T3 Code saved for this provider, read fresh for each request."""
    name = _NAMES.get(provider, provider)
    if provider not in T3_PROVIDERS:
        raise TtsError("missing-key", f"T3 Code has no {name} key. Save a separate key in Settings > Voice > Read aloud.")
    ref = _t3_reference(provider)
    if ref:
        source = (_t3_settings().get("secretProviders") or {}).get(ref["provider"])
        account = ((source or {}).get("config") or {}).get("account") if isinstance(source, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "one_password":
            raise TtsError("unreadable-key", f"T3 Code keeps the {name} key in a secret provider this add-on cannot read.")
        if not isinstance(account, str) or not _OP_ACCOUNT.match(account) or not _OP_REFERENCE.match(ref["reference"]):
            raise TtsError("unreadable-key", f"T3 Code's 1Password reference for the {name} key is not valid.")
        key = _op_read(account, ref["reference"], run=run, which=which).strip()
    else:
        try:
            key = t3_secret_file(provider).read_bytes().decode("utf-8").strip()
        except FileNotFoundError:
            raise TtsError("missing-key", f"T3 Code has no {name} key. In T3 Code open Settings > Voice and "
                                          f"save one, or save a separate key for read-aloud.") from None
        except (OSError, UnicodeDecodeError):
            raise TtsError("unreadable-key", f"T3 Code's saved {name} key could not be read. Save it again.") from None
    if not valid_key(key):
        raise TtsError("unreadable-key", f"T3 Code's saved {name} key is empty or not a key. Save it again.")
    return key


# ---------------------------------------------------------------- the add-on's own key


class KeyStore:
    """The separate read-aloud keys, one keyring entry per provider. `backend` is the keyring
    module, or any object with get_password, set_password and delete_password (tests)."""

    def __init__(self, backend=None):
        self._backend = backend

    def _kr(self):
        if self._backend is None:
            try:
                import keyring
            except ImportError:
                raise TtsError("unreadable-key", "The keyring package is missing. In the add-on folder run: "
                                                 ".venv\\Scripts\\python -m pip install -r requirements.txt") from None
            self._backend = keyring
        return self._backend

    def name(self) -> str:
        """The key store's name for the settings page, for example "Windows Credential Manager"."""
        try:
            kr = self._kr()
            backend = kr.get_keyring() if hasattr(kr, "get_keyring") else kr
        except Exception:  # noqa: BLE001
            return "the system key store"
        module = type(backend).__module__
        for part, label in ((".Windows", "Windows Credential Manager"), (".macOS", "macOS Keychain"),
                            (".SecretService", "Secret Service"), (".kwallet", "KWallet")):
            if module.endswith(part):
                return label
        return "the system key store"

    @staticmethod
    def _failed(action: str) -> TtsError:
        return TtsError("unreadable-key", f"The system key store could not {action} the key. "
                                          f"On Linux, install and unlock a Secret Service such as GNOME Keyring.")

    def get(self, provider: str) -> str | None:
        try:
            value = self._kr().get_password(SERVICE, provider)
        except TtsError:
            raise
        except Exception:  # noqa: BLE001  # keyring.errors.KeyringError, NoKeyringError, backend errors
            raise self._failed("read") from None
        return value if valid_key(value) else None

    def has(self, provider: str) -> bool:
        try:
            return self.get(provider) is not None
        except TtsError:
            return False

    def set(self, provider: str, key: str) -> None:
        if not valid_key(key):
            raise TtsError("bad-request", "That is not an API key. Paste the key itself, with no spaces, URL or reference.")
        try:
            self._kr().set_password(SERVICE, provider, key)
        except TtsError:
            raise
        except Exception:  # noqa: BLE001
            raise self._failed("save") from None

    def delete(self, provider: str) -> None:
        try:
            self._kr().delete_password(SERVICE, provider)
        except TtsError:
            raise
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "PasswordDeleteError":  # nothing saved: already removed
                return
            raise self._failed("remove") from None


# ---------------------------------------------------------------- the choice


_NAMES = {"openai": "OpenAI", "xai": "xAI", "openrouter": "OpenRouter", "elevenlabs": "ElevenLabs"}


def default_source(provider: str) -> str:
    """"t3" when T3 Code holds a key for this provider, else "own"."""
    return "t3" if t3_key_state(provider) in ("value", "reference") else "own"


def resolve(provider: str, source: str, store: KeyStore) -> str:
    if source == "t3":
        return read_t3_key(provider)
    key = store.get(provider)
    if not key:
        raise TtsError("missing-key", message("missing-key", _NAMES.get(provider, provider)))
    return key


def status(providers: list[str], store: KeyStore) -> dict:
    """For the page: which keys exist. Never the keys."""
    return {p: {"t3": t3_key_state(p), "own": store.has(p)} for p in providers}


def whisper_key(store: KeyStore) -> str | None:
    """An OpenAI key for whisper-1 word timings, for providers without their own: the add-on's own
    OpenAI key first, then T3 Code's. None means estimated timings."""
    for source in ("own", "t3"):
        try:
            return resolve("openai", source, store)
        except TtsError:
            continue
    return None
