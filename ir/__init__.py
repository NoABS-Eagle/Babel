"""Python model of the siskin_spec IR — one module per spec chapter/page.

Built so far:
- Component chapter (spec/README.md #1): units, naming, attr, graphics
  primitives, pad/smd/hole, model3d, pin, symbol, footprint,
  component/gate/device/map, library.
- Schematic chapter (#2), plus the project root: class, part,
  component_instance, pinref, label, net/segment, variant, note,
  schematic, module, module_instance, project.
- Board chapter (#3): stack (the formula, layout.md), layer_declaration,
  rules, via, plating, element, contactref, signal, layout.

Project does not carry layouts yet — that wiring (plus cross-checking a
board against its schematic: contactref<->pinref/map agreement, ghost
elements, class-pool resolution for track/via width) is the next piece
of work before main.py can produce real output.
"""

from .attr import Attr
from .class_ import Class
from .component import Component, Device, Gate, Map
from .component_instance import ComponentInstance
from .contactref import ContactRef
from .element import Element, Side
from .footprint import Footprint
from .graphics import Arc, Line, Polygon, Shape, Text, Vertex
from .label import Label, LabelStyle
from .layer_declaration import LayerDeclaration
from .layout import Layout
from .library import Library
from .model3d import Model3D
from .module import Module
from .module_instance import ModuleInstance, expand_designator, expand_net_name
from .net import Net, Segment
from .note import Note
from .pad import Hole, Pad, Smd
from .part import Part
from .pin import Direction, Pin
from .pinref import PinRef
from .plating import Plating, PlatingArc, PlatingLine
from .project import Project
from .rules import Rules
from .schematic import Schematic
from .signal import Signal
from .stack import Dielectric, copper_layer_numbers, parse_stack
from .symbol import Symbol
from .units import Layer
from .variant import ModuleInstanceOverride, PartOverride, Variant
from .via import Via

__all__ = [
    "Attr",
    "Class",
    "Component",
    "Device",
    "Gate",
    "Map",
    "ComponentInstance",
    "ContactRef",
    "Element",
    "Side",
    "Footprint",
    "Arc",
    "Line",
    "Polygon",
    "Shape",
    "Text",
    "Vertex",
    "Label",
    "LabelStyle",
    "LayerDeclaration",
    "Layout",
    "Library",
    "Model3D",
    "Module",
    "ModuleInstance",
    "expand_designator",
    "expand_net_name",
    "Net",
    "Segment",
    "Note",
    "Hole",
    "Pad",
    "Smd",
    "Part",
    "Direction",
    "Pin",
    "PinRef",
    "Plating",
    "PlatingArc",
    "PlatingLine",
    "Project",
    "Rules",
    "Schematic",
    "Signal",
    "Dielectric",
    "copper_layer_numbers",
    "parse_stack",
    "Symbol",
    "Layer",
    "ModuleInstanceOverride",
    "PartOverride",
    "Variant",
    "Via",
]
