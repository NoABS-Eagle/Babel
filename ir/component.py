"""<component> / <gate> / <device> / <map> — component.md, gate.md,
device.md, map.md. A component is a family: one or more gates (sections of
the symbol) plus zero or more devices (symbol-to-footprint pairings).

Cross-referential checks against the actual Symbol/Footprint objects a gate
or device names (pin existence, pad existence) belong to a library-level
resolve step, not here — this module only knows the strings, not the pools
they address.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr
from .naming import validate_catalog_name
from .units import validate_length


@dataclass
class Map:
    pin: str
    pad: str
    """One or more pad names, space-separated — map.md #один-пин--несколько-падов."""
    gate: str = ""

    @property
    def pads(self) -> list[str]:
        return self.pad.split()

    def __post_init__(self) -> None:
        if not self.pin or any(ch.isspace() or ch in "{}" for ch in self.pin):
            raise ValueError(f"map pin name out of domain: {self.pin!r}")
        if not self.pads:
            raise ValueError("map pad list is empty")
        for p in self.pads:
            if any(ch in "{}" for ch in p):
                raise ValueError(f"map pad name out of domain: {p!r}")


@dataclass
class Gate:
    symbol: str
    """Name of a <symbol> in this component's own library."""
    name: str = ""
    x: int = 0
    y: int = 0
    """Library preview layout only — does not affect any canvas placement."""

    def __post_init__(self) -> None:
        if any(ch.isspace() or ch in "{}" for ch in self.name):
            raise ValueError(f"gate name out of domain: {self.name!r}")
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")


@dataclass
class Device:
    footprint: str
    """Name of a <footprint> in this component's own library."""
    name: str = ""
    """Suffix appended to the component name with no separator: "-0603"."""
    maps: list[Map] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)

    def __post_init__(self) -> None:
        if any(ch.isspace() or ch in "{}" for ch in self.name):
            raise ValueError(f"device name out of domain: {self.name!r}")
        all_pads = [pad for m in self.maps for pad in m.pads]
        lowered = [p.lower() for p in all_pads]
        if len(lowered) != len(set(lowered)):
            raise ValueError("a pad name repeats across this device's maps — pad↔pin is one link, not two")


@dataclass
class Component:
    name: str
    gates: list[Gate]
    """A component without a gate does not exist — its symbol IS the gate,
    the only way to place it on a schematic at all."""
    prefix: str = "PART"
    library: str | None = None
    devices: list[Device] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_catalog_name(self.name)
        if not self.gates:
            raise ValueError(f"component {self.name!r}: at least one gate is required")
        if any(ch.isspace() or ch in "@{}:" for ch in self.prefix):
            raise ValueError(f"prefix out of the designator domain (no space, '@', '{{}}', ':'): {self.prefix!r}")
        if self.prefix != self.prefix.upper():
            raise ValueError(f"prefix must be upper-case, like a designator: {self.prefix!r}")

        gate_names = [g.name.lower() for g in self.gates]
        if len(gate_names) != len(set(gate_names)):
            raise ValueError(f"component {self.name!r}: duplicate gate name")

        device_names = [d.name.lower() for d in self.devices]
        if len(device_names) != len(set(device_names)):
            raise ValueError(f"component {self.name!r}: duplicate device name")

        known_gates = {g.name.lower() for g in self.gates}
        for d in self.devices:
            for m in d.maps:
                if m.gate.lower() not in known_gates:
                    raise ValueError(
                        f"component {self.name!r}, device {d.name!r}: "
                        f"map references unknown gate {m.gate!r}"
                    )
