"""<via> — via.md. A through-hole vertical conductor, always through the
whole stack. No layer (pierces everything), no name (nothing addresses it
— it's transit, not a terminal)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .units import validate_length


@dataclass
class Via:
    x: int
    y: int
    drill: int
    diameter: int
    """Outer copper ring diameter — required, no auto-sizing in the IR."""
    inner_dia: int | None = None
    """None = same as `diameter`; 0 = no ring on internal layers at all."""
    attrs: list[Attr] = field(default_factory=list)
    """Tool bookmarks (e.g. STITCHING), not properties — via.md: pretend
    the reader ignores them and nothing about copper/drilling changes."""

    def __post_init__(self) -> None:
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_length(self.drill, name="drill")
        validate_length(self.diameter, name="diameter")
        if self.diameter <= self.drill:
            raise ValueError("diameter must exceed drill — otherwise there's no ring left")
        if self.inner_dia is not None:
            validate_length(self.inner_dia, name="inner_dia")
            if self.inner_dia != 0 and self.inner_dia <= self.drill:
                raise ValueError("inner_dia must be 0 (no ring) or greater than drill")
        validate_unique_attrs(self.attrs)
