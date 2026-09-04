"""<pad> / <smd> / <hole> — pad.md, smd.md, hole.md."""

from __future__ import annotations

from dataclasses import dataclass

from .naming import validate_pin_or_pad_name
from .units import validate_bool01, validate_length, validate_range


@dataclass
class Pad:
    """Through-hole pad. No layer — it pierces every copper layer by
    construction, same reason a <hole> has none."""

    name: str
    x: int
    y: int
    width: int
    height: int
    drill: int
    rot: int = 0
    roundness: int = 0
    inner_dia: int | None = None
    """None = inscribed circle min(width, height); 0 = no inner-layer ring."""
    thermals: int = 100
    stopmask: int = 1

    def __post_init__(self) -> None:
        validate_pin_or_pad_name(self.name)
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.width, name="width")
        validate_length(self.height, name="height")
        validate_length(self.drill, name="drill")
        if self.drill >= min(self.width, self.height):
            raise ValueError("drill must be smaller than the short side of the copper (no ring left)")
        validate_range(self.rot, 0, 359999, name="rot")
        validate_range(self.roundness, 0, 100, name="roundness")
        if self.inner_dia is not None:
            validate_length(self.inner_dia, name="inner_dia")
            if self.inner_dia != 0 and self.inner_dia <= self.drill:
                raise ValueError("inner_dia must be 0 (no ring) or greater than drill")
        validate_range(self.thermals, 0, 100, name="thermals")
        validate_bool01(self.stopmask, name="stopmask")


@dataclass
class Smd:
    """SMD pad. Layer is mandatory: it lives on exactly one copper layer."""

    name: str
    x: int
    y: int
    width: int
    height: int
    layer: int
    rot: int = 0
    roundness: int = 0
    thermals: int = 100
    stopmask: int = 1
    paste: int = 1
    virtual: bool = False
    """True: names copper it doesn't draw — custom-pad.md. roundness/
    stopmask/paste are meaningless (recorded harmlessly, per smd.md)."""

    def __post_init__(self) -> None:
        validate_pin_or_pad_name(self.name)
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.width, name="width")
        validate_length(self.height, name="height")
        if abs(self.layer) >= 100 or self.layer == 0:
            raise ValueError(f"smd layer must be a copper layer (1 or -1, no internal SMD): {self.layer}")
        validate_range(self.rot, 0, 359999, name="rot")
        validate_range(self.roundness, 0, 100, name="roundness")
        validate_range(self.thermals, 0, 100, name="thermals")
        validate_bool01(self.stopmask, name="stopmask")
        validate_bool01(self.paste, name="paste")


@dataclass
class Hole:
    """Unplated hole — fixture, fiducial, keepout. No name: nothing
    addresses it, it belongs to no net. Lives in a footprint or straight
    on a layout."""

    x: int
    y: int
    drill: int

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.drill, name="drill")
