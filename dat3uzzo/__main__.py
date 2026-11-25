"""python -m dat3uzzo [serve|install|uninstall [--keep-cache]|status|patch|unpatch]
python -m dat3uzzo ui on|off|status       the daT3uzzo master switch
python -m dat3uzzo ui mod <id> on|off     one add-on's switch
python -m dat3uzzo allow-origin <url>     let a T3 Code page on another origin load the add-ons
python -m dat3uzzo deny-origin <url>      take that back
python -m dat3uzzo origins                the allowed origins, the userscript and the extension
python -m dat3uzzo extension              write the browser extension folder
python -m dat3uzzo client status|rebuild|apply|restore    client patches (debugging; the helper does it all)
python -m dat3uzzo restart                the helper itself: stop it, start it outside any process tree"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import audio_cache, client_patch, config, desktop, migrate, origins, patcher, ui
from .tts import keys as tts_keys

STARTUP_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/daT3uzzo.lnk"
UI_LINKS = {"off": f"{ui.NAME} off.lnk", "on": f"{ui.NAME} on.lnk"}


def health() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}/health", timeout=1.5) as r:
            data = json.loads(r.read())
            return data if data.get("name") == config.APP_ID else None
    except (OSError, ValueError):
        return None


def pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def start_detached() -> bool:
    """Start the helper outside the caller's process tree and job object.

    A shell that T3 Code started (an agent's terminal) runs inside T3 Code's job object, and a plain
    child of it would die when T3 Code quits, just when the client patch autopilot must swap. So on
    Windows, Explorer opens the Startup shortcut, as at logon. Without the shortcut, WMI's
    Win32_Process.Create starts pythonw; WMI's provider host is its parent."""
    if os.name == "nt":
        no_window = subprocess.CREATE_NO_WINDOW
        if STARTUP_LINK.exists():
            subprocess.Popen(["explorer.exe", str(STARTUP_LINK)], creationflags=no_window)
        else:
            line = f'"{pythonw()}" -m dat3uzzo serve'
            script = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                      f"@{{CommandLine={_ps_quote(line)}; CurrentDirectory={_ps_quote(str(config.PROJECT_ROOT))}}}"
                      " | Out-Null")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           check=False, creationflags=no_window)
    else:
        subprocess.Popen([str(pythonw()), "-m", "dat3uzzo", "serve"], cwd=str(config.PROJECT_ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
    for _ in range(60):
        time.sleep(0.25)
        if health():
            return True
    return False


def stop() -> bool:
    if not health():
        return True
    try:
        urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}/shutdown?token={config.token()}", timeout=2).read()
    except OSError:
        pass
    for _ in range(20):
        time.sleep(0.25)
        if not health():
            return True
    return False


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def add_autostart() -> None:
    STARTUP_LINK.parent.mkdir(parents=True, exist_ok=True)
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(STARTUP_LINK))});"
        f"$s.TargetPath={_ps_quote(str(pythonw()))};$s.Arguments='-m dat3uzzo serve';"
        f"$s.WorkingDirectory={_ps_quote(str(config.PROJECT_ROOT))};$s.WindowStyle=7;"
        "$s.Description='daT3uzzo helper for T3 Code add-ons';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def remove_autostart() -> None:
    STARTUP_LINK.unlink(missing_ok=True)


def desktop_dir() -> Path:
    if os.name == "nt":  # the real Desktop folder, also when OneDrive moved it
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    return Path.home() / "Desktop"


def add_ui_shortcuts() -> list[Path]:
    """Desktop shortcuts that flip the master switch in a console window that closes after 3 s."""
    exe = Path(sys.executable).with_name("python.exe")
    exe = exe if exe.exists() else Path(sys.executable)
    links = [desktop_dir() / name for name in UI_LINKS.values()]
    script = "".join(
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(link))});"
        f"$s.TargetPath={_ps_quote(str(exe))};$s.Arguments='-m dat3uzzo ui {word} --hold 3';"
        f"$s.WorkingDirectory={_ps_quote(str(config.PROJECT_ROOT))};$s.WindowStyle=1;"
        f"$s.Description={_ps_quote(f'Turn {ui.NAME} {word} (T3 Code add-ons), then press Ctrl+R in T3 Code')};$s.Save();"
        for word, link in zip(UI_LINKS, links)
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)
    return links


def remove_ui_shortcuts() -> None:
    for name in UI_LINKS.values():
        (desktop_dir() / name).unlink(missing_ok=True)


def cmd_serve(_args) -> int:
    if health():
        return 0  # one helper per machine
    from . import server
    server.setup_logging()
    try:
        asyncio.run(server.run())
    except OSError as e:  # port taken by something else
        server.log.error("cannot listen on %s:%s: %s", config.HOST, config.PORT, e)
        return 1
    return 0


def cmd_status(_args) -> int:
    h = health()
    print(f"helper:     {'running, pid ' + str(h['pid']) if h else 'not running'} (port {config.PORT})")
    print(f"preset:     OpenAI Realtime, {config.MODEL}, T3 Code's OpenAI key (the page can choose another)")
    store = tts_keys.KeyStore()
    for provider in ("openai", "xai", "openrouter", "elevenlabs"):
        t3 = tts_keys.t3_key_state(provider)
        own = "saved" if store.has(provider) else "none"
        print(f"key {provider + ':':12}T3 Code {t3 or 'n/a'}, separate key {own}")
    saved = audio_cache.stats()
    print(f"saved audio: {saved['bytes'] / 1e6:.0f} MB of {saved['cap'] / 1e6:.0f} MB, {saved['pieces']} pieces")
    print(f"autostart:  {'yes' if STARTUP_LINK.exists() else 'no'}")
    if migrate.pending():
        print("migration:  parts of the first release (t3-read-aloud) are left; run install again")
    block = patcher.loader_block(config.PORT, config.token())
    for asar in desktop.find_asars():
        version = client_patch.archive_version(asar) or "unknown version"
        print(f"desktop:    {desktop.channel(asar)} {version} in {asar.parent.parent}: {desktop.state(asar, block)}")
    if os.name != "nt":
        print(f"desktop:    {desktop.NOT_PATCHED}")
    allowed = origins.load()
    print(f"origins:    {len(allowed)} allowed{': ' + ', '.join(allowed) if allowed else ''}")
    for path in patcher.index_files(config.t3_home()):
        print(f"server:     {path.parent.parent.name}: {desktop.page_state(path.read_text(encoding='utf-8'), block)}")
    print(f"log:        {config.data_dir() / 'dat3uzzo.log'}")
    return 0


def print_ui(view: dict) -> None:
    print(f"{ui.NAME} is {'on' if view['enabled'] else 'off'}")
    for m in view["mods"]:
        state = "on" if m["on"] else "off"
        if m["on"] and not view["enabled"]:
            state = f"on, but inactive while {ui.NAME} is off"
        print(f"  {m['id']}: {state}  [{' + '.join(m['kinds'])}] {m['label']}")
    print(f"switches: {ui.state_file()}")


def cmd_ui(args) -> int:
    words = args.args
    if words in (["on"], ["off"]):
        query = {"enabled": words}
    elif len(words) == 3 and words[0] == "mod":
        query = {"mod": [words[1]], "on": [words[2]]}
    elif words in ([], ["status"]):
        query = {}
    else:
        print("usage: python -m dat3uzzo ui on|off|status, or ui mod <id> on|off", file=sys.stderr)
        return 2
    try:
        try:
            view = ui.apply_query(query)
        except ValueError as e:
            print(f"{e}. Add-ons: {', '.join(m.id for m in ui.find_mods())}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"could not save {ui.state_file()}: {e}", file=sys.stderr)
            return 1
        print_ui(view)
        if query:
            print("Press Ctrl+R in T3 Code.")
        return 0
    finally:
        if args.hold > 0:  # the desktop shortcuts: keep the console open long enough to read it
            print(f"This window closes in {args.hold:g} seconds.", flush=True)
            time.sleep(args.hold)


def print_origins() -> None:
    allowed = origins.load()
    print(f"Allowed origins ({len(allowed)}):" if allowed else "Allowed origins: none yet")
    for origin in allowed:
        print(f"  {origin}")
    print(f"userscript: http://127.0.0.1:{config.PORT}{origins.USERSCRIPT_PATH}"
          " (open it in a browser with Tampermonkey or Violentmonkey)")
    folder = origins.extension_dir()
    if folder.exists():
        print(f"extension:  {folder} (after a change, press Reload on it in chrome://extensions)")
    else:
        print("extension:  run `python -m dat3uzzo extension` to write it")


def cmd_origin(args) -> int:
    if len(args.args) != 1:
        print(f"usage: python -m dat3uzzo {args.command} <url>, for example https://app.t3.codes",
              file=sys.stderr)
        return 2
    change = origins.allow if args.command == "allow-origin" else origins.deny
    try:
        origin, changed = change(args.args[0])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except OSError as e:
        print(f"could not save {origins.state_file()}: {e}", file=sys.stderr)
        return 1
    adding = args.command == "allow-origin"
    if changed:
        print(f"{'allowed' if adding else 'removed'}: {origin}")
    else:
        print(f"no change: {origin} {'is already allowed' if adding else 'was not allowed'}")
    if changed and origins.extension_dir().exists():
        origins.write_extension(config.PORT)
    print_origins()
    if changed:
        print("The helper uses the new list at once. Update the userscript or reload the extension, then reload the page.")
    return 0


def cmd_origins(_args) -> int:
    print_origins()
    return 0


def cmd_client(args) -> int:
    return client_patch.cmd(args.args, args.hold)


def cmd_restart(_args) -> int:
    """Stop the helper and start it again outside the caller's process tree. T3 Code is not touched."""
    print("helper: " + ("stopped" if stop() else "did not stop; end it in Task Manager"))
    if start_detached():
        h = health()
        print(f"helper: started, pid {h['pid'] if h else '?'}")
        return 0
    print("helper: did not start; see the log")
    return 1


def cmd_extension(_args) -> int:
    folder = origins.write_extension(config.PORT)
    print(f"extension: {folder}")
    print("Load it once: chrome://extensions (or edge://extensions), turn on Developer mode, Load unpacked, pick that folder.")
    print_origins()
    return 0


def cmd_patch(_args) -> int:
    block = patcher.loader_block(config.PORT, config.token())
    for asar in desktop.find_asars():
        print(f"desktop {asar.parent.parent.name}: {desktop.patch_asar(asar, block)}")
    for version, outcome in patcher.patch_all(config.t3_home(), block).items():
        print(f"server {version}: {outcome}")
    return 0


def cmd_unpatch(_args) -> int:
    for asar in desktop.find_asars():
        print(f"desktop {asar.parent.parent.name}: {desktop.unpatch_asar(asar)}")
    for version, outcome in patcher.unpatch_all(config.t3_home()).items():
        print(f"server {version}: {outcome}")
    return 0


def cmd_install(args) -> int:
    try:
        kept = migrate.run()  # first: it moves the old token, which the patch step reads
    except (migrate.MigrationFailed, OSError) as e:
        print(f"migration: stopped: {e}", file=sys.stderr)
        return 1
    cmd_patch(args)
    if os.name == "nt":
        add_autostart()
        print(f"autostart: {STARTUP_LINK}")
        for link in add_ui_shortcuts():
            print(f"shortcut:  {link}")
    else:  # the Startup folder and .lnk shortcuts are Windows only
        print("autostart: Windows only. Start the helper at login with: python -m dat3uzzo serve")
    if health():
        print("helper: already running")
    elif start_detached():
        print("helper: started")
    else:
        print("helper: did not start; see the log")
        return 1
    print("Reload T3 Code (Ctrl+R) once. Updates are patched automatically from now on.")
    if kept:
        print(f"migration: {len(kept)} old entries stayed in {migrate.old_dir()}; see the lines above")
    return 0


def cmd_uninstall(args) -> int:
    try:
        migrate.run()  # an old helper, its state and its shortcut go too
    except (migrate.MigrationFailed, OSError) as e:
        print(f"migration: stopped: {e}", file=sys.stderr)
        return 1
    print("helper: " + ("stopped" if stop() else "still running; end it in Task Manager"))
    # Stopped first, so its autopilot cannot swap the patch back in. A refusal leaves everything else
    # installed, and a second run finishes the job.
    if not client_patch.restore_for_uninstall():
        return 1
    if os.name == "nt":
        remove_autostart()
        print("autostart: removed")
        remove_ui_shortcuts()
        print(f"shortcuts: removed {', '.join(UI_LINKS.values())}")
    cmd_unpatch(args)
    remove_cache(args.keep_cache)
    print("Reload T3 Code (Ctrl+R) to drop the buttons.")
    return 0


def remove_cache(keep: bool) -> None:
    """Saved audio goes only when the person agrees: --keep-cache keeps it, a console asks, and a run
    with no console keeps it."""
    folder = config.data_dir() / "cache"
    if not folder.exists():
        return
    size = audio_cache.stats()["bytes"]
    if keep:
        print(f"saved audio: kept ({size / 1e6:.0f} MB in {folder})")
        return
    answer = ""
    if sys.stdin and sys.stdin.isatty():
        answer = input(f"Delete the saved audio ({size / 1e6:.0f} MB in {folder})? [y/N] ").strip().lower()
    if answer in ("y", "yes"):
        audio_cache.delete_all()
        print("saved audio: deleted")
    else:
        print(f"saved audio: kept ({folder})")


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m dat3uzzo")
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "install", "uninstall", "status", "patch", "unpatch", "ui",
                             "allow-origin", "deny-origin", "origins", "extension", "client", "restart"])
    ap.add_argument("args", nargs="*", help="for ui: on, off, status, or mod <id> on|off; "
                                            "for allow-origin and deny-origin: the page's URL; "
                                            "for client: status, rebuild, apply or restore")
    ap.add_argument("--hold", type=float, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--keep-cache", action="store_true", help="for uninstall: keep the saved audio")
    args = ap.parse_args()
    if args.args and args.command not in ("ui", "allow-origin", "deny-origin", "client"):
        ap.error(f"{args.command} takes no arguments")
    if args.command not in ("install", "uninstall") and migrate.first_run():
        # Any other command would create new state files (a token, client-patches.json) that the
        # migration would then keep instead of the old ones.
        print(f"The first release's state is in {migrate.old_dir()}. Run `python -m dat3uzzo install` "
              "first: it moves it.", file=sys.stderr)
        return 1
    return {
        "serve": cmd_serve, "install": cmd_install, "uninstall": cmd_uninstall,
        "status": cmd_status, "patch": cmd_patch, "unpatch": cmd_unpatch, "ui": cmd_ui,
        "allow-origin": cmd_origin, "deny-origin": cmd_origin, "origins": cmd_origins,
        "extension": cmd_extension, "client": cmd_client, "restart": cmd_restart,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
