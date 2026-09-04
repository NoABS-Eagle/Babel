"""Python model of the siskin_spec IR — one module per spec chapter/page.

Built so far: units, naming, attr, graphics primitives, pad/smd/hole,
model3d, pin, symbol, footprint, component/gate/device/map, library — the
whole "Component" chapter (spec/README.md #1). Schematic and board chapters
are not implemented yet.
"""

from .attr import Attr
from .component import Component, Device, Gate, Map
from .footprint import Footprint
from .graphics import Arc, Line, Polygon, Shape, Text, Vertex
from .library import Library
from .model3d import Model3D
from .pad import Hole, Pad, Smd
from .pin import Direction, Pin
from .symbol import Symbol
from .units import Layer

__all__ = [
    "Attr",
    "Component",
    "Device",
    "Gate",
    "Map",
    "Footprint",
    "Arc",
    "Line",
    "Polygon",
    "Shape",
    "Text",
    "Vertex",
    "Library",
    "Model3D",
    "Hole",
    "Pad",
    "Smd",
    "Direction",
    "Pin",
    "Symbol",
    "Layer",
]
