"""Minimal, lossless iCalendar line editor.

Why not round-trip through ``icalendar.Calendar``?  Re-serialising normalises
folding, parameter quoting, property order and sometimes values, which would
rewrite properties written by Tasks.org that we never meant to touch.  This
module keeps every content line as its original (folded) bytes and only
replaces the lines we explicitly edit.  ``icalendar`` is still used to *parse
values* (dates, RRULE) and to generate VTIMEZONE components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

CRLF = "\r\n"
_NAME_RE = re.compile(r"^([A-Za-z0-9-]+)")


def escape_text(value: str) -> str:
    """Escape a TEXT value (RFC 5545 3.3.11)."""
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def unescape_text(value: str) -> str:
    """Unescape a TEXT value."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_list(value: str) -> list[str]:
    """Split a comma separated TEXT list on unescaped commas, then unescape."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i : i + 2])
            i += 2
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [unescape_text(p).strip() for p in parts if p.strip()]


def fold(line: str) -> str:
    """Fold a logical line at 75 octets without splitting UTF-8 sequences."""
    encoded = line.encode()
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    limit = 75
    size = 0
    for ch in line:
        width = len(ch.encode())
        if size + width > limit:
            chunks.append(current)
            current = ""
            size = 0
            limit = 74  # continuation lines start with a space
        current += ch
        size += width
    chunks.append(current)
    return (CRLF + " ").join(chunks)


@dataclass
class ContentLine:
    """One logical content line, remembering its original raw text."""

    raw: str  # possibly folded, without trailing newline
    modified: bool = False

    @property
    def unfolded(self) -> str:
        return re.sub(r"\r?\n[ \t]", "", self.raw)

    @property
    def name(self) -> str:
        match = _NAME_RE.match(self.unfolded)
        return match.group(1).upper() if match else ""

    def _split(self) -> tuple[str, str]:
        """Return (name+params, value), honouring quoted parameter values."""
        text = self.unfolded
        in_quote = False
        for idx, ch in enumerate(text):
            if ch == '"':
                in_quote = not in_quote
            elif ch == ":" and not in_quote:
                return text[:idx], text[idx + 1 :]
        return text, ""

    @property
    def value(self) -> str:
        return self._split()[1]

    @property
    def params(self) -> dict[str, str]:
        head = self._split()[0]
        result: dict[str, str] = {}
        # split on ; outside quotes
        pieces: list[str] = []
        buf = ""
        in_quote = False
        for ch in head:
            if ch == '"':
                in_quote = not in_quote
            if ch == ";" and not in_quote:
                pieces.append(buf)
                buf = ""
            else:
                buf += ch
        pieces.append(buf)
        for piece in pieces[1:]:
            if "=" in piece:
                key, val = piece.split("=", 1)
                result[key.upper()] = val.strip('"')
        return result

    @classmethod
    def build(cls, name: str, value: str, params: dict[str, str] | None = None) -> ContentLine:
        head = name.upper()
        for key, val in (params or {}).items():
            if any(c in val for c in ":;,"):
                val = f'"{val}"'
            head += f";{key}={val}"
        return cls(raw=fold(f"{head}:{value}"), modified=True)


@dataclass
class Component:
    """A BEGIN/END block holding lines and nested components in order."""

    name: str
    begin: ContentLine
    end: ContentLine | None = None
    children: list[ContentLine | Component] = field(default_factory=list)

    # -- navigation -----------------------------------------------------
    def lines(self, name: str) -> list[ContentLine]:
        name = name.upper()
        return [c for c in self.children if isinstance(c, ContentLine) and c.name == name]

    def first(self, name: str) -> ContentLine | None:
        found = self.lines(name)
        return found[0] if found else None

    def components(self, name: str) -> list[Component]:
        name = name.upper()
        return [c for c in self.children if isinstance(c, Component) and c.name == name]

    # -- editing ----------------------------------------------------------
    def remove(self, name: str) -> bool:
        name = name.upper()
        before = len(self.children)
        self.children = [c for c in self.children if not (isinstance(c, ContentLine) and c.name == name)]
        return len(self.children) != before

    def set(self, name: str, value: str, params: dict[str, str] | None = None) -> bool:
        """Set a single-valued property; no-op (returns False) if unchanged."""
        new = ContentLine.build(name, value, params)
        existing = self.lines(name)
        if len(existing) == 1:
            old = existing[0]
            if old.value == value and old.params == {k.upper(): v for k, v in (params or {}).items()}:
                return False
            idx = self.children.index(old)
            self.children[idx] = new
            return True
        if existing:
            idx = self.children.index(existing[0])
            self.remove(name)
            self.children.insert(idx, new)
            return True
        self._append_line(new)
        return True

    def replace_all(self, name: str, new_lines: list[ContentLine]) -> None:
        """Replace every line called ``name`` with ``new_lines`` at the first position."""
        existing = self.lines(name)
        idx = self.children.index(existing[0]) if existing else None
        self.remove(name)
        if idx is None:
            for line in new_lines:
                self._append_line(line)
        else:
            for offset, line in enumerate(new_lines):
                self.children.insert(idx + offset, line)

    def _append_line(self, line: ContentLine) -> None:
        # insert after the last property line, before any sub-component (VALARM)
        idx = len(self.children)
        for i, child in enumerate(self.children):
            if isinstance(child, Component):
                idx = i
                break
        self.children.insert(idx, line)

    def insert_component(self, comp: Component, index: int = 0) -> None:
        self.children.insert(index, comp)

    # -- output -----------------------------------------------------------
    def serialize(self) -> list[str]:
        out = [self.begin.raw]
        for child in self.children:
            if isinstance(child, Component):
                out.extend(child.serialize())
            else:
                out.append(child.raw)
        if self.end is not None:
            out.append(self.end.raw)
        return out


class ICalDocument:
    """A parsed VCALENDAR that serialises back losslessly."""

    def __init__(self, root: Component) -> None:
        self.root = root

    @classmethod
    def parse(cls, text: str) -> ICalDocument:
        newline = CRLF if "\r\n" in text else "\n"
        physical = re.split(r"\r?\n", text)
        logical: list[str] = []
        for line in physical:
            if line[:1] in (" ", "\t") and logical:
                logical[-1] += newline + line
            elif line == "":
                continue
            else:
                logical.append(line)
        stack: list[Component] = []
        root: Component | None = None
        for raw in logical:
            cl = ContentLine(raw)
            uname = cl.unfolded.upper()
            if uname.startswith("BEGIN:"):
                comp = Component(name=cl.value.strip().upper(), begin=cl)
                if stack:
                    stack[-1].children.append(comp)
                else:
                    root = comp
                stack.append(comp)
            elif uname.startswith("END:") and stack:
                stack.pop().end = cl
            elif stack:
                stack[-1].children.append(cl)
        if root is None or root.name != "VCALENDAR":
            raise ValueError("not a VCALENDAR")
        return cls(root)

    def serialize(self) -> str:
        # Always emit CRLF (RFC 5545).  calendar-data arrives via XML, whose
        # parser normalises CRLF to LF, so the original line endings are not
        # recoverable anyway; everything else on each line is kept verbatim.
        lines = [re.sub(r"\r?\n", CRLF, line) for line in self.root.serialize()]
        return CRLF.join(lines) + CRLF

    # convenience --------------------------------------------------------
    @property
    def vtodo(self) -> Component:
        """The master VTODO (the one without RECURRENCE-ID)."""
        todos = self.root.components("VTODO")
        if not todos:
            raise ValueError("no VTODO in calendar object")
        for todo in todos:
            if todo.first("RECURRENCE-ID") is None:
                return todo
        return todos[0]

    def timezones(self) -> set[str]:
        result = set()
        for tz in self.root.components("VTIMEZONE"):
            line = tz.first("TZID")
            if line:
                result.add(line.value.strip())
        return result

    def ensure_timezone(self, tzid: str) -> None:
        """Add a VTIMEZONE for ``tzid`` if the document lacks one."""
        if tzid in self.timezones():
            return
        try:
            from icalendar import Timezone

            vtz = Timezone.from_tzid(tzid)
        except Exception:
            return
        sub = ICalDocument.parse("BEGIN:VCALENDAR\r\n" + vtz.to_ical().decode() + "END:VCALENDAR\r\n")
        comp = sub.root.components("VTIMEZONE")[0]
        _mark_modified(comp)
        idx = self.root.children.index(self.root.components("VTODO")[0])
        self.root.insert_component(comp, idx)


def _mark_modified(comp: Component) -> None:
    comp.begin.modified = True
    for child in comp.children:
        if isinstance(child, Component):
            _mark_modified(child)
        else:
            child.modified = True
