"""Minimal comment-preserving editor for simple TOML files like config.toml.

Python's standard library can read TOML (tomllib) but not write it, and
writing a fresh file would throw away all the helpful comments. So this module
edits the text in place:

* `set_values(text, {(section, key): value})` replaces the value on an existing
  `key = value` line inside `[section]` (keeping indentation, spacing around `=`,
  and any trailing `# comment`), inserts missing keys at the end of their
  section (creating the section if needed), and removes keys whose new value is
  `REMOVE`. Multi-line values (e.g. a list spread over several lines) are
  replaced as a whole. Everything else in the file is left byte-for-byte alone.
* `format_value(value)` writes a Python value as valid TOML.

Only plain `[section]` tables with bare keys are supported (that's all
config.toml uses). The caller re-reads the result with tomllib to be safe.
"""

from __future__ import annotations

import math
import re
import tomllib

REMOVE = object()   # sentinel: delete this key

_HEADER_RE = re.compile(r"^[ \t]*\[[ \t]*([A-Za-z0-9_.-]+)[ \t]*\][ \t]*(?:#.*)?$")
_ARRAY_HEADER_RE = re.compile(r"^[ \t]*\[\[")
_KEY_RE = re.compile(r"^([ \t]*)([A-Za-z0-9_-]+)([ \t]*=[ \t]*)")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def format_value(value, multiline_indent: str | None = None, width: int = 100) -> str:
    """TOML text for a bool / int / float / str / list of those.

    With multiline_indent, a list is written one-or-more items per line inside
    [ ... ] (used when the original list spanned several lines).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("TOML values cannot be NaN or infinite here")
        text = repr(value)
        if "e" not in text and "." not in text:
            text += ".0"
        return text
    if isinstance(value, str):
        return format_string(value)
    if isinstance(value, (list, tuple)):
        items = [format_value(v) for v in value]
        if multiline_indent is None or not items:
            return "[" + ", ".join(items) + "]"
        lines, current = [], ""
        for item in items:
            piece = item + ","
            if current and len(multiline_indent) + len(current) + 1 + len(piece) > width:
                lines.append(multiline_indent + current)
                current = piece
            else:
                current = f"{current} {piece}" if current else piece
        if current:
            lines.append(multiline_indent + current)
        return "[\n" + "\n".join(lines) + "\n]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _value_end(text: str, start: int) -> int:
    """Index just past the TOML value that begins at text[start] (may span lines)."""
    i, n, depth = start, len(text), 0
    while i < n:
        ch = text[i]
        if text.startswith('"""', i) or text.startswith("'''", i):
            q = text[i:i + 3]
            j = i + 3
            while j < n:
                if q == '"""' and text[j] == "\\":
                    j += 2
                    continue
                if text.startswith(q, j):
                    j += 3
                    while j < n and text[j] == q[0]:   # up to two extra quotes may close it
                        j += 1
                    break
                j += 1
            i = j
            continue
        if ch == '"' or ch == "'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                if ch == '"' and text[j] == "\\":
                    j += 1
                j += 1
            i = j + 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#":
            if depth == 0:
                return _rstrip_pos(text, start, i)
            while i < n and text[i] != "\n":   # comment inside a multi-line array
                i += 1
            continue
        elif ch == "\n":
            if depth == 0:
                return _rstrip_pos(text, start, i)
        i += 1
    return _rstrip_pos(text, start, i)


def _rstrip_pos(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in " \t\r":
        end -= 1
    return end


class _Entry:
    __slots__ = ("section", "key", "line_start", "value_start", "value_end", "line_end", "indent")

    def __init__(self, section, key, line_start, value_start, value_end, line_end, indent):
        self.section, self.key = section, key
        self.line_start, self.value_start, self.value_end, self.line_end = line_start, value_start, value_end, line_end
        self.indent = indent


def _scan(text: str):
    """Find sections and key/value entries. Returns (entries, section_ends, section_header_ends)."""
    entries: list[_Entry] = []
    section_last: dict[str, int] = {}     # section -> end offset of its last entry line (incl. newline)
    headers: dict[str, int] = {}          # section -> end offset of its header line (incl. newline)
    section = ""
    pos, n = 0, len(text)
    while pos < n:
        nl = text.find("\n", pos)
        line_end = n if nl == -1 else nl + 1
        line = text[pos:line_end].rstrip("\r\n")
        if _ARRAY_HEADER_RE.match(line):
            section = "\0unsupported"
            pos = line_end
            continue
        m = _HEADER_RE.match(line)
        if m:
            section = m.group(1)
            headers.setdefault(section, line_end)
            section_last.setdefault(section, line_end)
            pos = line_end
            continue
        km = _KEY_RE.match(line)
        if km:
            vstart = pos + km.end()
            vend = _value_end(text, vstart)
            nl2 = text.find("\n", vend)
            entry_end = n if nl2 == -1 else nl2 + 1
            entries.append(_Entry(section, km.group(2), pos, vstart, vend, entry_end, km.group(1)))
            section_last[section] = entry_end
            pos = entry_end
            continue
        pos = line_end
    return entries, section_last, headers


def _same(old, new) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return type(old) is type(new) and old == new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return old == new
    return type(old) is type(new) and old == new


def _parse_value(text: str):
    try:
        return tomllib.loads("v = " + text)["v"]
    except tomllib.TOMLDecodeError:
        return object()     # unparseable: never "the same"


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def set_values(text: str, updates: dict[tuple[str, str], object]) -> str:
    """Return text with the given (section, key) values set (or removed with REMOVE)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    entries, section_last, headers = _scan(text)
    by_key = {(e.section, e.key): e for e in entries}

    edits: list[tuple[int, int, str]] = []          # (start, end, replacement), applied back to front
    inserts: dict[str, list[str]] = {}              # section -> new lines to add
    for (section, key), value in updates.items():
        e = by_key.get((section, key))
        if e is None:
            if value is REMOVE:
                continue
            inserts.setdefault(section, []).append(f"{key} = {format_value(value)}")
            continue
        if value is REMOVE:
            edits.append((e.line_start, e.line_end, ""))
            continue
        old_text = text[e.value_start:e.value_end]
        if _same(_parse_value(old_text), value):
            continue
        multiline = "\n" in old_text and isinstance(value, (list, tuple))
        new_text = format_value(value, multiline_indent=(e.indent + "  ") if multiline else None)
        if multiline and e.indent:
            new_text = new_text.replace("\n]", "\n" + e.indent + "]")
        edits.append((e.value_start, e.value_end, new_text.replace("\n", newline)))

    tail: list[str] = []
    for section, lines in inserts.items():
        block = "".join(line + newline for line in lines)
        if section in section_last:
            at = section_last[section]
            if at > 0 and text[at - 1] != "\n":
                block = newline + block
            edits.append((at, at, block))
        else:
            tail.append(f"[{section}]{newline}{block}")

    out = text
    for start, end, repl in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        out = out[:start] + repl + out[end:]
    if tail:
        if out and not out.endswith("\n"):
            out += newline
        if out.strip():
            out += newline
        out += newline.join(tail)
    return out
