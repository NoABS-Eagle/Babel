"""IR -> `.siprj` / `.silib` XML. The write half of the format: every
function here mirrors one `spec/*.md` page's "Атрибуты" table and "Дети"
list — tag names and attribute names are transcribed from the page, not
guessed from the Python field names (they happen to agree almost always,
which is exactly why the dataclasses were written the way they are).

Layer encoding: the dataclasses split a layer into a signed int plus an
`anti: bool` for convenience, but the file format writes them as one
string, `[!][-]N` (layer-model.md) — `_layer_attr` below re-combines them.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .attr import Attr
from .class_ import Class
from .component import Component, Device, Gate, Map
from .component_instance import ComponentInstance
from .contactref import ContactRef
from .element import Element as BoardElement
from .footprint import Footprint
from .graphics import Arc, Line, Polygon, Shape, Text, Vertex
from .label import Label
from .layer_declaration import LayerDeclaration
from .layout import Layout
from .library import Library
from .module import Module
from .module_instance import ModuleInstance
from .net import Net, Segment
from .note import Note
from .pad import Hole, Pad, Smd
from .part import Part
from .pin import Pin
from .pinref import PinRef
from .plating import Plating, PlatingArc, PlatingLine
from .project import Project
from .rules import Rules
from .schematic import Schematic
from .signal import Signal
from .symbol import Symbol
from .variant import ModuleInstanceOverride, PartOverride, Variant
from .via import Via

FORMAT_VERSION = (1, 0)


def _s(v) -> str:
    return str(v)


def _layer_attr(layer: int, anti: bool = False) -> str:
    return f"{'!' if anti else ''}{layer}"


def _set_if(el: ET.Element, name: str, value) -> None:
    if value is not None:
        el.set(name, _s(value))


def _attrs(parent: ET.Element, attrs: list[Attr]) -> None:
    for a in attrs:
        ET.SubElement(parent, "attr", name=a.name, value=a.value)


# --------------------------------------------------------------- graphics

def _vertex(v: Vertex) -> ET.Element:
    el = ET.Element("vertex", x=_s(v.x), y=_s(v.y))
    _set_if(el, "curve", v.curve)
    return el


def _line(g: Line) -> ET.Element:
    el = ET.Element("line", x1=_s(g.x1), y1=_s(g.y1), x2=_s(g.x2), y2=_s(g.y2), width=_s(g.width))
    if g.layer is not None:
        el.set("layer", _layer_attr(g.layer, g.anti))
    return el


def _arc(g: Arc) -> ET.Element:
    el = ET.Element("arc", x1=_s(g.x1), y1=_s(g.y1), x2=_s(g.x2), y2=_s(g.y2),
                     curve=_s(g.curve), width=_s(g.width))
    if g.layer is not None:
        el.set("layer", _layer_attr(g.layer, g.anti))
    return el


def _shape(g: Shape) -> ET.Element:
    return ET.Element(
        "shape", x=_s(g.x), y=_s(g.y), w=_s(g.w), h=_s(g.h),
        layer=_layer_attr(g.layer, g.anti), rot=_s(g.rot),
        roundness=_s(g.roundness), outline=_s(g.outline),
    )


def _polygon(g: Polygon, in_signal: bool = False) -> ET.Element:
    el = ET.Element("polygon", layer=_layer_attr(g.layer, g.anti), width=_s(g.width), fill=_s(g.fill))
    if in_signal:
        # polygon.md #вне-сигнала-эти-три-не-пишутся: rank/thermals/clearance
        # answer questions ("who wins the overlap", "spokes at whose pads")
        # that only exist for real copper inside a <signal>.
        el.set("rank", _s(g.rank))
        el.set("thermals", _s(g.thermals))
        el.set("clearance", _s(g.clearance))
    for v in g.vertices:
        el.append(_vertex(v))
    return el


def _text(g: Text) -> ET.Element:
    el = ET.Element(
        "text", x=_s(g.x), y=_s(g.y), height=_s(g.height),
        layer=_layer_attr(g.layer, g.anti), align=g.align, rot=_s(g.rot),
        mirror=_s(g.mirror), ratio=_s(g.ratio),
    )
    _set_if(el, "width", g.width)
    el.text = g.content
    return el


_GRAPHIC_WRITERS = {Line: _line, Arc: _arc, Shape: _shape, Polygon: _polygon, Text: _text}


def _graphic(g) -> ET.Element:
    return _GRAPHIC_WRITERS[type(g)](g)


# ------------------------------------------------------------ component pool

def _pin(p: Pin) -> ET.Element:
    return ET.Element(
        "pin", name=p.name, direction=p.direction.value, x=_s(p.x), y=_s(p.y),
        rot=_s(p.rot), length=_s(p.length), pinvis=_s(p.pinvis), padvis=_s(p.padvis),
    )


def _symbol(s: Symbol) -> ET.Element:
    el = ET.Element("symbol")
    if s.name is not None:
        el.set("name", s.name)
        if s.library is not None:
            el.set("library", s.library)
    for g in s.graphics:
        el.append(_graphic(g))
    for p in s.pins:
        el.append(_pin(p))
    return el


def _pad(p: Pad) -> ET.Element:
    el = ET.Element(
        "pad", name=p.name, x=_s(p.x), y=_s(p.y), width=_s(p.width), height=_s(p.height),
        drill=_s(p.drill), rot=_s(p.rot), roundness=_s(p.roundness),
        thermals=_s(p.thermals), stopmask=_s(p.stopmask),
    )
    _set_if(el, "inner_dia", p.inner_dia)
    return el


def _smd(p: Smd) -> ET.Element:
    return ET.Element(
        "smd", name=p.name, x=_s(p.x), y=_s(p.y), width=_s(p.width), height=_s(p.height),
        layer=_s(p.layer), rot=_s(p.rot), roundness=_s(p.roundness),
        thermals=_s(p.thermals), stopmask=_s(p.stopmask), paste=_s(p.paste),
        virtual=_s(int(p.virtual)),
    )


def _hole(h: Hole) -> ET.Element:
    return ET.Element("hole", x=_s(h.x), y=_s(h.y), drill=_s(h.drill))


def _model3d(m) -> ET.Element:
    el = ET.Element("model3d", tx=_s(m.tx), ty=_s(m.ty), tz=_s(m.tz), rx=_s(m.rx), ry=_s(m.ry), rz=_s(m.rz))
    _set_if(el, "key", m.key)
    return el


def _footprint(f: Footprint) -> ET.Element:
    el = ET.Element("footprint", name=f.name)
    if f.library is not None:
        el.set("library", f.library)
    for g in f.graphics:
        el.append(_graphic(g))
    for pad in f.pads:
        el.append(_smd(pad) if isinstance(pad, Smd) else _pad(pad))
    for h in f.holes:
        el.append(_hole(h))
    for m in f.models:
        el.append(_model3d(m))
    return el


def _gate(g: Gate) -> ET.Element:
    return ET.Element("gate", name=g.name, symbol=g.symbol, x=_s(g.x), y=_s(g.y))


def _map(m: Map) -> ET.Element:
    return ET.Element("map", gate=m.gate, pin=m.pin, pad=m.pad)


def _device(d: Device) -> ET.Element:
    el = ET.Element("device", name=d.name, footprint=d.footprint)
    for m in d.maps:
        el.append(_map(m))
    _attrs(el, d.attrs)
    return el


def _component(c: Component) -> ET.Element:
    el = ET.Element("component", name=c.name, prefix=c.prefix)
    if c.library is not None:
        el.set("library", c.library)
    for g in c.gates:
        el.append(_gate(g))
    for d in c.devices:
        el.append(_device(d))
    _attrs(el, c.attrs)
    return el


# --------------------------------------------------------------- schematic

def _part(p: Part) -> ET.Element:
    el = ET.Element("part", name=p.name, component=p.component, library=p.library)
    if p.device is not None:
        el.set("device", p.device)
    _attrs(el, p.attrs)
    return el


def _compinst(ci: ComponentInstance) -> ET.Element:
    el = ET.Element("compinst", part=ci.part, gate=ci.gate, x=_s(ci.x), y=_s(ci.y),
                     rot=_s(ci.rot), mirror=_s(ci.mirror))
    for t in ci.texts:
        el.append(_text(t))
    return el


def _pinref(r: PinRef) -> ET.Element:
    el = ET.Element("pinref", inst=r.inst, pin=r.pin)
    if r.gate is not None:
        el.set("gate", r.gate)
    return el


def _label(l: Label) -> ET.Element:
    el = ET.Element(
        "label", x=_s(l.x), y=_s(l.y), height=_s(l.height), align=l.align,
        layer=_s(l.layer), style=l.style.value, rot=_s(l.rot), mirror=_s(l.mirror), ratio=_s(l.ratio),
    )
    _set_if(el, "width", l.width)
    return el


def _segment(seg: Segment) -> ET.Element:
    el = ET.Element("segment")
    for line in seg.lines:
        el.append(_line(line))
    for ref in seg.pinrefs:
        el.append(_pinref(ref))
    for lbl in seg.labels:
        el.append(_label(lbl))
    return el


def _net(n: Net) -> ET.Element:
    el = ET.Element("net", name=n.name)
    for seg in n.segments:
        el.append(_segment(seg))
    _attrs(el, n.attrs)
    return el


def _part_override(o: PartOverride) -> ET.Element:
    el = ET.Element("part", name=o.name)
    if o.exclude:
        el.set("exclude", _s(o.exclude))
    _attrs(el, o.attrs)
    return el


def _modinst_override(o: ModuleInstanceOverride) -> ET.Element:
    el = ET.Element("modinst", name=o.name)
    if o.variant is not None:
        el.set("variant", o.variant)
    if o.exclude:
        el.set("exclude", _s(o.exclude))
    return el


def _variant(v: Variant) -> ET.Element:
    el = ET.Element("variant", name=v.name)
    for p in v.parts:
        el.append(_part_override(p))
    for m in v.modules:
        el.append(_modinst_override(m))
    return el


def _note(n: Note) -> ET.Element:
    el = ET.Element(
        "note", x=_s(n.x), y=_s(n.y), w=_s(n.w), h=_s(n.h), height=_s(n.height),
        layer=_s(n.layer), rot=_s(n.rot), mirror=_s(n.mirror), ratio=_s(n.ratio),
    )
    el.text = n.content
    return el


def _modinst(m: ModuleInstance) -> ET.Element:
    el = ET.Element("modinst", module=m.module, name=m.name, x=_s(m.x), y=_s(m.y),
                     rot=_s(m.rot), mirror=_s(m.mirror))
    if m.offset is not None:
        el.set("offset", _s(m.offset))
    if m.variant is not None:
        el.set("variant", m.variant)
    _attrs(el, m.attrs)
    for t in m.texts:
        el.append(_text(t))
    return el


def _schematic(s: Schematic) -> ET.Element:
    el = ET.Element("schematic")
    _attrs(el, s.attrs)
    for p in s.parts:
        el.append(_part(p))
    for ci in s.instances:
        el.append(_compinst(ci))
    for m in s.modinsts:
        el.append(_modinst(m))
    for n in s.nets:
        el.append(_net(n))
    for v in s.variants:
        el.append(_variant(v))
    for g in s.graphics:
        el.append(_graphic(g))
    for n in s.notes:
        el.append(_note(n))
    return el


def _module(m: Module) -> ET.Element:
    el = ET.Element("module", name=m.name, prefix=m.prefix)
    el.append(_symbol(m.symbol))
    el.append(_schematic(m.schematic))
    return el


# ------------------------------------------------------------------- board

def _rules(r: Rules) -> ET.Element:
    el = ET.Element("rules")
    for name in ("clearance", "edge_clearance", "min_width", "min_drill", "min_annular", "min_drill_web"):
        _set_if(el, name, getattr(r, name))
    return el


def _class(c: Class) -> ET.Element:
    el = ET.Element("class", name=c.name)
    _set_if(el, "width", c.width)
    _set_if(el, "clearance", c.clearance)
    _set_if(el, "drill", c.drill)
    _attrs(el, c.attrs)
    return el


def _layer_declaration(ld: LayerDeclaration) -> ET.Element:
    el = ET.Element("layer", number=_s(ld.number), visible=_s(ld.visible))
    _set_if(el, "name", ld.name)
    _set_if(el, "color", ld.color)
    return el


def _via(v: Via) -> ET.Element:
    el = ET.Element("via", x=_s(v.x), y=_s(v.y), drill=_s(v.drill), diameter=_s(v.diameter))
    _set_if(el, "inner_dia", v.inner_dia)
    _attrs(el, v.attrs)
    return el


def _plating_line(p: PlatingLine) -> ET.Element:
    return ET.Element("line", x1=_s(p.x1), y1=_s(p.y1), x2=_s(p.x2), y2=_s(p.y2))


def _plating_arc(p: PlatingArc) -> ET.Element:
    return ET.Element("arc", x1=_s(p.x1), y1=_s(p.y1), x2=_s(p.x2), y2=_s(p.y2), curve=_s(p.curve))


def _plating(p: Plating) -> ET.Element:
    el = ET.Element("plating", land=_s(p.land))
    _set_if(el, "inner_land", p.inner_land)
    for seg in p.path:
        el.append(_plating_arc(seg) if isinstance(seg, PlatingArc) else _plating_line(seg))
    return el


def _contactref(c: ContactRef) -> ET.Element:
    return ET.Element("contactref", element=c.element, pad=c.pad)


_COPPER_WRITERS = {Line: _line, Arc: _arc, Plating: _plating, Via: _via}


def _signal(sig: Signal) -> ET.Element:
    el = ET.Element("signal", name=sig.name)
    for ref in sig.contactrefs:
        el.append(_contactref(ref))
    for item in sig.copper:
        if isinstance(item, Polygon):
            el.append(_polygon(item, in_signal=True))
        else:
            el.append(_COPPER_WRITERS[type(item)](item))
    return el


def _board_element(e: BoardElement) -> ET.Element:
    el = ET.Element("element", name=e.name, x=_s(e.x), y=_s(e.y), rot=_s(e.rot), side=e.side.value)
    if e.exclude:
        el.set("exclude", _s(e.exclude))
    for t in e.texts:
        el.append(_text(t))
    return el


def _layout(lay: Layout) -> ET.Element:
    el = ET.Element("layout", name=lay.name, stack=lay.stack,
                     mask_expansion=_s(lay.mask_expansion), paste_expansion=_s(lay.paste_expansion))
    _attrs(el, lay.attrs)
    if lay.rules is not None:
        el.append(_rules(lay.rules))
    for ld in lay.layers:
        el.append(_layer_declaration(ld))
    for e in lay.elements:
        el.append(_board_element(e))
    for sig in lay.signals:
        el.append(_signal(sig))
    for g in lay.graphics:
        el.append(_graphic(g))
    for n in lay.notes:
        el.append(_note(n))
    for h in lay.holes:
        el.append(_hole(h))
    return el


# ----------------------------------------------------------------- roots

def _pool(parent: ET.Element, symbols, footprints, components) -> None:
    for s in symbols:
        parent.append(_symbol(s))
    for f in footprints:
        parent.append(_footprint(f))
    for c in components:
        parent.append(_component(c))


def project_to_xml(project: Project) -> ET.Element:
    major, minor = FORMAT_VERSION
    root = ET.Element("project", name=project.name, version=f"{major}.{minor}")
    _pool(root, project.symbols, project.footprints, project.components)
    for c in project.classes:
        root.append(_class(c))
    for ld in project.layers:
        root.append(_layer_declaration(ld))
    for m in project.modules:
        root.append(_module(m))
    root.append(_schematic(project.schematic))
    for lay in project.layouts:
        root.append(_layout(lay))
    return root


def library_to_xml(library: Library) -> ET.Element:
    major, minor = FORMAT_VERSION
    root = ET.Element("library", name=library.name, version=f"{major}.{minor}")
    _attrs(root, library.attrs)
    _pool(root, library.symbols, library.footprints, library.components)
    return root


def _write(root: ET.Element, path: Path) -> None:
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_project(project: Project, path: Path) -> None:
    _write(project_to_xml(project), path)


def write_library(library: Library, path: Path) -> None:
    _write(library_to_xml(library), path)
