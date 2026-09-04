"""<variant> — variant.md. A named list of differences from the base
schematic — never a full copy of it."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .units import validate_bool01


@dataclass
class PartOverride:
    name: str
    attrs: list[Attr] = field(default_factory=list)
    exclude: int = 0
    """0/1, like every other flag in this format (units.md #7) — not a
    Python bool, for the same reason mirror/stopmask/paste aren't."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a variant's part override needs a name")
        validate_unique_attrs(self.attrs)
        validate_bool01(self.exclude, name="exclude")


@dataclass
class ModuleInstanceOverride:
    name: str
    variant: str | None = None
    """Which of the module's own variants this channel builds — None means
    the module's base."""
    exclude: int = 0
    """No part is installed anywhere inside this channel."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a variant's channel override needs a name")
        validate_bool01(self.exclude, name="exclude")


@dataclass
class Variant:
    name: str
    parts: list[PartOverride] = field(default_factory=list)
    modules: list[ModuleInstanceOverride] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("variant name must not be empty")
        part_names = [p.name.lower() for p in self.parts]
        if len(part_names) != len(set(part_names)):
            raise ValueError(f"variant {self.name!r}: a part named more than once")
        mod_names = [m.name.lower() for m in self.modules]
        if len(mod_names) != len(set(mod_names)):
            raise ValueError(f"variant {self.name!r}: a channel named more than once")
