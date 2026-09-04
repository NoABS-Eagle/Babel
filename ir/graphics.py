"""Shared graphics primitives — line.md, arc.md, shape.md, polygon.md,
vertex.md, text.md. Used inside symbols, footprints, schematics, boards,
signals and platings; the parent container decides the role, not the tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .units import validate_angle, validate_bool01, validate_curve, validate_length, validate_range

ALIGN_GRID = {
    f"{v}-{h}" for v in ("top", "center", "bottom") for h in ("left", "center", "right")
}


@dataclass
class Vertex:
    x: int
    y: int
    curve: int | None = None
    """Bulge of the edge leaving THIS vertex, toward the next one."""

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        if self.curve is not None:
            validate_curve(self.curve)


@dataclass
class Line:
    x1: int
    y1: int
    x2: int
    y2: int
    width: int
    layer: int | None = None
    """None inside <segment> (wire) and <plating> (path) — the container
    fixes the layer, writing one would be a second home for one fact."""
    anti: bool = False
    """The `!` prefix — layer-model.md #часть-3-анти-слои. A cutout: this
    object is absent material, not present material, and subtracts from
    polygons on its own layer wherever it overlaps them."""

    def __post_init__(self) -> None:
        for n, v in (("x1", self.x1), ("y1", self.y1), ("x2", self.x2), ("y2", self.y2)):
            validate_length(v, name=n)
        validate_length(self.width, name="width")


@dataclass
class Arc:
    x1: int
    y1: int
    x2: int
    y2: int
    curve: int
    width: int
    layer: int | None = None
    anti: bool = False

    def __post_init__(self) -> None:
        for n, v in (("x1", self.x1), ("y1", self.y1), ("x2", self.x2), ("y2", self.y2)):
            validate_length(v, name=n)
        validate_curve(self.curve)
        validate_length(self.width, name="width")
        if (self.x1, self.y1) == (self.x2, self.y2):
            raise ValueError("arc endpoints coincide: no chord, no center, no arc")


@dataclass
class Shape:
    x: int
    y: int
    w: int
    h: int
    layer: int
    rot: int = 0
    roundness: int = 0
    outline: int = 0
    """0 = filled; any other value = outline-only stroke of that width."""
    anti: bool = False

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.w, name="w")
        validate_length(self.h, name="h")
        validate_angle(self.rot)
        validate_range(self.roundness, 0, 100, name="roundness")
        validate_length(self.outline, name="outline")


@dataclass
class Polygon:
    layer: int
    width: int
    vertices: list[Vertex] = field(default_factory=list)
    fill: int = 100
    rank: int = 1
    thermals: int = 1
    """0/1 — does this polygon grow thermal spokes at all. Not the pad-side
    0..100 insulation scale of the same name (pad.py/smd.py)."""
    clearance: int = 0
    anti: bool = False
    """An anti-polygon (`!N`) subtracts from every polygon on layer N
    instead of being copper itself — rank/thermals/clearance are then
    meaningless (layer-model.md), same "not written" family as elsewhere,
    left as unchecked here since that check needs the parent (Signal vs.
    bare layout child) to know whether they'd apply at all."""

    def __post_init__(self) -> None:
        validate_length(self.width, name="width")
        validate_range(self.fill, 0, 100, name="fill")
        validate_range(self.rank, 1, 6, name="rank")
        validate_bool01(self.thermals, name="thermals")
        validate_length(self.clearance, name="clearance")
        if len(self.vertices) < 3:
            raise ValueError("polygon needs at least 3 vertices")


@dataclass
class Text:
    x: int
    y: int
    height: int
    layer: int
    align: str
    content: str = ""
    width: int | None = None
    rot: int = 0
    mirror: int = 0
    ratio: int = 10
    anti: bool = False

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.height, name="height")
        if self.width is not None:
            validate_length(self.width, name="width")
        validate_angle(self.rot)
        validate_bool01(self.mirror, name="mirror")
        validate_range(self.ratio, 0, 25, name="ratio")
        if self.align not in ALIGN_GRID:
            raise ValueError(f"align out of the nine-cell grid: {self.align!r}")
