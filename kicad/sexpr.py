"""Generic S-expression tree, reader and writer, matching real KiCad's own
pretty-printer byte-for-byte (tab indent, LF endings, one nested list per
line, atoms stay on their parent's line) — ground-truthed against
testData/Eagle/tolmach/hardware/kicad/tolmach-eagle-import.kicad_sym, a
file KiCad 10 itself wrote.

The reader is the writer's mirror, and deliberately knows nothing about
KiCad: every file of theirs — `.kicad_sch`, `.kicad_pcb`, `.kicad_mod`,
`.kicad_sym`, both lib tables — is one s-expression, and this returns it
as a raw tree. No object model stands between the tree and the converter:
kiutils, tried on the legacy path, turned out to mis-parse `(hide yes)`,
not to know `embedded_files` at all, and to crash on legacy pads — every
one of which had to be worked around on the raw text anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Sym:
    """A bare, unquoted atom — `yes`, `no`, `global`, `line` — as opposed
    to a Python str, which always round-trips as a quoted string (even a
    single bare word like the generator name "eeschema")."""
    value: str

    def __str__(self) -> str:
        return self.value


Atom = str | int | float | Sym
Node = list  # [tag: str, *(Atom | Node)]


# One token: a paren, a quoted string (backslash escapes anything, the
# closing quote included), or a bare run up to the next space or paren.
# A regex rather than a character loop because these files are large —
# freq_new.kicad_pcb is 3.4 MB.
_TOKEN = re.compile(r'[()]|"(?:[^"\\]|\\.)*"|[^\s()]+')
_UNESCAPE = re.compile(r"\\(.)")
_UNESCAPED = {"n": "\n", "\\": "\\", '"': '"'}


def _parse_atom(token: str) -> Atom:
    """Exactly the inverse of `_format_atom`. Quoted is always a str, even
    when it looks like a number — `(version 20260206)` is an int and
    `(uuid "0fac93dd-…")` is not, and the quotes are what says so. Bare is
    a number when it parses as one, otherwise a `Sym`."""
    if token.startswith('"'):
        # `\n` is a real line break the writer escaped (a two-line silk
        # text in staya.brd is the case on record); an unknown escape
        # keeps the character it guards, which is what KiCad does too.
        return _UNESCAPE.sub(lambda m: _UNESCAPED.get(m.group(1), m.group(1)), token[1:-1])
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return Sym(token)


def loads(text: str) -> Node:
    """Parse one s-expression — every KiCad file is exactly one — into a
    `[tag, *(Atom | Node)]` tree, the same shape `dumps` writes."""
    stack: list[Node] = []
    root: Node | None = None
    for m in _TOKEN.finditer(text):
        token = m.group()
        if token == "(":
            node: Node = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError("second top-level s-expression: not a KiCad file")
            else:
                root = node
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError(f"unbalanced ')' at offset {m.start()}")
            if not stack.pop():
                raise ValueError(f"empty list at offset {m.start()}")
        elif stack:
            # A node's first token is its tag, and a tag is always a bare
            # word — kept a plain `str`, exactly as `Node` declares it, so
            # that `kid(node, "footprint")` compares against a string and
            # not against a `Sym`. Board layers are the one place where a
            # tag is a number — `(0 "F.Cu" signal)` — and "0" as a string
            # writes back out identically.
            stack[-1].append(token if not stack[-1] else _parse_atom(token))
        else:
            raise ValueError(f"atom {token!r} outside any list at offset {m.start()}")
    if stack:
        raise ValueError("unbalanced '(': file ends inside a list")
    if root is None:
        raise ValueError("empty file: no s-expression at all")
    return root


def load(path) -> Node:
    from pathlib import Path
    return loads(Path(path).read_text(encoding="utf-8"))


# Navigation. Three functions, because a raw tree is read far more often
# than it is built, and `[c for c in node if isinstance(c, list) and
# c[0] == tag]` spelled out at every call site would bury the intent.

def kids(node: Node, tag: str) -> list[Node]:
    """Every direct child list under `tag`."""
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == tag]


def kid(node: Node, tag: str) -> Node | None:
    """The first direct child list under `tag`, or None."""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def atoms(node: Node | None) -> list[Atom]:
    """A node's own values, with its tag and its child lists left out:
    `(at 12.7 5.08 90)` gives `[12.7, 5.08, 90]`.

    A missing node gives no values. `kid` returns None for a node that
    isn't there, and "absent" and "present but empty" mean the same thing
    to every caller here — an optional `(at …)` is simply the origin."""
    if node is None:
        return []
    return [c for c in node[1:] if not isinstance(c, list)]


def _format_atom(a: Atom) -> str:
    if isinstance(a, Sym):
        return a.value
    if isinstance(a, bool):
        raise TypeError("bare bool given as an s-expr atom — use Sym('yes'/'no')")
    if isinstance(a, float):
        s = f"{a:.6f}".rstrip("0").rstrip(".")
        return s if s and s != "-0" else "0"
    if isinstance(a, int):
        return str(a)
    # A LITERAL newline inside a quoted token is not legal s-expression, and
    # KiCad rejects the whole file for it ("Unterminated delimited string").
    # Real case: `staya.brd` carries a two-line silkscreen text, "Eagle\nPack
    # V0.1" — one `<text>` in a 600 KB board was enough to make the result
    # unopenable. Escaped, not stripped: the line break is the author's, and
    # KiCad reads `\n` back as one.
    return ('"' + str(a).replace("\\", "\\\\").replace('"', '\\"')
                        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
            + '"')


def dumps(node: Node) -> str:
    lines: list[str] = []

    def walk(n: Node, depth: int) -> None:
        tag, *rest = n
        atoms = [r for r in rest if not isinstance(r, list)]
        children = [r for r in rest if isinstance(r, list)]
        indent = "\t" * depth
        head = indent + "(" + str(tag)
        if atoms:
            head += " " + " ".join(_format_atom(a) for a in atoms)
        if not children:
            lines.append(head + ")")
            return
        lines.append(head)
        for c in children:
            walk(c, depth + 1)
        lines.append(indent + ")")

    walk(node, 0)
    return "\n".join(lines) + "\n"


def write(node: Node, path) -> None:
    from pathlib import Path
    Path(path).write_text(dumps(node), encoding="utf-8", newline="\n")
