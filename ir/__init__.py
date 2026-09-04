"""Python model of the siskin_spec IR — one module per spec chapter/page.

Built so far:
- Component chapter (spec/README.md #1): units, naming, attr, graphics
  primitives, pad/smd/hole, model3d, pin, symbol, footprint,
  component/gate/device/map, library.
- Schematic chapter (#2), plus the project root: class, part,
  component_instance, pinref, label, net/segment, variant, note,
  schematic, module, module_instance, project.

Board chapter (#3) is not implemented yet — a Project is schematic-only
until <layout> exists, so main.py still cannot produce real output for
any tool that requires a board (all three, per
project_import_requirements_v2).
"""

from .attr import Attr
from .class_ import Class
from .component import Component, Device, Gate, Map
from .component_instance import ComponentInstance
from .footprint import Footprint
from .graphics import Arc, Line, Polygon, Shape, Text, Vertex
from .label import Label, LabelStyle
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
from .project import Project
from .schematic import Schematic
from .symbol import Symbol
from .units import Layer
from .variant import ModuleInstanceOverride, PartOverride, Variant

__all__ = [
    "Attr",
    "Class",
    "Component",
    "Device",
    "Gate",
    "Map",
    "ComponentInstance",
    "Footprint",
    "Arc",
    "Line",
    "Polygon",
    "Shape",
    "Text",
    "Vertex",
    "Label",
    "LabelStyle",
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
    "Project",
    "Schematic",
    "Symbol",
    "Layer",
    "ModuleInstanceOverride",
    "PartOverride",
    "Variant",
]
