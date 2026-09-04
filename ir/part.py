"""<part> — part.md. One physical thing on the schematic, one for however
many sections it has (see component_instance.py)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .naming import validate_object_name


@dataclass
class Part:
    name: str
    component: str
    library: str
    device: str | None = None
    """Required iff the component has devices at all — a cross-pool check
    that needs the library, done at Project level, not here."""
    attrs: list[Attr] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_object_name(self.name)
        if self.name != self.name.upper():
            raise ValueError(f"designator must be upper-case: {self.name!r}")
        validate_unique_attrs(self.attrs)
