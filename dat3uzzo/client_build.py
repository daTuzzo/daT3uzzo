"""Build the staged archive of the client patches for one T3 Code version, the way T3 Code's release
builds its web client (.github/workflows/release.yml, job build_bundle, and
scripts/build-desktop-artifact.ts). The helper's autopilot calls build() in a background thread when
the installed version has no staged archive. Every step runs at idle priority with no console
window, and writes its output to %LOCALAPPDATA%\\dat3uzzo\\client-patch-build.log.

 1. Fetch the tag v<version> into the T3 Code clone if it is missing, and any missing patch commit.
 2. Add a temporary worktree at the tag, outside the clone, and cherry-pick the patch commits.
 3. vp install with the release job's filters, with the vite-plus version from pnpm-workspace.yaml.
 4. scripts/update-release-package-versions.ts <version>.
 5. A marker build, to find where the release's public config values sit in the built client. The
    values are then read from the original client (they are public: every install ships them) and
    kept in memory only, never in a file or the log.
 6. The real build with those values.
 7. The client folder: no *.map files and no hidden entries (the release's artifact upload and its
    archive packer skip them), the nightly or production brand, and the original's
    third-party-licenses.json (a Windows install lists other platform packages).
 8. client_patch.stage(): pack, check against the original, add the loader block, write the manifest.
 9. Remove the worktree, also after a failure.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import client_patch, config

IDLE = 0x00000040  # IDLE_PRIORITY_CLASS; child processes inherit it
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: the helper has no console, so no window pops up
STEP_TIMEOUT = 1800
FILTERS = ["--filter=t3...", "--filter=@t3tools/web...", "--filter=@t3tools/desktop...", "--filter=@t3tools/scripts..."]
WEB_BUILD = ["vp", "run", "--filter", "@t3tools/web", "build"]
MARKERS = {
    "T3CODE_CLERK_PUBLISHABLE_KEY": "T3RAMARKclerkpk",
    "T3CODE_CLERK_JWT_TEMPLATE": "T3RAMARKjwt",
    "T3CODE_CLERK_CLI_OAUTH_CLIENT_ID": "T3RAMARKcli",
    "T3CODE_RELAY_URL": "https://t3ramarkrelay.example.com",
    "T3CODE_RELAY_CLIENT_OTLP_TRACES_URL": "https://t3ramarktraces.example.com/v1/traces",
    "T3CODE_RELAY_CLIENT_OTLP_TRACES_DATASET": "T3RAMARKdataset",
    "T3CODE_RELAY_CLIENT_OTLP_TRACES_TOKEN": "T3RAMARKtoken",
}


class BuildFailed(Exception):
    """A build step failed; the message is the reason shown in Settings."""


def _flags() -> int:
    return IDLE | NO_WINDOW if os.name == "nt" else 0


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise BuildFailed(f"{name} is not on the helper's PATH")
    return found


class Runner:
    """Runs one step after another, with every command and its output in the build log."""

    def __init__(self, log_path: Path, out: Callable[[str], None]):
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            os.replace(log_path, log_path.with_name(log_path.name + ".1"))
        self.log = log_path.open("a", encoding="utf-8", errors="replace")
        self.out = out

    def note(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log.write(f"[{stamp}] {text}\n")
        self.log.flush()
        self.out(text)

    def run(self, label: str, args: list[str], cwd: Path | None = None, env: dict | None = None,
            timeout: int = STEP_TIMEOUT, check: bool = True) -> int:
        started = time.monotonic()
        self.note(f"{label}: {' '.join(args)}")  # arguments only, never the environment
        try:
            done = subprocess.run(args, cwd=str(cwd) if cwd else None, env=env, stdin=subprocess.DEVNULL,
                                  stdout=self.log, stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=_flags())
        except FileNotFoundError:
            raise BuildFailed(f"{label}: {args[0]} was not found") from None
        except subprocess.TimeoutExpired:
            raise BuildFailed(f"{label} took longer than {timeout // 60} minutes") from None
        self.note(f"{label}: exit {done.returncode} after {time.monotonic() - started:.0f} s")
        if check and done.returncode != 0:
            raise BuildFailed(f"{label} failed (exit {done.returncode}); see {self.log.name}")
        return done.returncode

    def output(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        done = subprocess.run(args, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace", creationflags=_flags())
        return done.returncode, done.stdout.strip()

    def close(self) -> None:
        self.log.close()


def is_nightly(version: str) -> bool:
    """The same test as resolveDesktopUpdateChannel in scripts/build-desktop-artifact.ts."""
    return bool(re.search(r"-nightly\.\d{8}\.\d+$", version))


def vite_plus_version(worktree: Path) -> str:
    """The vite-plus version CI uses: the catalog line in pnpm-workspace.yaml (setup-ci-deps action)."""
    text = (worktree / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    m = re.search(r"(?m)^  vite-plus: ([0-9][^ \r\n]*)$", text)
    if not m:
        raise BuildFailed("pnpm-workspace.yaml has no vite-plus catalog version")
    return m.group(1)


def build_env(version: str, extra: dict) -> dict:
    """The helper's environment without any T3 Code or Vite setting of its own, plus the release's."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("VITE_", "T3CODE_", "EXPO_PUBLIC_")) and k.upper() != "NODE_ENV"}
    env.update({"GITHUB_REPOSITORY": "pingdotgg/t3code",
                "T3CODE_TELEMETRY_CHANNEL": "nightly" if is_nightly(version) else "stable"})
    env.update(extra)
    return env


def resolve_commits(r: Runner, repo: Path, git: str, patches: list[dict]) -> list[tuple[str, list[str]]]:
    out = []
    for p in patches:
        remote = p.get("remote", "fork")
        if p.get("commits"):
            shas = []
            for c in p["commits"]:
                code, full = r.output([git, "-C", str(repo), "rev-parse", "-q", "--verify", f"{c}^{{commit}}"])
                if code != 0:
                    r.run(f"fetch {remote}", [git, "-C", str(repo), "fetch", "--no-tags", remote])
                    code, full = r.output([git, "-C", str(repo), "rev-parse", "-q", "--verify", f"{c}^{{commit}}"])
                if code != 0:
                    raise BuildFailed(f"{p['id']}: commit {c[:10]} is not in the clone or on {remote}")
                shas.append(full)
        else:
            r.run(f"fetch {remote}/{p['branch']}", [git, "-C", str(repo), "fetch", "--no-tags", remote, p["branch"]])
            base = p.get("base", "origin/main")
            code, listing = r.output([git, "-C", str(repo), "rev-list", "--reverse", "--no-merges",
                                      f"{base}..{remote}/{p['branch']}"])
            shas = listing.split()
            if code != 0 or not shas:
                raise BuildFailed(f"{p['id']}: no commits on {remote}/{p['branch']} after {base}")
        out.append((p["id"], shas))
    return out


def cherry_pick(r: Runner, wt: Path, git: str, tag: str, commits: list[tuple[str, list[str]]]) -> int:
    """Apply every commit. Returns how many changed something (0 means upstream has them all)."""
    applied = 0
    for patch_id, shas in commits:
        for sha in shas:
            if r.run(f"cherry-pick {sha[:10]} ({patch_id})", [git, "-C", str(wt), "cherry-pick", sha], check=False) == 0:
                applied += 1
                continue
            _, conflicted = r.output([git, "-C", str(wt), "diff", "--name-only", "--diff-filter=U"])
            if not conflicted:
                code, _ = r.output([git, "-C", str(wt), "diff", "--cached", "--quiet"])
                if code == 0:  # empty: the tag already holds this change
                    r.run("cherry-pick --skip", [git, "-C", str(wt), "cherry-pick", "--skip"], check=False)
                    continue
            r.run("cherry-pick --abort", [git, "-C", str(wt), "cherry-pick", "--abort"], check=False)
            files = ", ".join(conflicted.split()[:3]) or "unknown files"
            raise BuildFailed(f"{patch_id}: commit {sha[:10]} does not apply to {tag} (conflict in {files})")
    return applied


def public_config(dist: Path, original: Path) -> dict[str, str]:
    """Find each marker's code context in the marker build, find the same context in the original
    client, and return the original's value. A value whose context sits next to an earlier marker is
    found in a later round, once that marker is replaced by its value."""
    header, base = client_patch.read_header(original)
    installed = []
    with original.open("rb") as f:
        for path, node in client_patch.entries(header).items():
            if path.startswith(client_patch.CLIENT_DIR + "assets/") and path.endswith(".js"):
                f.seek(base + int(node["offset"]))
                installed.append(f.read(int(node["size"])).decode("utf-8", "replace"))
    built = {p: p.read_text(encoding="utf-8", errors="replace") for p in (dist / "assets").rglob("*.js")}
    found: dict[str, str] = {}
    for _ in range(len(MARKERS)):
        for name, mark in MARKERS.items():
            if name in found:
                continue
            values = set()
            for text in built.values():
                for m in re.finditer(re.escape(mark), text):
                    quote = text[m.start() - 1]
                    if quote not in "\"'`":
                        continue
                    before = text[max(0, m.start() - 48):m.start()]
                    for other in installed:
                        for hit in re.finditer(re.escape(before), other):
                            end = other.find(quote, hit.end())
                            if end > hit.end():
                                values.add(other[hit.end():end])
            if len(values) == 1:
                found[name] = values.pop()
                built = {p: t.replace(mark, found[name]) for p, t in built.items()}
    missing = [n for n in MARKERS if n not in found]
    if missing:
        raise BuildFailed(f"the release's public config was not found in the installed client ({', '.join(missing)})")
    return found


def copy_client(dist: Path, dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    count = 0
    for src in dist.rglob("*"):
        rel = src.relative_to(dist)
        if src.is_dir() or src.suffix == ".map" or any(part.startswith(".") for part in rel.parts):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        count += 1
    return count


def asar_module(wt: Path) -> Path:
    """The @electron/asar package the release's scripts use (scripts/package.json), from the install."""
    import json
    spec = json.loads((wt / "scripts" / "package.json").read_text(encoding="utf-8"))
    wanted = (spec.get("dependencies", {}) | spec.get("devDependencies", {})).get("@electron/asar", "")
    major = (re.findall(r"\d+", wanted) or [""])[0]
    best = None
    for d in (wt / "node_modules" / ".pnpm").glob("@electron+asar@*"):
        ver = d.name.split("@")[-1]
        nums = tuple(int(x) for x in re.findall(r"\d+", ver)[:3])
        if major and str(nums[0] if nums else "") != major:
            continue
        if best is None or nums > best[0]:
            best = (nums, d / "node_modules" / "@electron" / "asar")
    if best is None or not best[1].exists():
        raise BuildFailed("the install has no @electron/asar package")
    return best[1]


def _rmtree(path: Path) -> None:
    """Delete a folder with long paths. It never follows a junction (Python 3.8 and later), so the
    pnpm store and the T3 Code clone are safe; read-only files lose the flag first."""
    def again(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        target = "\\\\?\\" + str(path.resolve()) if os.name == "nt" else str(path)
        if sys.version_info >= (3, 12):
            shutil.rmtree(target, onexc=again)
        else:  # Python 3.11 has only onerror; it calls again() the same way
            shutil.rmtree(target, onerror=again)


def work_dir(version: str) -> Path:
    return config.data_dir() / "b" / hashlib.sha256(version.encode()).hexdigest()[:8]


def build(version: str, patches: list[dict], keys: list[dict], repo: Path, original: Path, log_path: Path,
          out: Callable[[str], None] | None = None) -> Path:
    """Build and stage the patch for one version. Raises BuildFailed with the reason."""
    r = Runner(log_path, out or (lambda s: None))
    git = _tool("git")
    work = work_dir(version)
    wt = work / "wt"
    tag = f"v{version}"
    started = time.monotonic()
    r.note(f"=== build for T3 Code {version}: {', '.join(p['id'] for p in patches)}")
    try:
        _cleanup(r, git, repo, work)
        work.mkdir(parents=True)
        if r.output([git, "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"])[0] != 0:
            r.run(f"fetch {tag}", [git, "-C", str(repo), "fetch", "--no-tags", "origin",
                                   f"+refs/tags/{tag}:refs/tags/{tag}"])
        commits = resolve_commits(r, repo, git, patches)
        r.run("worktree add", [git, "-C", str(repo), "worktree", "add", "--detach", str(wt), tag], timeout=900)
        if cherry_pick(r, wt, git, tag, commits) == 0:
            raise BuildFailed(f"T3 Code {version} already holds every patch commit; no patch is needed")
        npx = _tool("npx")
        vp = ["-y", "-p", f"vite-plus@{vite_plus_version(wt)}"]
        r.run("vp install", [npx, *vp, "vp", "install", *FILTERS], cwd=wt, env=build_env(version, {}))
        r.run("release versions", [_tool("node"), "scripts/update-release-package-versions.ts", version], cwd=wt)
        r.run("marker build", [npx, *vp, *WEB_BUILD], cwd=wt, env=build_env(version, MARKERS))
        values = public_config(wt / "apps" / "web" / "dist", original)
        r.note(f"public config: {len(values)} values found in the original client")
        r.run("web build", [npx, *vp, *WEB_BUILD], cwd=wt, env=build_env(version, values))
        values.clear()
        dist = wt / "apps" / "web" / "dist"
        if any(any(m in p.read_text(encoding="utf-8", errors="replace") for m in MARKERS.values())
               for p in (dist / "assets").rglob("*.js")):
            raise BuildFailed("the web build still holds the marker values (a cached build?)")
        client = work / "client"
        r.note(f"client folder: {copy_client(wt / 'apps' / 'web' / 'dist', client)} files")
        brand = "nightly" if is_nightly(version) else "production"
        r.run(f"{brand} brand", [_tool("node"), "scripts/apply-web-brand-assets.ts", brand, str(client)], cwd=wt)
        licences = client_patch.CLIENT_DIR + "third-party-licenses.json"
        if licences in client_patch.entries(client_patch.read_header(original)[0]):
            (client / "third-party-licenses.json").write_bytes(client_patch.read_file(original, licences))
        source = f"{tag} + " + ", ".join(f"{pid} {' '.join(s[:10] for s in shas)}" for pid, shas in commits)
        client_patch.stage(client, asar_module(wt), source=source, out=r.note, version=version, keys=keys,
                           original=original, flags=_flags())
        r.note(f"=== staged for {version} in {time.monotonic() - started:.0f} s")
        return client_patch.staged_asar(version)
    except client_patch.Refused as e:
        r.note(f"=== failed: {e}")
        raise BuildFailed(str(e)) from None
    except BuildFailed as e:
        r.note(f"=== failed: {e}")
        raise
    except Exception as e:  # noqa: BLE001 - shown as the reason in Settings
        r.note(f"=== failed: {type(e).__name__}: {e}")
        raise BuildFailed(f"{type(e).__name__}: {e}") from None
    finally:
        try:
            _cleanup(r, git, repo, work)
        finally:
            r.close()


def _cleanup(r: Runner, git: str, repo: Path, work: Path) -> None:
    """Remove the worktree folder, then git's record of it. Only this build's own folder."""
    wt = work / "wt"
    _rmtree(wt)
    r.run("worktree remove", [git, "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=False)
    _rmtree(work)
