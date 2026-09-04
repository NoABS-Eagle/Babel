"""<compinst> — component-instance.md. Placement of one section of a part
on the schematic canvas."""

from __future__ import annotations

from dataclasses import dataclass, field

from .graphics import Text
from .units import validate_bool01, validate_length, validate_orthogonal_angle


@dataclass
class ComponentInstance:
    part: str
    x: int
    y: int
    gate: str = ""
    rot: int = 0
    mirror: int = 0
    texts: list[Text] = field(default_factory=list)
    """Placeholder-layout override list. Exhaustive when non-empty — the
    naming.md smash model: present means "this is everything", absent
    means "read the symbol's own layout"."""

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_orthogonal_angle(self.rot)
        validate_bool01(self.mirror, name="mirror")
