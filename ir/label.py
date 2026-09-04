"""<label> — label.md. Purely visual: a net's name comes from <net name>,
never duplicated here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .graphics import ALIGN_GRID
from .units import validate_bool01, validate_length, validate_orthogonal_angle, validate_range


class LabelStyle(Enum):
    CRUMMY = "crummy"
    """Plain text on the wire — net.md's default when a segment has no
    single pin to borrow a flag shape from."""
    IN = "in"
    OUT = "out"
    IO = "io"
    PASSIVE = "pas"
    POWER = "pwr"


@dataclass
class Label:
    x: int
    y: int
    height: int
    align: str
    layer: int
    style: LabelStyle
    width: int | None = None
    rot: int = 0
    mirror: int = 0
    ratio: int = 10

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.height, name="height")
        if self.width is not None:
            validate_length(self.width, name="width")
        validate_orthogonal_angle(self.rot)
        validate_bool01(self.mirror, name="mirror")
        validate_range(self.ratio, 0, 25, name="ratio")
        if self.align not in ALIGN_GRID:
            raise ValueError(f"align out of the nine-cell grid: {self.align!r}")
