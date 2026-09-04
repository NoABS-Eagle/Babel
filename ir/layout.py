"""<layout> — layout.md. A board: the physical reading of the one shared
schematic. Holds no components or nets directly, only name-references to
them (element.py, signal.py) — connectivity is one-directional, schematic
to board, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .contactref import ContactRef
from .element import Element
from .graphics import Arc, Line, Polygon, Shape, Text
from .layer_declaration import LayerDeclaration
from .pad import Hole
from .note import Note
from .rules import Rules
from .signal import Signal
from .stack import DEFAULT_STACK, copper_layer_numbers, parse_stack
from .units import validate_length

GraphicChild = Line | Arc | Shape | Polygon | Text


@dataclass
class Layout:
    name: str
    stack: str = DEFAULT_STACK
    mask_expansion: int = 50
    paste_expansion: int = 0
    rules: Rules | None = None
    layers: list[LayerDeclaration] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    graphics: list[GraphicChild] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    holes: list[Hole] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("layout name must not be empty")
        copper, _dielectrics = parse_stack(self.stack)
        self._copper_numbers = set(copper_layer_numbers(len(copper)))

        validate_length(self.mask_expansion, name="mask_expansion")
        validate_length(self.paste_expansion, name="paste_expansion")
        validate_unique_attrs(self.attrs)

        self._validate_layers()
        self._validate_elements()
        self._validate_signals()
        self._validate_graphics()

    def _validate_layers(self) -> None:
        numbers = [ld.number for ld in self.layers]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"layout {self.name!r}: duplicate layer number")
        names = [ld.name.lower() for ld in self.layers if ld.name is not None]
        if len(names) != len(set(names)):
            raise ValueError(f"layout {self.name!r}: duplicate layer name")
        for ld in self.layers:
            if abs(ld.number) < 100 and ld.number not in self._copper_numbers:
                raise ValueError(
                    f"layout {self.name!r}: <layer number={ld.number}> names a copper "
                    f"layer absent from the stack {self.stack!r}"
                )

    def _validate_elements(self) -> None:
        names = [e.name.lower() for e in self.elements]
        if len(names) != len(set(names)):
            raise ValueError(f"layout {self.name!r}: duplicate element name")

    def _validate_signals(self) -> None:
        names = [s.name.lower() for s in self.signals]
        if len(names) != len(set(names)):
            raise ValueError(f"layout {self.name!r}: duplicate signal name")

        known_elements = {e.name.lower() for e in self.elements}
        seen_pads: dict[str, set[tuple[str, str]]] = {}
        for signal in self.signals:
            for ref in signal.contactrefs:
                if ref.element.lower() not in known_elements:
                    raise ValueError(
                        f"layout {self.name!r}, signal {signal.name!r}: "
                        f"contactref names unknown element {ref.element!r}"
                    )
                key = (ref.element.lower(), ref.pad.lower())
                for other_signal, pads in seen_pads.items():
                    if key in pads:
                        raise ValueError(
                            f"layout {self.name!r}: pad {ref.pad!r} of element {ref.element!r} "
                            f"belongs to both {other_signal!r} and {signal.name!r} — one pad, one net"
                        )
                seen_pads.setdefault(signal.name, set()).add(key)

            for item in signal.copper:
                layer = getattr(item, "layer", None)
                if layer is not None and layer not in self._copper_numbers:
                    raise ValueError(
                        f"layout {self.name!r}, signal {signal.name!r}: copper on layer {layer}, "
                        f"absent from the stack {self.stack!r}"
                    )

    def _validate_graphics(self) -> None:
        for item in self.graphics:
            if isinstance(item, (Line, Arc, Polygon)) and not item.anti:
                layer = item.layer
                if layer is not None and abs(layer) < 100:
                    raise ValueError(
                        f"layout {self.name!r}: a copper conductor must live inside a <signal>, "
                        f"found one as a direct child of the board (layer {layer})"
                    )
