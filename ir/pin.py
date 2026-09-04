"""<pin> — pin.md. One entity, two contexts (part pin / module pin),
distinguished structurally by the parent symbol (pooled vs. moduleless),
never by a flag on the pin itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .units import validate_angle, validate_bool01, validate_length, validate_orthogonal_angle


class Direction(Enum):
    IN = "in"
    OUT = "out"
    IO = "io"
    PASSIVE = "pas"
    POWER = "pwr"
    SUPPLY = "sup"
    """power-symbol.md wire — carries the bus name onto its net. Forbidden
    on a module pin."""
    OPEN_COLLECTOR = "oc"
    HIZ = "hiz"


@dataclass
class Pin:
    name: str
    direction: Direction
    x: int
    y: int
    rot: int = 0
    length: int = 2540
    pinvis: int = 1
    """Only meaningful on a pooled (library) symbol — see is_module_pin."""
    padvis: int = 1

    def __post_init__(self) -> None:
        if not self.name or any(ch.isspace() or ch in "{}" for ch in self.name):
            raise ValueError(f"pin name out of domain (no space, '{{', '}}'): {self.name!r}")
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_orthogonal_angle(self.rot)
        validate_length(self.length, name="length")
        validate_bool01(self.pinvis, name="pinvis")
        validate_bool01(self.padvis, name="padvis")

    def display_name(self) -> str:
        """Strip the `@N` disambiguation suffix — three GND pins draw as GND."""
        return self.name.partition("@")[0]
