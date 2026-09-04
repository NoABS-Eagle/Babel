"""<signal> — signal.md. All the copper of one net — membership comes
from the container, never from a child attribute, same discipline as
<segment> on the schematic side."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contactref import ContactRef
from .graphics import Arc, Line, Polygon
from .plating import Plating
from .via import Via

CopperChild = Line | Arc | Polygon | Plating | Via


@dataclass
class Signal:
    name: str
    """Unique among a board's signals. Must name a real net on the
    schematic iff this signal carries at least one contactref — an
    unconnected signal is free to carry an auto-name (project-level
    check, needs the schematic's net names)."""
    contactrefs: list[ContactRef] = field(default_factory=list)
    copper: list[CopperChild] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("signal name must not be empty")
        for item in self.copper:
            if isinstance(item, (Line, Arc)) and (item.layer is None or abs(item.layer) >= 100):
                raise ValueError(
                    f"signal {self.name!r}: copper must live on a copper layer, got {item.layer!r}"
                )
            if isinstance(item, Polygon) and abs(item.layer) >= 100:
                raise ValueError(
                    f"signal {self.name!r}: copper polygon must live on a copper layer, got {item.layer!r}"
                )
        pairs = [(c.element.lower(), c.pad.lower()) for c in self.contactrefs]
        if len(pairs) != len(set(pairs)):
            raise ValueError(f"signal {self.name!r}: the same (element, pad) named more than once")
