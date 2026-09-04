"""<element> — element.md. Placed component: footprint on coordinates and
a side. Carries no reference to component or footprint at all — that's
already said on the schematic (device -> footprint), and duplicating it
here would give the same fact two homes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .graphics import Text
from .units import validate_angle, validate_bool01, validate_length


class Side(Enum):
    TOP = "top"
    BOTTOM = "bottom"


@dataclass
class Element:
    name: str
    """Same designator as the <part> on the schematic — the only link."""
    x: int
    y: int
    rot: int = 0
    """Free — the board isn't limited to 90° steps like the schematic."""
    side: Side = Side.TOP
    exclude: int = 0
    """A ghost: this board doesn't carry the part at all. Only legal when
    every one of its pads sits on at most one net (element.md
    #погасить-можно-то-что-ничего-не-соединяет) — that check needs the
    net/contactref picture and belongs at Layout level, not here."""
    texts: list[Text] = field(default_factory=list)
    """Placeholder-layout override list, same exhaustive-when-present rule
    as component_instance.py."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("element name must not be empty")
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_angle(self.rot)
        validate_bool01(self.exclude, name="exclude")
