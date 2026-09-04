"""<project> — project.md. Root of `.siprj`: shared pools compiled in
whole, the product's one electrical truth, and any number of physical
boards reading it (project.md #одна-схема-n-плат).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .class_ import Class
from .component import Component
from .footprint import Footprint
from .layout import Layout
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
    layouts: list[Layout] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("project name must not be empty")
        major, minor = self.version
        if major < 0 or minor < 0:
            raise ValueError(f"version must be non-negative: {self.version}")

        layout_names = [layout.name.lower() for layout in self.layouts]
        if len(layout_names) != len(set(layout_names)):
            raise ValueError(f"project {self.name!r}: duplicate layout name")

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

        for layout in self.layouts:
            self._validate_layout(layout, components_by_key)

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

    def _validate_layout(self, layout: Layout, components_by_key: dict[tuple[str, str], Component]) -> None:
        """Cross-checks a board against the product schematic: element.md
        (an element resolves to exactly one part, a ghost may only cover a
        part touching at most one net) and contactref.md (every contactref
        must agree with the schematic's pinref+map).

        Scoped to top-level parts only — an element placed from inside a
        module channel (name like `IC101` or `DCDC1:IC1`) isn't resolved
        against the module's own schematic here, since that requires
        replaying the channel's name expansion; such elements are skipped
        rather than rejected, which is permissive, not validated.
        """
        parts_by_name = {p.name.lower(): p for p in self.schematic.parts}

        pin_net: dict[tuple[str, str, str], str] = {}
        for net in self.schematic.nets:
            for segment in net.segments:
                for ref in segment.pinrefs:
                    if ref.gate is None:
                        continue
                    pin_net[(ref.inst.lower(), ref.gate.lower(), ref.pin.lower())] = net.name

        for element in layout.elements:
            part = parts_by_name.get(element.name.lower())
            if part is None:
                continue

            component = components_by_key.get(self._pool_key(part.library, part.component))
            if component is None:
                continue
            device = next(
                (d for d in component.devices if d.name.lower() == (part.device or "").lower()), None
            )
            if device is None:
                continue

            pad_to_net: dict[str, str] = {}
            for m in device.maps:
                net_name = pin_net.get((part.name.lower(), m.gate.lower(), m.pin.lower()))
                if net_name is None:
                    continue
                for pad in m.pads:
                    pad_to_net[pad.lower()] = net_name

            if element.exclude:
                distinct_nets = set(pad_to_net.values())
                if len(distinct_nets) > 1:
                    raise ValueError(
                        f"layout {layout.name!r}: element {element.name!r} is a ghost, but its pads "
                        f"reach {len(distinct_nets)} different nets on the schematic — only a part "
                        "touching at most one net may be ghosted"
                    )
                continue

            for signal in layout.signals:
                for ref in signal.contactrefs:
                    if ref.element.lower() != element.name.lower():
                        continue
                    expected_net = pad_to_net.get(ref.pad.lower())
                    if expected_net is None:
                        raise ValueError(
                            f"layout {layout.name!r}, signal {signal.name!r}: contactref names pad "
                            f"{ref.pad!r} of {element.name!r}, which the schematic does not wire to any net"
                        )
                    if expected_net.lower() != signal.name.lower():
                        raise ValueError(
                            f"layout {layout.name!r}: contactref ties {element.name!r} pad {ref.pad!r} "
                            f"to signal {signal.name!r}, but the schematic wires it to net {expected_net!r}"
                        )
