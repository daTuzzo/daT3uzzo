# Install daT3uzzo: a procedure for coding agents

You are a coding agent, and the user asked you to install daT3uzzo, a set of add-ons for T3 Code. Follow these steps in order. Report each result to the user in plain words.

## Rules for the whole procedure

1. Never open, read, print, copy or upload any of these. Programs read them, you do not:
   - T3 Code's saved keys: `<T3 home>/userdata/secrets/` (for example `voice-cloud-openai.bin`).
   - The daT3uzzo token: `%LOCALAPPDATA%\dat3uzzo\token` on Windows, `~/.local/share/dat3uzzo/token` elsewhere.
   - The system key store entries of the service `t3-read-aloud`.
2. Never quit, restart or kill T3 Code. The user reloads it with Ctrl+R when you ask.
3. Never run `client apply`, `client restore` or `client rebuild`, and never edit `client-patches.json`, unless the user asks for client patches.
4. If a step fails, stop and show the user the command and its output. Do not work around it.

## 1. Detect the system and T3 Code

Find the operating system first.

**On Windows (PowerShell):**

```powershell
$programs = Join-Path $env:LOCALAPPDATA "Programs\t3code"
Get-ChildItem $programs -Filter "T3 Code*.exe" -ErrorAction SilentlyContinue | Select-Object Name
```

- `T3 Code (Nightly).exe` means the desktop app is T3 Code Nightly.
- `T3 Code (Beta).exe` means the desktop app is T3 Code Stable.
- Nothing found: the desktop app is not installed for this user. Stable and Nightly share this folder, so at most one of them is installed.

**On every system**, find the T3 home and the T3 server versions that browsers use:

```powershell
$t3home = if ($env:T3CODE_HOME) { $env:T3CODE_HOME } else { Join-Path $HOME ".t3" }
Get-ChildItem (Join-Path $t3home "runtime\versions") -Directory -ErrorAction SilentlyContinue | Select-Object Name
```

```bash
t3home="${T3CODE_HOME:-$HOME/.t3}"
ls "$t3home/runtime/versions" 2>/dev/null
```

A version folder name ending in `-nightly.<date>.<n>` is Nightly, any other is Stable. Tell the user what you found, for example: "T3 Code Nightly desktop app, plus server versions 0.1.11 and 0.1.12".

If there is no desktop app on Windows and no version folder, T3 Code is not installed here. Stop and tell the user.

On macOS and Linux, daT3uzzo patches only the page that the T3 server sends to browsers. The desktop window there is not patched. Tell the user this before you go on.

## 2. Check Python

daT3uzzo needs Python 3.11 or later.

- Windows: `py -3 --version`. If `py` is missing, try `python --version`.
- macOS and Linux: `python3 --version`.

If the version is older than 3.11 or Python is missing, stop and ask the user to install Python 3.11 or later from python.org (on Windows, with the "py launcher" option).

Git must be installed too: `git --version`.

## 3. Clone and install

Pick a folder the user agrees to, for example `C:\src` on Windows or `~/src` elsewhere.

**Windows:**

```powershell
git clone https://github.com/daTuzzo/daT3uzzo C:\src\daT3uzzo
cd C:\src\daT3uzzo
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m dat3uzzo install
```

**macOS and Linux:**

```bash
git clone https://github.com/daTuzzo/daT3uzzo ~/src/daT3uzzo
cd ~/src/daT3uzzo
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m dat3uzzo install
```

`install` patches T3 Code's page, adds the Windows Startup shortcut and the two desktop switch shortcuts (Windows only), and starts the helper. Running it again is safe.

On macOS and Linux there is no autostart. Tell the user to start the helper at login with `<folder>/.venv/bin/python -m dat3uzzo serve`, for example from their desktop's autostart settings.

## 4. Verify

Run `status` from the daT3uzzo folder (`.venv\Scripts\python -m dat3uzzo status` on Windows, `.venv/bin/python -m dat3uzzo status` elsewhere). Check these lines:

- `helper:     running, pid <n> (port 47613)`
- `desktop:    Nightly <version> in ...: patched` or `Stable ...: patched` (Windows desktop app only)
- `server:     <version>: patched` for each version folder
- `key openai:     T3 Code value` or `T3 Code reference`. `missing` means the user has no OpenAI key saved in T3 Code yet.

Then check the helper over HTTP:

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:47613/health).StatusCode
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:47613/health
```

It must print `200`. The JSON body has `"name": "dat3uzzo"`. It holds no key and no token.

Then tell the user:

1. Press **Ctrl+R** in T3 Code once. A speaker button appears after Copy and Reply on each finished reply.
2. If `status` said `missing` for the OpenAI key: open **Settings > Voice** in T3 Code, choose OpenAI and save the key there. Read-aloud uses that key. Another voice service, or a separate key, goes in **Settings > Voice > Read aloud**.
3. The switches are in **Settings > General > daT3uzzo**.

Do not test read-aloud yourself: each spoken reply costs the user money.

## 5. Update

```powershell
git -C C:\src\daT3uzzo pull --ff-only
C:\src\daT3uzzo\.venv\Scripts\python -m pip install -r C:\src\daT3uzzo\requirements.txt
cd C:\src\daT3uzzo
.venv\Scripts\python -m dat3uzzo install
```

`install` restarts nothing that already runs. To load new helper code, also run `.venv\Scripts\python -m dat3uzzo restart`. It restarts the helper only, not T3 Code. Then ask the user to press Ctrl+R in T3 Code.

## 6. Recover: the off switch

If T3 Code's page breaks after an install or an update, turn every add-on off. Any one of these works, and none needs T3 Code's page:

- `.venv\Scripts\python -m dat3uzzo ui off` in the daT3uzzo folder.
- The **daT3uzzo off** shortcut on the Windows desktop.
- Edit `%LOCALAPPDATA%\dat3uzzo\ui.json` (`~/.local/share/dat3uzzo/ui.json` elsewhere) so it says `"enabled": false`.

Then ask the user to press Ctrl+R in T3 Code. `ui status` lists each add-on, and `ui mod <id> off` turns off one of them. `ui on` turns everything back on.

## 7. Uninstall

```powershell
cd C:\src\daT3uzzo
.venv\Scripts\python -m dat3uzzo uninstall
```

It stops the helper, puts back T3 Code's original page files, and removes the shortcuts. It asks before it deletes the saved audio. Add `--keep-cache` to keep it without a question. If it says that a client patch is still in T3 Code, ask the user to close T3 Code, then run `uninstall` again. Then ask the user to press Ctrl+R in T3 Code, and delete the daT3uzzo folder if they want.
