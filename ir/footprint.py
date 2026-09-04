"""<footprint> — footprint.md."""

from __future__ import annotations

from dataclasses import dataclass, field

from .graphics import Arc, Line, Polygon, Shape, Text
from .model3d import Model3D
from .naming import validate_catalog_name
from .pad import Hole, Pad, Smd

GraphicChild = Line | Arc | Shape | Polygon | Text
PadChild = Pad | Smd


@dataclass
class Footprint:
    name: str
    library: str | None = None
    """Only in a project — footprint.md: "в .silib не пишется"."""
    graphics: list[GraphicChild] = field(default_factory=list)
    pads: list[PadChild] = field(default_factory=list)
    holes: list[Hole] = field(default_factory=list)
    models: list[Model3D] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_catalog_name(self.name)

        pad_names = [p.name.lower() for p in self.pads]
        if len(pad_names) != len(set(pad_names)):
            raise ValueError(f"duplicate pad name within footprint {self.name!r}")

        for g in self.graphics:
            if isinstance(g, (Shape, Polygon)) and abs(g.layer) < 100 and g.layer not in (1, -1):
                raise ValueError(
                    f"footprint {self.name!r}: internal copper layer {g.layer} illegal in a footprint "
                    "(a footprint knows nothing of any board's stack)"
                )

        unconditional = [m for m in self.models if m.key is None]
        if len(unconditional) > 1:
            raise ValueError(f"footprint {self.name!r}: at most one fallback <model3d> (key=None)")
        keys = [m.key.lower() for m in self.models if m.key is not None]
        if len(keys) != len(set(keys)):
            raise ValueError(f"footprint {self.name!r}: duplicate model3d key (case-insensitive)")
