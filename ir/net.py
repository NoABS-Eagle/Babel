"""<net> / <segment> — net.md, segment.md. A net is a pool of segments; a
segment is one electrically-connected clump of wire, tied to others of the
same net only by sharing the net's name — never touching geometrically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .graphics import Line
from .label import Label
from .pinref import PinRef


@dataclass
class Segment:
    lines: list[Line] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    pinrefs: list[PinRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a segment with no wire is meaningless — segment.md")


@dataclass
class Net:
    name: str
    segments: list[Segment] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)
    """class/match/diffpair — role is the key name, all resolve into the
    same <class> pool (class.md #пул-один-ролей-несколько)."""

    def __post_init__(self) -> None:
        if not self.name or any(ch.isspace() or ch in "@{}" for ch in self.name):
            raise ValueError(f"net name out of domain (no space, '@', '{{}}'): {self.name!r}")
        validate_unique_attrs(self.attrs)

    def attr(self, key: str) -> str | None:
        for a in self.attrs:
            if a.name.lower() == key.lower():
                return a.value
        return None

    @property
    def net_class(self) -> str | None:
        return self.attr("class")
