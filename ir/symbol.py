"""<symbol> — symbol.md."""

from __future__ import annotations

from dataclasses import dataclass, field

from .graphics import Arc, Line, Polygon, Shape, Text
from .naming import validate_catalog_name
from .pin import Direction, Pin

GraphicChild = Line | Arc | Shape | Polygon | Text


@dataclass
class Symbol:
    """Pooled when `name` is set (addressed by <gate>); a module's own
    outward face when it is None — symbol.md #символ-внутри-модуля-без-имени.
    library is only meaningful alongside a name.
    """

    name: str | None = None
    library: str | None = None
    pins: list[Pin] = field(default_factory=list)
    graphics: list[GraphicChild] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.name is not None:
            validate_catalog_name(self.name)
        elif self.library is not None:
            raise ValueError("a module symbol (name=None) carries no library either")

        pin_names = [p.name for p in self.pins]
        if len(pin_names) != len(set(n.lower() for n in pin_names)):
            raise ValueError("duplicate pin name within this symbol (case-insensitive)")

        if self.name is None:
            for p in self.pins:
                if p.direction is Direction.SUPPLY:
                    raise ValueError("direction='sup' is forbidden on a module pin")
                if p.pinvis != 1 or p.padvis != 1:
                    raise ValueError("pinvis/padvis are not writable on a module pin — visibility is fixed")

    @property
    def is_pooled(self) -> bool:
        return self.name is not None

    @property
    def is_frame(self) -> bool:
        """A <shape> on layer 98 makes this symbol a sheet frame —
        frame.md, component.md #особые-роли-распознаются-по-структуре."""
        return any(isinstance(g, Shape) and g.layer == 98 for g in self.graphics)
