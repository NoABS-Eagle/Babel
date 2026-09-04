"""<project> — project.md. Root of `.siprj`: shared pools compiled in
whole, plus the product's one electrical truth. The board pool (<layout>
x N) is not modeled yet — the board chapter hasn't been built, so a
Project today is schematic-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .class_ import Class
from .component import Component
from .footprint import Footprint
from .module import Module
from .schematic import Schematic
from .symbol import Symbol

_CLASS_ROLE_KEYS = {"class", "match", "diffpair"}


@dataclass
class Project:
    name: str
    schematic: Schematic
    version: tuple[int, int] = (0, 1)
    symbols: list[Symbol] = field(default_factory=list)
    footprints: list[Footprint] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    classes: list[Class] = field(default_factory=list)
    modules: list[Module] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("project name must not be empty")
        major, minor = self.version
        if major < 0 or minor < 0:
            raise ValueError(f"version must be non-negative: {self.version}")

        self._check_pool_uniqueness()
        self._validate_references()

    @staticmethod
    def _pool_key(library: str | None, name: str) -> tuple[str, str]:
        return ((library or "").lower(), name.lower())

    def _check_pool_uniqueness(self) -> None:
        sym_keys = [self._pool_key(s.library, s.name) for s in self.symbols if s.name is not None]
        if len(sym_keys) != len(set(sym_keys)):
            raise ValueError(f"project {self.name!r}: duplicate (library, symbol) pair")

        fp_keys = [self._pool_key(f.library, f.name) for f in self.footprints]
        if len(fp_keys) != len(set(fp_keys)):
            raise ValueError(f"project {self.name!r}: duplicate (library, footprint) pair")

        comp_keys = [self._pool_key(c.library, c.name) for c in self.components]
        if len(comp_keys) != len(set(comp_keys)):
            raise ValueError(f"project {self.name!r}: duplicate (library, component) pair")

        class_names = [c.name.lower() for c in self.classes]
        if len(class_names) != len(set(class_names)):
            raise ValueError(f"project {self.name!r}: duplicate class name")

        module_names = [m.name.lower() for m in self.modules]
        if len(module_names) != len(set(module_names)):
            raise ValueError(f"project {self.name!r}: duplicate module name")

    def _validate_references(self) -> None:
        symbols_by_key = {
            self._pool_key(s.library, s.name): s for s in self.symbols if s.name is not None
        }
        footprints_by_key = {self._pool_key(f.library, f.name): f for f in self.footprints}
        components_by_key = {self._pool_key(c.library, c.name): c for c in self.components}
        classes_by_name = {c.name.lower() for c in self.classes}
        modules_by_name = {m.name.lower(): m for m in self.modules}

        for component in self.components:
            self._validate_component(component, symbols_by_key, footprints_by_key)

        self._validate_schematic(
            self.schematic, components_by_key, classes_by_name, modules_by_name, context=f"project {self.name!r}"
        )
        for module in self.modules:
            if module.schematic.modinsts:
                raise ValueError(f"module {module.name!r}: hierarchy is exactly one level deep")
            self._validate_schematic(
                module.schematic,
                components_by_key,
                classes_by_name,
                modules_by_name,
                context=f"module {module.name!r}",
            )

    def _validate_component(self, component, symbols_by_key, footprints_by_key) -> None:
        gate_symbols: dict[str, Symbol] = {}
        for gate in component.gates:
            key = self._pool_key(component.library, gate.symbol)
            symbol = symbols_by_key.get(key)
            if symbol is None:
                raise ValueError(
                    f"component ({component.library!r}, {component.name!r}), gate {gate.name!r}: "
                    f"symbol {gate.symbol!r} not found in library {component.library!r}"
                )
            gate_symbols[gate.name.lower()] = symbol

        all_pins = [
            (gate_name, pin.name.lower())
            for gate_name, symbol in gate_symbols.items()
            for pin in symbol.pins
        ]

        for device in component.devices:
            key = self._pool_key(component.library, device.footprint)
            footprint = footprints_by_key.get(key)
            if footprint is None:
                raise ValueError(
                    f"component ({component.library!r}, {component.name!r}), device {device.name!r}: "
                    f"footprint {device.footprint!r} not found in library {component.library!r}"
                )
            if footprint.pads and not all_pins:
                raise ValueError(
                    f"component ({component.library!r}, {component.name!r}): footprint "
                    f"{footprint.name!r} has pads, but no gate's symbol carries a pin to reach them"
                )
            mapped = {(m.gate.lower(), m.pin.lower()) for m in device.maps}
            pad_names = {p.name.lower() for p in footprint.pads}
            for gate_name, pin_name in all_pins:
                if (gate_name, pin_name) not in mapped:
                    raise ValueError(
                        f"component ({component.library!r}, {component.name!r}), device {device.name!r}: "
                        f"pin {pin_name!r} of gate {gate_name!r} is not wired to any pad"
                    )
            for m in device.maps:
                for pad in m.pads:
                    if pad.lower() not in pad_names:
                        raise ValueError(
                            f"component ({component.library!r}, {component.name!r}), device {device.name!r}: "
                            f"map names pad {pad!r}, absent from footprint {footprint.name!r}"
                        )

    def _validate_schematic(
        self, schematic: Schematic, components_by_key, classes_by_name, modules_by_name, *, context: str
    ) -> None:
        for part in schematic.parts:
            key = self._pool_key(part.library, part.component)
            component = components_by_key.get(key)
            if component is None:
                raise ValueError(
                    f"{context}: part {part.name!r} references unknown component "
                    f"({part.library!r}, {part.component!r})"
                )
            has_devices = bool(component.devices)
            if has_devices and part.device is None:
                raise ValueError(f"{context}: part {part.name!r} needs `device` — its component has devices")
            if not has_devices and part.device is not None:
                raise ValueError(
                    f"{context}: part {part.name!r} names a device, but component "
                    f"{component.name!r} has none"
                )
            if part.device is not None:
                known_devices = {d.name.lower() for d in component.devices}
                if part.device.lower() not in known_devices:
                    raise ValueError(f"{context}: part {part.name!r} names unknown device {part.device!r}")

        for net in schematic.nets:
            for a in net.attrs:
                if a.name.lower() in _CLASS_ROLE_KEYS and a.value.lower() not in classes_by_name:
                    raise ValueError(
                        f"{context}: net {net.name!r} attr {a.name!r} references unknown class {a.value!r}"
                    )

        for modinst in schematic.modinsts:
            module = modules_by_name.get(modinst.module.lower())
            if module is None:
                raise ValueError(
                    f"{context}: channel {modinst.name!r} references unknown module {modinst.module!r}"
                )
            if modinst.variant is not None:
                known_variants = {v.name.lower() for v in module.schematic.variants}
                if modinst.variant.lower() not in known_variants:
                    raise ValueError(
                        f"{context}: channel {modinst.name!r} selects unknown variant "
                        f"{modinst.variant!r} of module {module.name!r}"
                    )
