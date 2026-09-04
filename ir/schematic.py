"""<schematic> — schematic.md. The same entity in two places: a project's
one product canvas, or a module's contents — see module.py (not yet
implemented; <modinst> children are not modeled here yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .component_instance import ComponentInstance
from .graphics import Arc, Line, Polygon, Shape, Text
from .net import Net
from .note import Note
from .part import Part
from .variant import Variant

GraphicChild = Line | Arc | Shape | Polygon | Text


@dataclass
class Schematic:
    parts: list[Part] = field(default_factory=list)
    instances: list[ComponentInstance] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    graphics: list[GraphicChild] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)
    """Global attrs — the input sheet for the product, or for a module's
    own contents; role is decided by the parent (project vs module), not
    by anything stored here."""

    def __post_init__(self) -> None:
        validate_unique_attrs(self.attrs)

        part_names = [p.name.lower() for p in self.parts]
        if len(part_names) != len(set(part_names)):
            raise ValueError("duplicate part name on this schematic")

        net_names = [n.name.lower() for n in self.nets]
        if len(net_names) != len(set(net_names)):
            raise ValueError("duplicate net name on this schematic")

        variant_names = [v.name.lower() for v in self.variants]
        if len(variant_names) != len(set(variant_names)):
            raise ValueError("duplicate variant name on this schematic")

        known_parts = {p.name.lower() for p in self.parts}
        seen_sections: set[tuple[str, str]] = set()
        for inst in self.instances:
            if inst.part.lower() not in known_parts:
                raise ValueError(f"compinst references unknown part {inst.part!r}")
            key = (inst.part.lower(), inst.gate.lower())
            if key in seen_sections:
                raise ValueError(f"section (part={inst.part!r}, gate={inst.gate!r}) placed more than once")
            seen_sections.add(key)

        for variant in self.variants:
            for override in variant.parts:
                if override.name.lower() not in known_parts:
                    raise ValueError(
                        f"variant {variant.name!r} references unknown part {override.name!r}"
                    )

        for net in self.nets:
            for segment in net.segments:
                for ref in segment.pinrefs:
                    if ref.gate is not None and ref.inst.lower() not in known_parts:
                        raise ValueError(
                            f"net {net.name!r}: pinref names part {ref.inst!r}, "
                            "which is not on this schematic"
                        )
