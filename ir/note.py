"""<note> — note.md. A markdown textbox; on the schematic or straight on
the board. `h` is a floor, not a ceiling — content may grow past it."""

from __future__ import annotations

from dataclasses import dataclass

from .units import validate_angle, validate_bool01, validate_length, validate_range


@dataclass
class Note:
    x: int
    y: int
    w: int
    h: int
    height: int
    layer: int
    content: str = ""
    rot: int = 0
    mirror: int = 0
    ratio: int = 10

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.w, name="w")
        validate_length(self.h, name="h")
        validate_length(self.height, name="height")
        validate_angle(self.rot)
        validate_bool01(self.mirror, name="mirror")
        validate_range(self.ratio, 0, 25, name="ratio")
