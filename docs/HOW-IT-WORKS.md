# How daT3uzzo works

This page holds the details behind the [README](../README.md): how the add-ons survive T3 Code updates, how client patches are built and swapped, how a T3 server on another machine loads the add-ons, and the design decisions with the options that were rejected.

## The parts

- **The helper**, `python -m dat3uzzo serve`: a Python process on `127.0.0.1:47613`. It serves the add-on bundle at `/dat3uzzo.js`, speaks replies on `/speak`, answers settings on `/settings`, flips switches on `/ui`, and keeps T3 Code patched.
- **The loader block** in T3 Code's page, between `<!-- dat3uzzo:start -->` and `<!-- dat3uzzo:end -->`. It sets `window.__DAT3UZZO__` (the port and the token), fetches `/dat3uzzo.js` and runs it as an inline script. The desktop window's Content-Security-Policy allows inline scripts and `http:` connections, but no script files from other origins. If the helper is down, the fetch fails quietly, retries for about 20 minutes, and T3 Code works as before.
- **The bundle**: the switches script (`dat3uzzo/ui_control.js`) first, then each enabled `web/*.js` add-on as its own `<script>` element in its own error guard.
- **The state folder**, `%LOCALAPPDATA%\dat3uzzo\` (`~/.local/share/dat3uzzo` elsewhere): `token`, `ui.json` (the switches), `origins.json`, `backups\`, `cache\` (saved audio), `client-patches.json`, `client-patch.json`, `client-patch\` (staged archives) and `dat3uzzo.log`.

## How it survives updates

T3 Code has two copies of its page, and the helper patches both:

- **The desktop window** serves `apps/server/dist/client/index.html` from inside `%LOCALAPPDATA%\Programs\t3code\resources\server.asar`.
  - The helper edits that one file in place, at its exact byte size, so nothing else in the archive moves. It makes room by removing full-line comments from the page's inline startup script, then pads with spaces.
  - It first backs up the original page to `backups\<sha256>.html`. The archive's header keeps the original SHA-256, so `unpatch` can always restore the page exactly.
  - It refuses to patch when the app's Electron fuse `EnableEmbeddedAsarIntegrityValidation` is on, or when the page differs from the archive's own record of it.
- **Browsers** get the page from the T3 server, which reads `<T3 home>/runtime/versions/<version>/client/index.html` from disk on every request. The T3 home is `~/.t3`, or `$T3CODE_HOME` when it is set, the same rule T3 Code uses for Stable and Nightly.

The helper checks both places every second. A desktop update replaces `server.asar`, and a server update adds a version folder. Either way the helper adds the loader block again within about a second. Right after a desktop update, the first window may open before the patch lands, so press Ctrl+R once.

The read-aloud add-on finds replies through markers that T3 Code uses itself: `data-message-id`, `data-timeline-row-kind`, `data-assistant-citation-source`, and the Copy button's label. If a future T3 Code version renames them, the speaker button disappears and T3 Code still works. Check with `status` and the log.

### Stable and Nightly on Windows

T3 Code's packaging (`scripts/build-desktop-artifact.ts` in its repo) names the Nightly app "T3 Code (Nightly)" and the Stable app "T3 Code (Beta)". Both builds have the app id `com.t3tools.t3code`, and the per-user installer names the install folder after the app's package name, `t3code`. So both install to `%LOCALAPPDATA%\Programs\t3code`, one at a time. `status` prints which one it found. Both use the same T3 home.

## Read-aloud in detail

### How highlighting gets its timings

1. The reply is spoken in small pieces: first one sentence, then about 450 characters each.
2. When a piece finishes, the helper spreads the model's own transcript over the piece's length. This instant estimate is sent first.
3. The helper sends the piece's audio to OpenAI speech-to-text (`whisper-1`) with word timestamps. The heard words replace the estimate a second or two later.
4. `dat3uzzo/timing.py` aligns heard words to the words on screen. Both sides spell numbers the same way, so "92%" and "ninety-two percent" match.

xAI and ElevenLabs send their own character timings with the audio, so they need no speech-to-text. OpenRouter uses speech-to-text when an OpenAI key is saved, else the estimate stays. The highlight costs 0.55 ms per animation frame on average on the stand-in page (5.1 ms at most, when the word changes). `window.__T3RA_API__.perf()` reports the live numbers.

### Saved audio

- Each spoken piece is saved in `cache\`. Its key is a SHA-256 of the service, model, voice, mode, prompt version, cleanup version and the exact text. Speed is not part of it: the player sets speed.
- Playing a reply again streams it from disk: no voice API call, no speech-to-text call, and no key needed. A reply that stopped halfway plays its saved pieces and makes only the missing ones. A saved 30-minute reply starts to play 0.24 s after the request (`eval/cache_replay_timing.py`, fake pieces, no cost).
- A piece is saved only after it is complete, through a temporary file that replaces the target in one step.
- The pieces are PCM, about 2.9 MB per audio minute, because joined MP3 pieces would shift the word highlight. The oldest go first when the total passes the cap (1 GB by default, about 5.8 hours).
- When the read-aloud switch is off, the helper refuses `/speak` and `/settings`, so the cache is not used.

## Client patches

A client patch is a T3 Code commit that changes T3 Code's own page files and that upstream has not merged yet. `client-patches.json` lists them:

- `id` is the daT3uzzo add-on whose switch controls the patch. Each entry is one row in Settings > General > daT3uzzo, of kind "client patch".
- `repo` is the folder of a git clone of `pingdotgg/t3code`.
- `commits` lists the SHAs to cherry-pick, in order. `remote` is where to fetch a commit that the clone does not have.
- Instead of `commits`, an entry can name a branch: `"branch": "feat/x", "remote": "fork", "base": "origin/main"`. The helper then cherry-picks every commit on `fork/feat/x` after `origin/main`.

### What the helper does, every 5 seconds

1. **No staged archive for the installed version** (a new T3 Code version, or a changed config): it builds one in the background, at idle priority, with no window (`dat3uzzo/client_build.py`):
   - It fetches the tag `v<version>` and adds a temporary worktree under the state folder, outside the clone's own working tree.
   - It cherry-picks the patch commits.
   - It installs the dependencies with the release job's filters, then runs `scripts/update-release-package-versions.ts <version>`.
   - It runs a marker build, reads the release's public config values from the original client (in memory only, never in a file or the log), then runs the real build.
   - It copies `apps/web/dist` without `*.map` files and hidden entries, applies the nightly or production brand, keeps the original's `third-party-licenses.json`, then packs and checks the archive. Every server file must be identical and the unpacked set the same.
   - It removes the worktree, also after a failure. The log is `client-patch-build.log`.
2. **A staged archive is ready and T3 Code runs**: Settings shows "Ready for T3 Code <version>. It applies at the next T3 Code restart."
3. **A staged archive is ready and no T3 Code process has run for 8 seconds**: it backs up the original to `backups\server-<version>-<sha12>.asar` (never overwritten), then swaps the staged archive in. The updater's processes count as T3 Code. After a plain exit, it does not open T3 Code.
4. **The patch's switch or the master switch is off**: it puts the original back the same way, at the next exit.
5. **A failed build or swap**: T3 Code keeps its original page files, and Settings shows the reason. A failed build is tried again after 6 hours, or at once for a new version or a changed config.

The helper records each original's and each staged archive's client folder in `client-patch.json`. So it never backs up a patched archive as an original, and it leaves an archive it did not make alone.

### Restart T3 now

While a swap waits, the Settings row shows a **Restart T3 now** button. A script can do the same with `GET http://127.0.0.1:47613/client/restart?token=<install token>`: `202` when the restart starts, `409` when nothing waits, `403` without the token. `GET /client/state?token=...` returns each patch's status.

T3 Code's page has no quit call. So the helper does what the window's X button does: it posts `WM_CLOSE` to each visible top-level window of T3 Code's processes. On Windows, Electron's `window-all-closed` then calls `app.quit()`, and T3 Code's `before-quit` handler runs its full shutdown. The helper waits up to 2 minutes for the exit and never forces it. Then it swaps and opens T3 Code through Explorer.

### The helper lives outside T3 Code

A process that T3 Code starts, for example an agent's shell, runs in T3 Code's job object and dies when T3 Code quits. The helper must outlive T3 Code to swap at the exit. So `install` and `restart` start it through Explorer with the Startup shortcut, as at logon, or through WMI when the shortcut is missing.

### Commands for debugging

- `client status`: each patch's status, the installed version and kind, the staged archive, the backups, the last build.
- `client rebuild`: build the staged archive for the installed version now, in the foreground.
- `client apply` and `client restore`: wait until T3 Code closes, then swap. They do not open T3 Code.
- `restart`: restart the helper itself. T3 Code is not touched.

## T3 Code on another machine

A T3 server on another machine (a dev box, a VPS, the hosted app) serves its own page, and the helper never changes a remote server. So a browser add-on loads the add-ons there, and the helper must allow that page's origin first.

1. Allow the page's origin (the scheme, host and port you open in the browser):
   ```
   .venv\Scripts\python -m dat3uzzo allow-origin https://devbox.example:3773
   ```
   For a server you reach through an SSH port forward, allow the local address, for example `http://localhost:4000`.
2. Install one of these in Chrome, Edge or another Chromium browser:
   - **Userscript**: with Tampermonkey or Violentmonkey installed, open `http://127.0.0.1:47613/dat3uzzo.user.js`. After `allow-origin` or `deny-origin`, update the script in the manager.
   - **Extension**: run `python -m dat3uzzo extension`. Open `chrome://extensions`, turn on Developer mode, click Load unpacked and pick `%LOCALAPPDATA%\dat3uzzo\browser-extension`. After `allow-origin` or `deny-origin`, click Reload on it.
3. Open the T3 page. Chrome and Edge ask once whether the site may access apps and services on this device. Click Allow.

How it works:
- The userscript and the extension fetch the bundle from `http://127.0.0.1:47613` with the page's own `Origin` header, which a page cannot fake.
- Only an allowed origin gets the token, inside the bundle. Browsers count loopback addresses as secure, so an https page can reach `http://127.0.0.1` with no mixed-content block.
- An http page from another machine is not confirmed: in the test, headless Chrome blocked it and also refused the permission prompt. Serve the remote T3 server over https, or use an SSH port forward to `localhost`.
- The desktop app needs none of this. Its window runs on this PC and is already patched, also for threads on a remote environment. That case was not tested.

### The Origin check

Every request, WebSockets included, first passes the check in `dat3uzzo/origins.py`:
- No `Origin` header: a program on this PC, or the desktop window, which removes its own `Origin` from its requests. Served with no token.
- The desktop origin `t3code://app` or a loopback origin (`127.0.0.1`, `localhost`, `::1`): served with no token. These pages carry the token from the patch.
- An origin in `origins.json`: served with the token in the bundle, plus CORS headers and `Access-Control-Allow-Private-Network: true`.
- Any other origin, `null` included: 403 with no CORS headers. The log records it at most once per 10 minutes for each path.

Allow only origins you trust. Any script on an allowed origin can make the helper speak with your keys and change your read-aloud settings. It cannot read a key.

## Moving from the first release

The first release of this project was called t3-read-aloud. `install` moves such an install by itself: it stops the old helper with its own token, moves its state folder to `%LOCALAPPDATA%\dat3uzzo` (the token, the switches, the backups and the client patch records), replaces each old loader block with the new one at the same byte size, and removes the old Startup shortcut. A backup of an original `server.asar` stays recognised as the original, because the client folder that identifies it does not include `index.html`. `tests/test_migrate.py` covers it.

## Decisions and rejected options

- **Add-ons with a local helper**: chosen. It keeps working across every T3 Code update without a rebuild, and each add-on has an off switch.
- **A fork build of T3 Code**: rejected. It leaves the official update channel.
- **`gpt-realtime-2.1-mini`**: rejected. In the test it misread digits in 3 of 5 replies and gave table rows the wrong values ([test-results.md](test-results.md)).
- **The model converts the numbers**: rejected. The full model read one group of an IP address as the wrong number. Code now spells all numbers first.
- **The model's transcript as a drift check**: rejected. In the test, the transcript was correct while the audio said wrong facts.
- **OpenAI's older speech endpoint**: rejected. It needs no helper, but OpenAI shuts it down on 6 Jan 2027, and it has no cleanup step.
- **Remote T3 pages through a userscript or a Manifest V3 extension, with an origin allow-list in the helper**: chosen. The helper decides by the browser's `Origin` header, and it never changes a remote server.
- **The `origins=` list built into `websockets.serve`**: rejected. It checks WebSocket handshakes only and reads its list once at start.
- **A userscript with `GM_xmlhttpRequest`**: rejected. That request comes from the extension, so the helper could not see which page asked.
- **The token pasted into the userscript**: rejected. A copy would sit in the browser's extension storage, and a removed origin would keep it.
- **Every loopback origin gets the token**: rejected. Any local dev server page could then take it.
- **Patching the macOS app, the Linux AppImage or the deb**: rejected. macOS seals `app.asar` with the app's code signature, the AppImage is read-only, and the deb is owned by root.
- **Client patches on by default**: rejected. A build needs Git, Node.js and a clone of T3 Code's repo, so a new install starts with none.
