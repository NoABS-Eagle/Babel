"""<plating> — plating.md. A vertical conductor across the whole stack —
the metallized wall of a routed edge or slot. Lives in a <signal> (which
gives it its net, same as any other copper there) or in a <footprint>
(where a virtual <smd> names it instead, since a pool has no signal at
all). No layer, no coordinates of its own — both live in its path.

Path children carry neither `width` nor `layer`: the geometry of the wall
is the routed edge itself (layer 120), and a path only names a stretch of
it — reusing the shared Line/Arc would force a meaningless width on every
caller, so this gets its own minimal segment types instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .units import validate_curve, validate_length


@dataclass
class PlatingLine:
    x1: int
    y1: int
    x2: int
    y2: int

    def __post_init__(self) -> None:
        for n, v in (("x1", self.x1), ("y1", self.y1), ("x2", self.x2), ("y2", self.y2)):
            validate_length(v, name=n)


@dataclass
class PlatingArc:
    x1: int
    y1: int
    x2: int
    y2: int
    curve: int

    def __post_init__(self) -> None:
        for n, v in (("x1", self.x1), ("y1", self.y1), ("x2", self.x2), ("y2", self.y2)):
            validate_length(v, name=n)
        validate_curve(self.curve)
        if (self.x1, self.y1) == (self.x2, self.y2):
            raise ValueError("arc endpoints coincide: no chord, no arc")


@dataclass
class Plating:
    path: list[PlatingLine | PlatingArc] = field(default_factory=list)
    land: int = 0
    """Copper ring width from the routed edge, outer layers."""
    inner_land: int | None = None
    """None = same as `land`; 0 = no ring on internal layers — a legal,
    common state (non-functional pad), not a defect."""

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("plating needs a path — it names a stretch of the routed edge")
        validate_length(self.land, name="land")
        if self.inner_land is not None:
            validate_length(self.inner_land, name="inner_land")
