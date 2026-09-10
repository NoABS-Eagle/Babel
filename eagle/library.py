"""Eagle `<library>` (standalone .lbr, or one entry of a project's embedded
<libraries>) -> IR `Symbol` / `Footprint` / `Component` pools.

conversion-eagle.md #библиотеки is the contract this file implements:
gates transfer 1:1, `nc` pins become `pas`, octagonal/offset pads become
round, and — the one real piece of engineering here — a deviceset that
uses Eagle `<technology>` splits into one `Component` per technology
name, because the IR has no concept of `technology` at all.
"""

from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path

from ir.attr import Attr
from ir.component import Component, Device, Gate, Map
from ir.footprint import Footprint
from ir import model3d
from ir.model3d import Model3D
from ir.pad import Hole, Pad, Smd
from ir.pin import Direction, Pin
from ir.symbol import Symbol

from . import geometry as geo

_DIRECTION_MAP = {
    "in": Direction.IN,
    "out": Direction.OUT,
    "io": Direction.IO,
    "pas": Direction.PASSIVE,
    "pwr": Direction.POWER,
    "sup": Direction.SUPPLY,
    "oc": Direction.OPEN_COLLECTOR,
    "hiz": Direction.HIZ,
    "nc": Direction.PASSIVE,  # pin.md #вывода-nc-не-существует — but a library
    # that already drew one is not ours to judge (conversion-eagle.md
    # #библиотеки): keep the pin, take the most neutral direction, log it.
}
_PIN_LENGTH = {"point": 0, "short": 2540, "middle": 5080, "long": 7620}
_PIN_VISIBLE = {"off": (0, 0), "pin": (1, 0), "pad": (0, 1), "both": (1, 1)}


def convert_pin(el) -> Pin:
    direction_s = el.get("direction", "io")
    direction = _DIRECTION_MAP[direction_s]
    _mirror, rot = geo.angle(el.get("rot"))
    length = _PIN_LENGTH[el.get("length", "long")]
    pinvis, padvis = _PIN_VISIBLE[el.get("visible", "both")]
    return Pin(
        name=el.get("name"),
        direction=direction,
        x=geo.um(el.get("x")),
        y=geo.um(el.get("y")),
        rot=rot,
        length=length,
        pinvis=pinvis,
        padvis=padvis,
    )


_GEOMETRY_CONVERTERS = {
    "wire": geo.convert_wire,
    "circle": geo.convert_circle,
    "rectangle": geo.convert_rectangle,
    "text": geo.convert_text,
}


def _convert_geometry_children(el, layer_of, log, is_footprint: bool = False, label: str = "") -> list:
    """Shared symbol/footprint geometry loop — <polygon> is special-cased
    since it alone can degrade to a <line> or vanish (geo.convert_polygon)."""
    graphics = []
    for child in el:
        layer_attr = child.get("layer")
        if is_footprint and layer_attr is not None and int(layer_attr) in range(2, 16):
            # footprint.md: internal copper (2-15) inside a footprint is a
            # format error — real Eagle library authors sometimes draw
            # decorative reinforcement on every possible internal routing
            # layer defensively (seen on connector packages: USB-C,
            # barrel-jack, XT60 — big pads where the author doesn't know
            # the target board's layer count ahead of time). It adds
            # nothing electrically (a <pad> already pierces every copper
            # layer by construction), so it's dropped rather than
            # mis-translated into a made-up non-copper layer via +100.
            log(f"{label}: <{child.tag}> on Eagle internal copper layer {layer_attr} "
                f"has no IR equivalent inside a footprint (footprint.md) — dropped")
            continue
        if child.tag == "polygon":
            result = geo.convert_polygon(child, layer_of, log)
            if result is not None:
                graphics.append(result)
            continue
        conv = _GEOMETRY_CONVERTERS.get(child.tag)
        if conv is not None:
            result = conv(child, layer_of)
            if result is not None:   # короче микрона — см. geo.convert_wire
                graphics.append(result)
    return graphics


def convert_symbol(el, lib_name: str, log) -> Symbol:
    name = el.get("name")
    pins = []
    nc_count = 0
    for pin_el in el.findall("pin"):
        if pin_el.get("direction") == "nc":
            nc_count += 1
        pins.append(convert_pin(pin_el))
    if nc_count:
        log(f"symbol {lib_name}:{name}: {nc_count} pin(s) declared direction=nc "
            "(does not exist in the IR) -> pas")

    graphics = _convert_geometry_children(el, geo.schematic_layer, log, label=f"symbol {lib_name}:{name}")

    frame_el = el.find("frame")
    if frame_el is not None:
        graphics.append(_synth_frame_shape(frame_el, lib_name, name, log))

    return Symbol(name=name, library=lib_name, pins=pins, graphics=graphics)


_FRAME_DEFAULT_BORDER = 1000  # µm — frame.md gives no width; Eagle's native
# <frame> tag doesn't carry one either (the real border/grid ships as the
# symbol's own <wire> children, imported alongside as ordinary graphics).
# This shape only exists to satisfy is_frame's "<shape> on layer 98" rule.


def _synth_frame_shape(frame_el, lib_name: str, sym_name: str, log):
    from ir.graphics import Shape

    x1, y1 = geo.um(frame_el.get("x1")), geo.um(frame_el.get("y1"))
    x2, y2 = geo.um(frame_el.get("x2")), geo.um(frame_el.get("y2"))
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    w, h = abs(x2 - x1), abs(y2 - y1)
    log(f"symbol {lib_name}:{sym_name}: synthesized <shape layer=98> from Eagle's "
        f"native <frame> tag for frame recognition (border width defaulted)")
    return Shape(cx, cy, w, h, 98, outline=_FRAME_DEFAULT_BORDER)


def convert_pad(el, footprint_label: str, log, restring=None) -> Pad:
    name = el.get("name")
    drill = geo.um(el.get("drill"))
    shape = el.get("shape", "round")
    rules = restring if restring is not None else geo.Restring()
    diameter_s = el.get("diameter")
    if diameter_s is None or float(diameter_s) == 0.0:
        diameter = rules.diameter("pad_outer", drill)
        if not rules.from_rules:
            log(f"{footprint_label}: pad {name!r} has Eagle AUTO diameter and there is no board "
                f"beside this library to read the restring rules from — Eagle's own defaults "
                f"give {diameter} um")
    else:
        diameter = geo.um(diameter_s)
    # An explicit `diameter` names the OUTER ring only; the inner one is
    # always computed (geo.Restring). Written only when it really differs.
    inner = rules.diameter("pad_inner", drill)
    inner_dia = inner if inner != diameter else None
    roundness = 0 if shape == "square" else 100
    if shape in ("octagon", "offset"):
        log(f"{footprint_label}: pad {name!r} shape {shape!r} has no IR equivalent -> round")
    elif shape == "long":
        log(f"{footprint_label}: pad {name!r} shape 'long' elongation is design-rule "
            f"dependent; using Eagle's default 2x diameter")
        diameter_long = diameter * 2
        _mirror, rot = geo.angle(el.get("rot"))
        return Pad(name, geo.um(el.get("x")), geo.um(el.get("y")), diameter, diameter_long,
                   drill, rot=rot, roundness=100, inner_dia=inner_dia)
    _mirror, rot = geo.angle(el.get("rot"))
    return Pad(name, geo.um(el.get("x")), geo.um(el.get("y")), diameter, diameter, drill,
               rot=rot, roundness=roundness, inner_dia=inner_dia)


def convert_smd(el, layer_of) -> Smd:
    signed, _anti = geo._split(layer_of(int(el.get("layer"))))
    _mirror, rot = geo.angle(el.get("rot"))
    stop = el.get("stop", "yes") != "no"
    cream = el.get("cream", "yes") != "no"
    thermals = 0 if el.get("thermals", "yes") == "no" else 100
    return Smd(
        name=el.get("name"),
        x=geo.um(el.get("x")),
        y=geo.um(el.get("y")),
        width=geo.um(el.get("dx")),
        height=geo.um(el.get("dy")),
        layer=signed,
        rot=rot,
        roundness=int(el.get("roundness", "0")),
        thermals=thermals,
        stopmask=1 if stop else 0,
        paste=1 if cream else 0,
    )


def convert_hole(el) -> Hole:
    return Hole(geo.um(el.get("x")), geo.um(el.get("y")), geo.um(el.get("drill")))


# ---------------------------------------------------------------- 3D models

_MODEL3D_RE = re.compile(r"<!--3d:(.*?)-->", re.DOTALL)


def _parse_3d_records(description: str | None) -> list[dict]:
    """NoABS.Eagle3d — conversion-eagle.md #3d-модели: placement metadata
    lives inside a footprint's own `<description>`, as one or more HTML
    comments `<!--3d:{...}-->` (model3d.md allows several per footprint,
    one per dispatch `key`), sitting alongside whatever other HTML text
    Eagle's own description editor put there. Only this comment survives
    into IR — `<description>` otherwise never crosses into a project at
    all (library.md #описание-не-переезжает-в-проект)."""
    if not description:
        return []
    return [json.loads(m) for m in _MODEL3D_RE.findall(description)]


def _convert_3d_models(description: str | None, lib_name: str, footprint_name: str,
                        models_dir: Path | None, log) -> list[Model3D]:
    """conversion-eagle.md #3d-модели: metadata without a matching file is
    logged and dropped — "корпус переносится без модели" — never attached
    half-formed. `models_dir` is None when there's no project context to
    look in at all (a standalone `.lbr`), which is the same outcome by the
    same rule, just silent: nowhere to look isn't a missing-file error."""
    models = []
    for rec in _parse_3d_records(description):
        key = rec.get("key")
        model_file = model3d.find_file(models_dir, footprint_name, key)
        if model_file is None:
            if models_dir is not None:
                log(f"footprint {lib_name}:{footprint_name}: 3D placement metadata found, but no "
                    f"{model3d.base_name(footprint_name, key)}.step/.stp in {models_dir} — footprint "
                    "carries over without this model (conversion-eagle.md #3d-модели)")
            continue
        # **The angles are converted, not copied** — Eagle's 3D frame is
        # Y-up, the IR's is Z-up (geo.eagle_to_mcad_rot). Eagle's own
        # everyday `rx=90` means "already upright" and has to arrive as
        # zero; carried over as 90 it tips every model onto its side.
        rx, ry, rz = geo.eagle_to_mcad_rot(
            float(rec["rx"]), float(rec["ry"]), float(rec["rz"]))
        models.append(Model3D(
            key=key,
            tx=geo.um(str(rec["tx"])), ty=geo.um(str(rec["ty"])), tz=geo.um(str(rec["tz"])),
            rx=geo._signed_mdeg(str(rx)),
            ry=geo._signed_mdeg(str(ry)),
            rz=geo._signed_mdeg(str(rz)),
        ))
    return models


def convert_footprint(el, lib_name: str, log, models_dir: Path | None = None,
                       restring=None) -> Footprint:
    name = el.get("name")
    label = f"footprint {lib_name}:{name}"
    layer_of = partial(geo.footprint_layer, log=log)

    pads: list[Pad | Smd] = []
    for pad_el in el.findall("pad"):
        pads.append(convert_pad(pad_el, label, log, restring))
    for smd_el in el.findall("smd"):
        pads.append(convert_smd(smd_el, layer_of))

    holes = [convert_hole(h) for h in el.findall("hole")]

    graphics = _convert_geometry_children(el, layer_of, log, is_footprint=True, label=label)

    desc_el = el.find("description")
    models = _convert_3d_models(desc_el.text if desc_el is not None else None, lib_name, name, models_dir, log)

    return Footprint(name=name, library=lib_name, graphics=graphics, pads=pads, holes=holes, models=models)


def _technology_names(deviceset_el) -> list[str]:
    names: set[str] = set()
    for dev_el in deviceset_el.find("devices"):
        techs_el = dev_el.find("technologies")
        if techs_el is None:
            names.add("")
            continue
        for t in techs_el.findall("technology"):
            names.add(t.get("name", ""))
    return sorted(names) if names != {""} else [""]


def _technology_attrs(dev_el, tech_name: str, log) -> list[Attr]:
    techs_el = dev_el.find("technologies")
    if techs_el is None:
        return []
    for t in techs_el.findall("technology"):
        if t.get("name", "") == tech_name:
            return [geo.safe_attr(a.get("name"), a.get("value", ""), log) for a in t.findall("attribute")]
    return []


def convert_deviceset(el, lib_name: str, footprints_by_name: dict[str, Footprint], log) -> list[Component]:
    """One Eagle `<deviceset>` -> one or more IR `Component`s — one per
    distinct technology name (conversion-eagle.md #библиотеки). A device
    with no `package` attribute at all carries no footprint in Eagle
    (frames, power symbols) and contributes no IR `Device`."""
    base_name = el.get("name")
    prefix = el.get("prefix", "PART") or "PART"

    gates = [
        Gate(symbol=g.get("symbol"), name=g.get("name", ""), x=geo.um(g.get("x")), y=geo.um(g.get("y")))
        for g in el.find("gates")
    ]

    technologies = _technology_names(el)
    components: list[Component] = []
    for tech in technologies:
        comp_name = base_name if tech == "" else f"{base_name}{tech}"
        devices: list[Device] = []
        for dev_el in el.find("devices"):
            package = dev_el.get("package")
            if not package:
                continue  # no footprint at all -> not a placeable device (device.md)
            techs_el = dev_el.find("technologies")
            dev_techs = _technology_names_of(techs_el)
            if tech not in dev_techs:
                continue  # this technology wasn't offered on this footprint
            footprint = footprints_by_name.get(package.lower())
            if footprint is None:
                raise ValueError(f"deviceset {lib_name}:{base_name}: device references "
                                  f"unknown package {package!r}")
            connects_el = dev_el.find("connects")
            maps = [
                Map(pin=c.get("pin"), pad=c.get("pad"), gate=c.get("gate", ""))
                for c in (connects_el if connects_el is not None else [])
            ]
            attrs = [geo.safe_attr(a.get("name"), a.get("value", ""), log) for a in dev_el.findall("attribute")]
            attrs += _technology_attrs(dev_el, tech, log)
            devices.append(Device(footprint=package, name=dev_el.get("name", ""), maps=maps, attrs=attrs))

        if not devices and technologies != [""]:
            continue  # this technology name produced no usable device -> no component
        components.append(Component(name=comp_name, gates=gates, prefix=prefix, library=lib_name, devices=devices))

    if len(technologies) > 1:
        log(f"deviceset {lib_name}:{base_name}: split into {len(components)} components "
            f"by technology: {', '.join(c.name for c in components)}")
    return components


def _technology_names_of(techs_el) -> set[str]:
    if techs_el is None:
        return {""}
    names = {t.get("name", "") for t in techs_el.findall("technology")}
    return names or {""}


def convert_library(
    lib_el, log, models_dir: Path | None = None, restring=None
) -> tuple[list[Symbol], list[Footprint], list[Component], dict[tuple[str, str], bool]]:
    """A whole `<library>` element -> its three pools, in dependency order
    (footprints before devicesets, which reference them by name), plus a
    (library, component) -> `uservalue` side table — conversion-eagle.md
    #атрибуты needs it to know whether a part with no literal `value=`
    shows blank (`uservalue="yes"`: the user hasn't typed one in yet,
    e.g. a solder-jumper's designation) or the deviceset+device name
    (`uservalue="no"`, Eagle's own default: an IC, a connector — nothing
    to type, Eagle just labels it). Not modeled as a `Component` field:
    it's consulted once, at import, to bake the resulting string straight
    into the part's own `value` attr (schematic.py's `finalize_parts`),
    which both eagle exporters then just read back uniformly."""
    lib_name = lib_el.get("name")

    symbols_el = lib_el.find("symbols")
    symbols = [convert_symbol(s, lib_name, log) for s in (symbols_el if symbols_el is not None else [])]

    packages_el = lib_el.find("packages")
    footprints = [convert_footprint(p, lib_name, log, models_dir, restring)
                  for p in (packages_el if packages_el is not None else [])]
    footprints_by_name = {f.name.lower(): f for f in footprints}

    components: list[Component] = []
    uservalue_by_key: dict[tuple[str, str], bool] = {}
    devicesets_el = lib_el.find("devicesets")
    for ds_el in (devicesets_el if devicesets_el is not None else []):
        ds_components = convert_deviceset(ds_el, lib_name, footprints_by_name, log)
        uservalue = ds_el.get("uservalue", "no") == "yes"
        for c in ds_components:
            uservalue_by_key[(lib_name.lower(), c.name.lower())] = uservalue
        components.extend(ds_components)

    return symbols, footprints, components, uservalue_by_key
