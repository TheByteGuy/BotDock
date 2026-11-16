"""Minimal dotenv parser (KEY=VALUE, export prefix, quotes, comments, multiline double quotes)."""

from __future__ import annotations

import re

_KEY_LINE = re.compile(r"[ \t]*(?:export[ \t]+)?([^=\s#]+)[ \t]*=[ \t]*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}


def parse_dotenv(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(key, value), ...], [keys whose value could not be parsed])."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    pairs: list[tuple[str, str]] = []
    bad: list[str] = []
    pos, n = 0, len(text)
    while pos < n:
        eol = text.find("\n", pos)
        if eol == -1:
            eol = n
        line = text[pos:eol]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pos = eol + 1
            continue
        m = _KEY_LINE.match(line)
        if not m:  # not KEY=VALUE; never echo the line back (it may contain a secret)
            pos = eol + 1
            continue
        key = m.group(1)
        vstart = pos + m.end()
        quote = text[vstart : vstart + 1]
        if quote == '"':
            i = vstart + 1
            buf: list[str] = []
            closed = False
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append(_ESCAPES.get(nxt, "\\" + nxt))
                    i += 2
                    continue
                if ch == '"':
                    closed = True
                    break
                buf.append(ch)
                i += 1
            if not closed:
                bad.append(key)
                pos = eol + 1
                continue
            pairs.append((key, "".join(buf)))
            nl = text.find("\n", i)
            pos = n if nl == -1 else nl + 1
        elif quote == "'":
            close = text.find("'", vstart + 1)
            if close == -1:
                bad.append(key)
                pos = eol + 1
                continue
            pairs.append((key, text[vstart + 1 : close]))
            nl = text.find("\n", close)
            pos = n if nl == -1 else nl + 1
        else:
            raw = text[vstart:eol]
            raw = re.split(r"[ \t]+#", raw, maxsplit=1)[0]
            pairs.append((key, raw.strip()))
            pos = eol + 1
    return pairs, bad
