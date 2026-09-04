"""<layer> — layer.md. Declares one layer: name/color/visibility for a
number that already exists (a schematic-space number under <project>, a
board-space number under <layout>) — it never creates copper, which is
born only from the stack formula (layout.py)."""

from __future__ import annotations

import re

from dataclasses import dataclass

from .units import validate_bool01

_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{8}$")


@dataclass
class LayerDeclaration:
    number: int
    name: str | None = None
    color: str | None = None
    """Hex RRGGBBAA. The one place "whatever the tool picks" is legal —
    color is decoration, not data (layer.md #цвет)."""
    visible: int = 1

    def __post_init__(self) -> None:
        if self.number == 0:
            raise ValueError("layer number 0 is illegal — zero has nowhere to put a sign")
        if self.color is not None and not _COLOR_RE.match(self.color):
            raise ValueError(f"color must be hex RRGGBBAA: {self.color!r}")
        validate_bool01(self.visible, name="visible")
