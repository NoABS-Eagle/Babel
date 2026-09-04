"""<library> — library.md. Three independent pools, and nothing else."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr
from .component import Component
from .footprint import Footprint
from .symbol import Symbol


@dataclass
class Library:
    name: str
    symbols: list[Symbol] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)
    """Metadata about the file itself — description, author. Does not
    survive a round trip through a project (library.md #описание-не-переезжает-в-проект)."""

    def __post_init__(self) -> None:
        if ":" in self.name:
            raise ValueError(f"library name must not contain ':': {self.name!r}")

        sym_names = [s.name.lower() for s in self.symbols if s.name is not None]
        if len(sym_names) != len(set(sym_names)):
            raise ValueError(f"library {self.name!r}: duplicate symbol name")

        fp_names = [f.name.lower() for f in self.footprints]
        if len(fp_names) != len(set(fp_names)):
            raise ValueError(f"library {self.name!r}: duplicate footprint name")

        comp_names = [c.name.lower() for c in self.components]
        if len(comp_names) != len(set(comp_names)):
            raise ValueError(f"library {self.name!r}: duplicate component name")

        self._validate_references()

    def _validate_references(self) -> None:
        symbols_by_name = {s.name.lower(): s for s in self.symbols if s.name is not None}
        footprints_by_name = {f.name.lower(): f for f in self.footprints}

        for component in self.components:
            gate_symbols: dict[str, Symbol] = {}
            for gate in component.gates:
                symbol = symbols_by_name.get(gate.symbol.lower())
                if symbol is None:
                    raise ValueError(
                        f"component {component.name!r}, gate {gate.name!r}: "
                        f"symbol {gate.symbol!r} not found in library {self.name!r}"
                    )
                gate_symbols[gate.name.lower()] = symbol

            all_pins = [
                (gate_name, pin.name.lower())
                for gate_name, symbol in gate_symbols.items()
                for pin in symbol.pins
            ]

            for device in component.devices:
                footprint = footprints_by_name.get(device.footprint.lower())
                if footprint is None:
                    raise ValueError(
                        f"component {component.name!r}, device {device.name!r}: "
                        f"footprint {device.footprint!r} not found in library {self.name!r}"
                    )
                has_pads = bool(footprint.pads)
                if has_pads and not all_pins:
                    raise ValueError(
                        f"component {component.name!r}: footprint {footprint.name!r} has pads, "
                        "but no gate's symbol carries a pin to reach them"
                    )
                mapped = {(m.gate.lower(), m.pin.lower()) for m in device.maps}
                for gate_name, pin_name in all_pins:
                    if (gate_name, pin_name) not in mapped:
                        raise ValueError(
                            f"component {component.name!r}, device {device.name!r}: "
                            f"pin {pin_name!r} of gate {gate_name!r} is not wired to any pad — "
                            "an unmapped pin is a lie on the schematic, not a legal state"
                        )
                    pad_names = {p.name.lower() for p in footprint.pads}
                    for m in device.maps:
                        if m.gate.lower() != gate_name or m.pin.lower() != pin_name:
                            continue
                        for pad in m.pads:
                            if pad.lower() not in pad_names:
                                raise ValueError(
                                    f"component {component.name!r}, device {device.name!r}: "
                                    f"map names pad {pad!r}, absent from footprint {footprint.name!r}"
                                )
