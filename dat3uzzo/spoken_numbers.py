"""Deterministic spoken form for numbers, dates, money, units and identifiers (English).

The Realtime model misread dense numbers when it converted them itself (docs/test-results.md),
so code spells them before the model sees the text. Order matters: specific patterns run first.
Uses num2words 0.5.14 (LGPL-2.1) for the words.
"""
from __future__ import annotations

import re

from num2words import num2words

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MON = {m[:3].lower(): i for i, m in enumerate(MONTHS)}
MON_RE = "(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
DIGIT = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
CURRENCY = {"€": "euros", "$": "US dollars", "£": "pounds", "лв": "leva", "BGN": "leva",
            "EUR": "euros", "USD": "US dollars"}
UNITS = {"GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes", "TB": "terabytes",
         "ms": "milliseconds", "min": "minutes", "s": "seconds", "h": "hours"}


def words(n: int) -> str:
    return num2words(n).replace(",", "")


def digits(s: str) -> str:
    return " ".join(DIGIT[int(c)] for c in s if c.isdigit())


def decimal(s: str) -> str:
    s = s.replace(",", "")
    if "." not in s:
        return words(int(s))
    whole, frac = s.split(".", 1)
    return f"{words(int(whole or 0))} point {digits(frac)}"


def year(y: str) -> str:
    return num2words(int(y), to="year").replace(",", "")


def ordinal(d: str) -> str:
    return num2words(int(d), to="ordinal")


def clock(h: str, m: str) -> str:
    mm = int(m)
    tail = "" if mm == 0 else (f" oh {words(mm)}" if mm < 10 else f" {words(mm)}")
    return f"{words(int(h))}{tail or ' hundred'}"


def _dotted(parts: str) -> str:
    return " point ".join(words(int(p)) for p in parts.split("."))


def speak_numbers(text: str) -> str:
    t = text
    t = re.sub(r"\bCVE-(\d{4})-(\d+)\b", lambda m: f"CVE {year(m[1])}, {digits(m[2])}", t)
    t = re.sub(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b",
               lambda m: " dot ".join(words(int(g)) for g in m.groups()), t)
    t = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b",
               lambda m: f"the {ordinal(m[3])} of {MONTHS[int(m[2]) - 1]}, {year(m[1])}", t)
    t = re.sub(rf"\bbetween (\d{{1,2}}) and (\d{{1,2}}) {MON_RE}",
               lambda m: f"between the {ordinal(m[1])} and the {ordinal(m[2])} of {MONTHS[MON[m[3].lower()]]}", t)
    t = re.sub(rf"\b(\d{{1,2}}) {MON_RE}(?: (\d{{4}}))?\b",
               lambda m: f"the {ordinal(m[1])} of {MONTHS[MON[m[2].lower()]]}" + (f", {year(m[3])}" if m[3] else ""), t)
    # versions: pkg@1.2.3, v2.5, 1.5.13 (three or more parts)
    t = re.sub(r"@(\d+(?:\.\d+)+)\b", lambda m: " version " + _dotted(m[1]), t)
    t = re.sub(r"\b[vV](\d+(?:\.\d+)+)\b", lambda m: "version " + _dotted(m[1]), t)
    t = re.sub(r"\b(\d+(?:\.\d+){2,})\b", lambda m: _dotted(m[1]), t)
    t = re.sub(r"#(\d+)\b", lambda m: "number " + (digits(m[1]) if len(m[1]) >= 3 else words(int(m[1]))), t)
    # long unseparated numbers are identifiers: digit by digit
    t = re.sub(r"\b(\d{5,})((?:\.\d+)*)\b",
               lambda m: digits(m[1]) + "".join(" point " + digits(p) for p in m[2].split(".") if p), t)
    # money, percent, multipliers, clock times, units
    t = re.sub(r"([€$£])\s?(\d[\d,]*(?:\.\d+)?)", lambda m: f"{decimal(m[2])} {CURRENCY[m[1]]}", t)
    t = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s?(лв|BGN|EUR|USD)", lambda m: f"{decimal(m[1])} {CURRENCY[m[2]]}", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: f"{decimal(m[1])} percent", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s?[x×](?![\w])", lambda m: f"{decimal(m[1])} times", t)
    t = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", lambda m: clock(m[1], m[2]), t)
    # "s" and "h" need a space before them, so "1990s" is not read as seconds
    t = re.sub(r"\b(\d+(?:\.\d+)?)(?:\s?(GB|MB|KB|TB|ms|min)|\s(s|h))\b",
               lambda m: f"{decimal(m[1])} {UNITS[m[2] or m[3]]}", t)
    t = re.sub(r"\b(?:19|20)\d{2}\b", lambda m: year(m[0]), t)
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: words(int(m[0].replace(",", ""))), t)
    t = re.sub(r"\b\d+\.\d+\b", lambda m: decimal(m[0]), t)
    t = re.sub(r"(?<![\w-])\d+\b", lambda m: words(int(m[0])), t)
    t = re.sub(r"\s?[~≈]\s?", " about ", t)
    t = re.sub(r"\s?→\s?", ", then ", t)
    return re.sub(r"[ \t]{2,}", " ", t)
