"""Structure pass: markdown in, speakable text out. Fixed rules only; it never guesses at language.

The web script turns the rendered reply into markdown-like text (fences, pipe tables, headings,
list markers), and this module turns that into lines a listener can follow.
"""
from __future__ import annotations

import re

PIECE_CHARS = 450  # small pieces: word timings arrive per piece, ahead of playback even at 2x

LANG_NAMES = {
    "ts": "TypeScript", "tsx": "TypeScript", "typescript": "TypeScript",
    "js": "JavaScript", "jsx": "JavaScript", "javascript": "JavaScript",
    "py": "Python", "python": "Python", "bash": "shell", "sh": "shell", "shell": "shell", "zsh": "shell",
    "powershell": "PowerShell", "pwsh": "PowerShell", "ps1": "PowerShell", "json": "JSON", "yaml": "YAML",
    "yml": "YAML", "sql": "SQL", "rust": "Rust", "rs": "Rust", "go": "Go", "diff": "diff",
    "html": "HTML", "css": "CSS", "mermaid": "mermaid diagram", "text": "", "txt": "", "plaintext": "",
}
FENCE = re.compile(r"^```([\w+-]*)[^\n]*\n(.*?)^```[ \t]*$", re.M | re.S)
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")
PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:|~|[\w.\-]+)?(?:[\\/][\w.\-]+){3,}[\\/]?")


def _short_path(m: re.Match) -> str:
    raw = m.group(0)
    parts = [p for p in re.split(r"[\\/]", raw) if p]
    tail = "/".join(parts[-2:])
    return tail + ("/" if raw.endswith(("/", "\\")) else "")


def inline(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url) -> text
    s = re.sub(r"https?://([^/\s)]+)[^\s)]*", r"a link to \1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = PATH.sub(_short_path, s)
    return s.strip()


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _code_label(m: re.Match) -> str:
    raw = m.group(1).lower()
    lang = LANG_NAMES.get(raw, m.group(1))
    n = len(m.group(2).rstrip("\n").splitlines())
    if lang == "mermaid diagram":
        return "(Skipped a mermaid diagram.)\n"
    kind = f"{lang} code block" if lang else "code block"
    return f"(Skipped a {kind} of {n} lines.)\n"


def prepare(md: str) -> str:
    md = FENCE.sub(_code_label, md.replace("\r\n", "\n"))
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
            header = [inline(h) for h in _cells(line)]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            # Each row is led by its first cell; the other cells follow as "column: value."
            out.append(f"A table with {len(rows)} rows.")
            for row in rows:
                lead = inline(row[0]).rstrip(".") if row else ""
                parts = [f"{h}: {inline(c).rstrip('.')}." for h, c in zip(header[1:], row[1:]) if inline(c)]
                out.append(" ".join([f"{lead}." if lead else ""] + parts).strip())
            continue
        h = re.match(r"^#{1,6}\s+(.*)", line) or re.match(r"^\s*\*\*([^*]+)\*\*:?\s*$", line)
        b = re.match(r"^\s*[-*+]\s+(.*)", line)
        if h:
            text = inline(h.group(1))
            out.append(text if text.endswith((".", ":", "?", "!")) else text + ".")
        elif b:
            out.append(inline(b.group(1)))
        else:
            out.append(inline(line))
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def short_first(parts: list[str], limit: int = 160) -> list[str]:
    """Cut the first piece at a sentence end before `limit` characters. Word timings arrive per
    piece, so a short first piece starts the highlighting within a few seconds."""
    if not parts or len(parts[0]) <= limit:
        return parts
    first = parts[0]
    cut = None
    for m in re.finditer(r"[.!?:](?=\s)|\n", first):
        if m.end() > limit:
            break
        if m.end() >= 40:
            cut = m.end()
            break  # the first sentence is enough
    if cut is None:
        return parts
    return [first[:cut].strip(), first[cut:].strip()] + parts[1:]


def ramp(parts: list[str], limits: tuple[int, ...] = (220, 350)) -> list[str]:
    """After a short first piece, grow the next pieces step by step, so each piece's word timings
    arrive before playback reaches it, even at 2x speed. Cuts at the last sentence end under the limit."""
    out = list(parts)
    for k, limit in enumerate(limits, start=1):
        if k >= len(out) or len(out[k]) <= limit:
            continue
        text = out[k]
        cut = None
        for m in re.finditer(r"[.!?:](?=\s)|\n", text):
            if m.end() > limit:
                break
            if m.end() >= 40:
                cut = m.end()
        if cut:
            out[k:k + 1] = [text[:cut].strip(), text[cut:].strip()]
    return out


def pieces(text: str) -> list[str]:
    """Split into pieces of at most about PIECE_CHARS, at paragraph and then line boundaries."""
    paras: list[str] = []
    for p in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        if len(p) <= PIECE_CHARS:
            paras.append(p)
            continue
        block = ""
        for line in p.splitlines():
            if block and len(block) + len(line) + 1 > PIECE_CHARS:
                paras.append(block)
                block = line
            else:
                block = f"{block}\n{line}" if block else line
        if block:
            paras.append(block)
    out: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > PIECE_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out
