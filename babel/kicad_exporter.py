"""IR → KiCad .kicad_sym + .pretty exporter."""
import math
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from babel.ir_util import (parse_layer, symbol_pool, component_gates,
                           footprint_pool, model3d_dialog_rotation,
                           resolve_model3d_file, sanitize_filename, arc_mid,
                           instance_footprint)
from babel.kicad_layers import ir_to_kicad
from babel import import_log

_SYM_VERSION = 20251024   # KiCad 10
_FP_VERSION  = 20251024

_PIN_DIR = {
    'in':  'input',
    'out': 'output',
    'io':  'bidirectional',
    'pwr': 'power_in',
    'pas': 'passive',
    'nc':  'no_connect',
    'sup': 'power_in',
}

def _fp_kicad_layer(ln):
    """IR footprint layer attribute -> KiCad layer name, or None (drop).
    Layer projection is the user-editable table in babel/kicad_layers.py.
    Anti layers ('!...') have no KiCad footprint equivalent."""
    try:
        anti, n = parse_layer(ln)
    except (TypeError, ValueError):
        return None
    if anti:
        return None
    return ir_to_kicad(n)


def _f(v):
    """Format a number for s-expression output: fixed-point, no exponent.

    NOT `%g`: (a) below 1e-05 it switches to exponent notation ('1e-06'),
    which kiutils' sexpr tokenizer doesn't recognize as a number — it comes
    back as a STRING and crashes arithmetic downstream (found on a real
    round-trip: float dust from arc mid-point trig emitted as '1e-06');
    (b) it keeps only 6 SIGNIFICANT digits, silently shaving µm off
    coordinates past 1000mm (reachable on wide multi-page tilings).
    """
    r = round(float(v), 6)
    if r == 0:
        return '0'
    return f'{r:.6f}'.rstrip('0').rstrip('.')


def _mm(um):
    """IR µm → mm float for KiCad output."""
    return float(um) / 1000


def _ky(y):
    """IR Y (up, µm) → KiCad Y (down, mm)."""
    return -float(y) / 1000


def model3d_kicad_xyz(m3):
    """IR <model3d> -> the six numbers of a KiCad (model ...) block:
    (tx, ty, tz) mm and (rx, ry, rz) degrees, ready to print.

    Offset: µm -> mm, axes PASS THROUGH unchanged — the model offset lives
    in the model's own MCAD frame (Y up), not the footprint's Y-down 2D
    frame, so there is no Y mirror here (ground truth: maximus DD1 ty=+7
    / XS1 ty=+9.5 visually confirmed in KiCad, 2026-07-15).

    Rotation: ir_util.model3d_dialog_rotation gives the angles a model
    dialog shows (same law in KiCad and in Altium); KiCad's FILE stores them
    NEGATED.
    """
    tx = float(m3.get('tx', 0)) / 1000
    ty = float(m3.get('ty', 0)) / 1000
    tz = float(m3.get('tz', 0)) / 1000
    a, b, g = model3d_dialog_rotation(m3)

    def _n(v):                   # negate for the file, normalize -180..180
        v = -v % 360
        return v - 360 if v > 180 else v
    return tx, ty, tz, _n(a), _n(b), _n(g)


def _q(s):
    # \n escaped: multi-line Eagle description prose rides (descr)/property
    # strings, a literal newline inside a quoted s-expr token is invalid
    return ('"' + str(s).replace('\\', '\\\\').replace('"', '\\"')
                        .replace('\n', '\\n') + '"')


def _eagle_overbar_to_kicad(s):
    """Eagle/IR `!TEXT!` overbar toggle -> KiCad `~{TEXT}`. Inverse of
    kicad_parser._kicad_overbar_to_eagle: `!` toggles overbar on/off, an
    unclosed toggle runs to the end of the string. Applies to every string
    a KiCad consumer RENDERS (pin display names, symbol texts, values) —
    never to identity keys like pin-mapping lookups or pad numbers."""
    if not s or '!' not in s:
        return s
    parts = str(s).split('!')
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 0:
            out.append(seg)
        elif seg:
            out.append('~{' + seg + '}')
    return ''.join(out)


def _justify(align, flip_v=False):
    """IR align ('bottom-left', 'center', 'top-right', ...) → KiCad justify clause.

    IR/Eagle anchor names are '<vertical>-<horizontal>' (vertical/horizontal
    each one of bottom/center/top resp. left/center/right; a lone 'center'
    means both centered). flip_v swaps bottom/top — needed in footprint space,
    where Y is mirrored (_ky) relative to the IR's Y-up convention.
    """
    parts = align.lower().split('-')
    v = parts[0] if len(parts) > 1 else 'center'
    h = parts[-1]
    if flip_v:
        v = {'top': 'bottom', 'bottom': 'top'}.get(v, v)
    kws = [k for k in (h if h in ('left', 'right') else None,
                       v if v in ('top', 'bottom') else None) if k]
    return f' (justify {" ".join(kws)})' if kws else ''


def _norm_text_angle(rot, align):
    """KiCad symbol-field text is never upside down: its angle folds to
    [0, 180) (0/180 -> 0, 90/270 -> 90) and the justify is PRESERVED — only
    the position rotates. Ground truth: the SAME R symbol placed by KiCad at
    rot=180 keeps its library justify verbatim (VALUE bottom-right stays
    'right bottom'); flipping the anchor (horizontal or both) was wrong."""
    return rot % 180, align


def _build_pin_map(fp_el, gate_name=None):
    """pin_name → KiCad pin NUMBER string from <pin-mapping>.

    A multi-pad map (`<map pad="2 4" pin="GND">`, ir_schema.md «один пин ->
    несколько падов») becomes a KiCad 10 STACKED pin: one symbol pin whose
    number is the bracketed pad list `[2,4]` — the user's hand-made ground
    truth on tolmach CRYSTAL-4P-16MHZ, and exactly what kicad_parser.
    _pin_numbers already reads back. Taking only the first pad here (the old
    behavior) silently unlinked every extra pad from the symbol (Z7: pad 4
    lost its GND association).

    For multi-gate components the mapping pins are 'GATE.pin'; pass gate_name
    to strip the matching prefix and return bare pin names.
    """
    if fp_el is None:
        return {}
    pm = fp_el.find('pin-mapping')
    if pm is None:
        return {}
    result = {}
    prefix = f'{gate_name}.' if gate_name else None
    for m in pm.findall('map'):
        pin = m.get('pin', '')
        pads = m.get('pad', '').split()
        if not pin or not pads:
            continue
        pad = pads[0] if len(pads) == 1 else '[' + ','.join(pads) + ']'
        if prefix:
            if pin.startswith(prefix):
                result[pin[len(prefix):]] = pad
        else:
            result[pin] = pad
    return result


# ---------------------------------------------------------------------------
# Symbol geometry
# ---------------------------------------------------------------------------

def _sym_geom(sym_el, pin_to_pad):
    """Yield indented S-expression strings for symbol body elements."""
    pin_seq = 0
    for el in sym_el:
        t = el.tag

        if t == 'line':
            x1 = _f(_mm(el.get('x1'))); y1 = _f(_mm(el.get('y1')))
            x2 = _f(_mm(el.get('x2'))); y2 = _f(_mm(el.get('y2')))
            w  = _f(_mm(el.get('width', '0')))
            yield (f'      (polyline\n'
                   f'        (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type none))\n'
                   f'      )')

        elif t == 'arc':
            # endpoint canon: start/end are the stored endpoints VERBATIM,
            # only the mid point is derived (ir_util.arc_mid)
            x1, y1 = _mm(el.get('x1')), _mm(el.get('y1'))
            x2, y2 = _mm(el.get('x2')), _mm(el.get('y2'))
            mx, my = arc_mid(x1, y1, x2, y2, float(el.get('curve')))
            w      = _f(_mm(el.get('width', '0')))
            yield (f'      (arc (start {_f(x1)} {_f(y1)}) (mid {_f(mx)} {_f(my)}) (end {_f(x2)} {_f(y2)})\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type none))\n'
                   f'      )')

        elif t == 'shape':
            x, y = _mm(el.get('x')), _mm(el.get('y'))
            w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
            rnd  = int(el.get('roundness', 0))
            outline = _mm(el.get('outline', '0'))
            sw   = _f(outline)
            # outline=0 is a SOLID shape (cap plates, mosfet dots): KiCad
            # 'outline' fills with the foreground color, matching svg_renderer
            # (fc=color) and the <polygon> path below. 'background' (pale body
            # color) was wrong here — it left solid shapes looking unfilled.
            fill = 'none' if outline else 'outline'
            if rnd == 100:
                yield (f'      (circle (center {_f(x)} {_f(y)}) (radius {_f(w/2)})\n'
                       f'        (stroke (width {sw}) (type default))\n'
                       f'        (fill (type {fill}))\n'
                       f'      )')
            else:
                x1 = _f(x - w/2); y1 = _f(y + h/2)
                x2 = _f(x + w/2); y2 = _f(y - h/2)
                yield (f'      (rectangle (start {x1} {y1}) (end {x2} {y2})\n'
                       f'        (stroke (width {sw}) (type default))\n'
                       f'        (fill (type {fill}))\n'
                       f'      )')

        elif t == 'polygon':
            # IR <polygon> is closed by definition — REPEAT the first vertex:
            # KiCad has no closed-flag, only first==last (an unclosed filled
            # polyline renders almost right — KiCad fills the implicit
            # closure — but the stroke misses one edge, and OUR OWN importer
            # would decompose it back into loose lines). Fill from the IR
            # fill percent (0 = contour), not a hardcoded solid.
            w = _f(_mm(el.get('width', '0')))
            verts = el.findall('vertex')
            pts = ''.join(f' (xy {_f(_mm(v.get("x","0")))} {_f(_mm(v.get("y","0")))})'
                          for v in (verts + verts[:1] if verts else verts))
            fill_type = 'outline' if int(el.get('fill', '100') or '100') > 0 else 'none'
            yield (f'      (polyline\n'
                   f'        (pts{pts})\n'
                   f'        (stroke (width {w}) (type default))\n'
                   f'        (fill (type {fill_type}))\n'
                   f'      )')

        elif t == 'text':
            text = el.text or ''
            if text.startswith('>'):
                continue
            kx     = _f(_mm(el.get('x')));  ky_ = _f(_mm(el.get('y')))
            sz_f   = _mm(el.get('size', '1270'))
            sz     = _f(sz_f)
            rot    = float(el.get('rot', 0))
            kr     = _f(rot % 360)
            ratio_ = int(el.get('ratio', '8'))
            t      = _f(sz_f * ratio_ / 100)
            b      = ' (bold yes)' if ratio_ >= 15 else ' (bold no)'
            j      = _justify(el.get('align', 'bottom-left'))
            yield (f'      (text {_q(_eagle_overbar_to_kicad(text))} (at {kx} {ky_} {kr})\n'
                   f'        (effects (font (size {sz} {sz}) (thickness {t}){b}){j})\n'
                   f'      )')

        elif t == 'pin':
            name   = el.get('name', '')
            dir_   = _PIN_DIR.get(el.get('direction', 'pas'), 'passive')
            kx     = _f(_mm(el.get('x')))
            ky_    = _f(_mm(el.get('y')))
            rot    = float(el.get('rot', 0))
            ka     = _f(rot % 360)
            length = _f(_mm(el.get('length', '2540')))
            pin_seq += 1
            # pin_to_pad is keyed by the RAW IR name; only the displayed
            # name gets the overbar conversion.
            number = pin_to_pad.get(name, str(pin_seq))
            yield (f'      (pin {dir_} line (at {kx} {ky_} {ka}) (length {length})\n'
                   f'        (name {_q(_eagle_overbar_to_kicad(name))} (effects (font (size 1.27 1.27))))\n'
                   f'        (number {_q(number)} (effects (font (size 1.27 1.27))))\n'
                   f'      )')


# ---------------------------------------------------------------------------
# Symbol export
# ---------------------------------------------------------------------------

def _pin_map_signature(fp_el):
    pm = fp_el.find('pin-mapping')
    return frozenset((m.get('pin'), tuple(sorted((m.get('pad') or '').split())))
                     for m in (pm.findall('map') if pm is not None else []))


def needs_variant_split(comp_el):
    """True when this component CANNOT be one KiCad symbol: its variants
    carry DIVERGENT pin-mappings, and a KiCad pin NUMBER is the only
    symbol<->pad link there is — one shared number set would wire every
    other variant's placement to the wrong pads (maximus U13/U7,
    NSIP83086(V): devices with different pad numbering; export_symbol baked
    fps[0]'s numbers and the RS485 pins came back shifted by one). Per the
    user: such a family exports as a SET OF ATOMIC symbols, one per
    variant, named like the Eagle device (см. variant_symbol_name)."""
    fps = comp_el.findall('footprint')
    if len(fps) < 2:
        return False
    return len({_pin_map_signature(fp) for fp in fps}) > 1


def variant_symbol_name(comp_el, fp_el):
    """Split-symbol name = Eagle device name: component + variant suffix
    (the variant string already carries its own separator, '-SO-20W');
    a variant-less footprint falls back to '-' + footprint name — the same
    convention eagle_exporter._device_names uses."""
    v = fp_el.get('variant')
    return comp_el.get('name', '') + (v if v else '-' + (fp_el.get('name') or ''))


def export_symbol(comp_el, root, lib_name, variant_fp=None, sym_name=None):
    """
    Convert <component> to a KiCad symbol S-expression string.
    Returns None for components with no resolvable symbol.

    variant_fp/sym_name: the per-variant ATOMIC split (needs_variant_split)
    — the symbol is built against exactly this footprint (its pin numbers,
    its Footprint reference, its baked attrs) under the given name.
    """
    gates = component_gates(comp_el)
    pool  = symbol_pool(root)
    gate_syms = [(gn, pool[sn]) for gn, sn in gates if sn in pool]
    if not gate_syms:
        return None

    comp_id = sym_name or comp_el.get('name', '')
    prefix  = comp_el.get('prefix', 'U')
    fps     = [variant_fp] if variant_fp is not None \
        else comp_el.findall('footprint')
    multi   = len(fps) > 1

    # First gate drives symbol-level text/style/visibility (properties live on
    # the parent symbol, shared by all units; per-gate placeholders are
    # duplicated identically across gates, see progress.md item 10).
    sym_el = gate_syms[0][1]

    # The entry NAME, not the IR name: diverging same-named land patterns
    # get an `@N` suffix in the one project library (footprint_pool), and the
    # symbol has to point at the copy this component actually uses.
    _fp_pool = footprint_pool(root)
    fp_ref = f'{lib_name}:{sanitize_filename(_fp_pool.get(id(fps[0]), fps[0].get("name", "")))}'         if len(fps) == 1 else ''
    pin_to_pad = _build_pin_map(fps[0] if fps else None, gate_syms[0][0])

    # Component-level attrs are FAMILY facts — uniform across variants by
    # construction (ir_schema.md I9), so their VALUES ride even on a generic
    # multi-variant symbol (blanking them here made Description vanish from
    # the library projection and resurface as per-instance field overrides
    # on re-import — the sch/board divergence the user caught). What a
    # generic symbol genuinely can't bake is only the variant-DIVERGENT
    # values — the footprint attrs below.
    attrs_el = comp_el.find('attributes')
    attrs = {}
    for a in (attrs_el.findall('attr') if attrs_el is not None else []):
        attrs[a.get('name', '')] = a.get('value', '')

    # Variant attributes (manf#, package, the materialized value, ...) live
    # on the FOOTPRINT in IR (ir_schema.md I9: значения — у варианта, схема
    # с пустыми объявлениями — у компонента). For an ATOMIC KiCad symbol the
    # .kicad_sym itself must carry their VALUES, or every placed instance
    # shows fields the library symbol lacks (KiCad's "not atomic"). Merge by
    # the resolution canon — a variant's non-empty value OVERRIDES the
    # component's empty declaration (setdefault here once let the declared
    # `value=""` swallow the materialized variant value: the closed-loop
    # linter returned IC2 with an empty element value). A multi-footprint
    # component can't bake values (its variants genuinely differ), so its
    # symbol keeps the bare schema exactly as the generic rule prescribes.
    if not multi and fps:
        fp_attrs_el = fps[0].find('attributes')
        for a in (fp_attrs_el.findall('attr') if fp_attrs_el is not None else []):
            n, v = a.get('name', ''), a.get('value', '')
            if v or n not in attrs:
                attrs[n] = v

    datasheet = attrs.pop('datasheet', '')
    value_val = attrs.pop('value', comp_id)

    # Collect style (position, size, align, ratio) from all >XXX placeholder texts
    _def_style = lambda y: {'at': (0, y, 0), 'size': 1.27, 'align': 'bottom-left', 'ratio': 8}
    name_style        = _def_style(2.54)
    value_style       = _def_style(0)
    has_name          = False   # source symbol carries a >NAME placeholder?
    has_value         = False   # ...a >VALUE placeholder?
    placeholder_style = {}   # lowercase attr name → style dict
    for el in sym_el:
        if el.tag != 'text':
            continue
        txt = (el.text or '').strip()
        if not txt.startswith('>'):
            continue
        rot, align = _norm_text_angle(float(el.get('rot', 0)),
                                      el.get('align', 'bottom-left'))
        info = {
            'at':    (_mm(el.get('x', 0)), _mm(el.get('y', 0)), rot),
            'size':  _mm(el.get('size', '1270')),
            'align': align,
            'ratio': int(el.get('ratio', '8')),
        }
        if txt == '>NAME':    name_style, has_name = info, True
        elif txt == '>VALUE': value_style, has_value = info, True
        else:                 placeholder_style[txt[1:].lower()] = info

    def _effects(style):
        sz    = style['size']
        ratio = style.get('ratio', 8)
        s     = _f(sz)
        t     = _f(sz * ratio / 100)
        b     = ' (bold yes)' if ratio >= 15 else ' (bold no)'
        j     = _justify(style['align'])
        return f'(effects (font (size {s} {s}) (thickness {t}){b}){j})'

    def _at(info):
        a = info['at']
        return f'{_f(a[0])} {_f(a[1])} {_f(a[2])}'

    def _prop(name, val, at_str, eff, hide=False):
        result = [f'    (property {_q(name)} {_q(val)}',
                  f'      (at {at_str})',
                  f'      (show_name no)',
                  f'      (do_not_autoplace yes)']
        if hide:
            result.append(f'      (hide yes)')
        result.append(f'      {eff}')
        result.append(f'    )')
        return result

    # Majority vote on pin name / number visibility, across all gates
    show_name = show_num = 0
    for _, gsym in gate_syms:
        for el in gsym:
            if el.tag != 'pin':
                continue
            if el.get('pinvis', '1') == '1': show_name += 1
            else:                            show_name -= 1
            if el.get('padvis', '1') == '1': show_num  += 1
            else:                            show_num  -= 1

    _hidden = {'at': (0, 0, 0), 'size': 1.27, 'align': 'center'}

    # pin_names / pin_numbers blocks. An EMPTY `(pin_numbers)` is what real
    # KiCad never writes (ground truth: absent unless hiding) and what
    # kiutils' Symbol.from_sexpr can't even parse (unconditional item[1] ->
    # IndexError) — emit the block only when it actually hides something.
    pn_lines = ['    (pin_names', '      (offset 1.016)']
    if show_name < 0:
        pn_lines.append('      (hide yes)')
    pn_lines.append('    )')
    pnum_lines = (['    (pin_numbers', '      (hide yes)', '    )']
                  if show_num < 0 else [])

    # Supply component (IR: a `sup`-direction pin, ir_schema.md "Supply
    # symbol") -> KiCad power-flag symbol. `(power global)` is a literal
    # ground-truth copy (testData/multichannel.kicad_sym, written by real
    # KiCad 9); without it the re-imported symbol loses isPower, its pin
    # stops naming the net, and every supply island falls apart into its
    # own N$ net (found by the first project round-trip). The v10
    # `(power local)` counterpart is pending its own ground truth (see
    # kicad_project_exporter module docstring, supply LOCAL TODO).
    is_power = any(el.get('direction') == 'sup'
                   for _, gsym in gate_syms for el in gsym.findall('pin'))
    lines = [f'  (symbol {_q(comp_id)}',
             *(['    (power global)'] if is_power else []),
             *pnum_lines,
             *pn_lines,
             f'    (exclude_from_sim no)',
             f'    (in_bom yes)',
             f'    (on_board yes)',
             f'    (in_pos_files yes)',
             f'    (duplicate_pin_numbers_are_jumpers no)']

    # Power/supply lib symbol: Reference is the KiCad `#`-prefixed hidden
    # field (ground truth: KiCad's own GND carries `#PWR`, hidden — the VALUE
    # is what shows). Without the `#` and the hide, dropping the symbol from
    # the library paints a stray, offset "GND" over the graphic, and the lib
    # Reference disagrees with the schematic instance (which prefixes `#`).
    ref_val = ('#' + prefix if is_power and not prefix.startswith('#')
               else prefix)
    # hide when power (#-ref) OR the source had no >NAME (pin-less parts:
    # fiducials, screws — Eagle never showed a reference for them). Same
    # "no placeholder = not displayed" rule the schematic instance uses.
    lines.extend(_prop('Reference', ref_val, _at(name_style),
                       _effects(name_style), hide=is_power or not has_name))
    # A power symbol's Value IS the net name it drives (KiCad reads it to
    # name the net) — and in IR that name is the `sup` PIN's name, never the
    # component name or its display value (Eagle model: supply device
    # "3.3V" with sup pin VDD_3V3 drives net VDD_3V3; the closed-loop
    # oracle caught the schematic net renamed to "3.3V" while the board
    # kept VDD_3V3). Same rule kicad_project_exporter applies to the
    # placed instances' Value.
    if is_power:
        sup_names = {el.get('name')
                     for _, gsym in gate_syms for el in gsym.findall('pin')
                     if el.get('direction') == 'sup'}
        if len(sup_names) == 1:
            value_val = next(iter(sup_names))
        elif not value_val:
            value_val = comp_id
    # Value visible only when the source symbol SHOWS it (>VALUE
    # placeholder) — no placeholder = not displayed, same rule the
    # schematic-instance emitter already follows; power symbols keep it
    # visible always (Value IS the rail label KiCad renders). Found by the
    # closed-loop linter: an always-visible Value came back to IR as a
    # >VALUE placeholder the original never had.
    lines.extend(_prop('Value',     _eagle_overbar_to_kicad(value_val),
                       _at(value_style), _effects(value_style),
                       hide=not has_value and not is_power))
    lines.extend(_prop('Footprint', fp_ref,    '0 -2.54 0',      _effects(_hidden), hide=True))
    lines.extend(_prop('Datasheet', datasheet, '0 -5.08 0',      _effects(_hidden), hide=True))
    # Attrs: visible if Eagle had a >ATTRNAME placeholder, hidden otherwise
    attrs_lower = {k.lower(): k for k in attrs}
    emitted = set()
    for ph_name, ph_style in placeholder_style.items():
        orig = attrs_lower.get(ph_name, ph_name)
        val  = _eagle_overbar_to_kicad(attrs.get(orig, ''))
        lines.extend(_prop(orig, val, _at(ph_style), _effects(ph_style), hide=False))
        emitted.add(orig)
    for name, val in attrs.items():
        if name not in emitted:
            lines.extend(_prop(name, _eagle_overbar_to_kicad(val), '0 0 0',
                               _effects(_hidden), hide=True))

    for unit, (gname, gsym) in enumerate(gate_syms, start=1):
        lines.append(f'    (symbol {_q(f"{comp_id}_{unit}_1")}')
        lines.extend(_sym_geom(gsym, _build_pin_map(fps[0] if fps else None, gname)))
        lines.append(f'    )')
    lines.append(f'    (embedded_fonts no)')
    lines.append(f'  )')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Footprint export
# ---------------------------------------------------------------------------

def export_footprint(fp_el, model_path=None):
    """Convert <footprint> IR element to a KiCad .kicad_mod S-expression string.

    model_path, if given, is the path (relative to the .kicad_mod file) of an
    already-copied STEP sidecar to reference via a `(model ...)` block.
    """
    fp_id = fp_el.get('name', 'unknown')

    # ir_schema.md I8: the package-description prose is the ordinary
    # `fp_desc` attribute of the variant — routed here into KiCad's native
    # (descr ...) slot VERBATIM. The old first-line/pre-HTML/120-char strip
    # emptied every value that STARTS with a tag ('<a href=…', '<b>…' — the
    # common Eagle library shape), killing fp_desc on the KiCad→IR leg
    # (tolmach: 68 of 77 footprints lost it).
    fa = fp_el.find('attributes')
    desc_a = fa.find('attr[@name="fp_desc"]') if fa is not None else None
    desc = desc_a.get('value', '') if desc_a is not None else ''

    # Collect >NAME / >VALUE positions from any layer
    def _fp_text_style(placeholder, default_y):
        for el in fp_el:
            if el.tag == 'text' and (el.text or '').strip() == placeholder:
                ratio_ = int(el.get('ratio', '8') or '8')
                sz_    = _mm(el.get('size', '1000') or '1000')
                return True, {
                    'x':     _f(_mm(el.get('x', '0'))),
                    'y':     _f(_ky(el.get('y', '0'))),
                    # No negation: IR and KiCad footprint angles share
                    # the same CCW sense despite the Y-flip — proven by
                    # visual ground truth (testData/rtfp, decisions.md
                    # "KiCad: _rot_fp"); inverse of kicad_parser._rot_fp.
                    'rot':   _f(float(el.get('rot', '0') or '0') % 360),
                    'size':  _f(sz_),
                    'thick': _f(sz_ * ratio_ / 100),
                    'bold':  '(bold yes)' if ratio_ >= 15 else '(bold no)',
                    'just':  _justify(el.get('align', 'bottom-left'), flip_v=True),
                }
        sz_ = 1.0
        return False, {'x': '0', 'y': _f(default_y), 'rot': '0',
                       'size': '1', 'thick': _f(sz_ * 8 / 100), 'bold': '(bold no)',
                       'just': ''}

    # KiCad's Footprint Editor always shows a reference/value placeholder
    # regardless of file content, so omitting fp_text doesn't actually hide
    # anything — it just stops us controlling it. Always emit both fields
    # (KLC expects them); hide the ones the source footprint never had a
    # real >NAME/>VALUE for, show+size the ones that did.
    ref_found, rs = _fp_text_style('>NAME',  -1.5)
    val_found, vs = _fp_text_style('>VALUE',  1.5)

    lines = [f'(footprint {_q(fp_id)}',
             f'  (version {_FP_VERSION})',
             f'  (generator babel)',
             f'  (layer "F.Cu")']
    if desc:
        lines.append(f'  (descr {_q(desc)})')

    lines += [
        f'  (fp_text reference "REF**" (at {rs["x"]} {rs["y"]} {rs["rot"]}) (layer "F.SilkS")',
        *([f'    (hide yes)'] if not ref_found else []),
        f'    (effects (font (size {rs["size"]} {rs["size"]}) (thickness {rs["thick"]}) {rs["bold"]}){rs["just"]})',
        f'  )',
        f'  (fp_text value {_q(fp_id)} (at {vs["x"]} {vs["y"]} {vs["rot"]}) (layer "F.Fab")',
        *([f'    (hide yes)'] if not val_found else []),
        f'    (effects (font (size {vs["size"]} {vs["size"]}) (thickness {vs["thick"]}) {vs["bold"]}){vs["just"]})',
        f'  )',
    ]

    for el in fp_el:
        t = el.tag
        if t in ('description', 'model3d', 'pin-mapping', 'attributes'):
            continue

        if t in ('smd', 'pad', 'hole'):
            # no layer attr on pads (mount-side copper by construction);
            # an smd's far-side marker layer="-1" flips its layer set.
            far = el.get('layer') == '-1'
            kl = _q('B.Cu' if far else 'F.Cu')
        else:
            kicad_layer = _fp_kicad_layer(el.get('layer'))
            if kicad_layer is None:
                continue
            kl = _q(kicad_layer)


        if t == 'line':
            x1 = _f(_mm(el.get('x1'))); y1 = _f(_ky(el.get('y1')))
            x2 = _f(_mm(el.get('x2'))); y2 = _f(_ky(el.get('y2')))
            w  = _f(_mm(el.get('width', '120')))
            lines.append(f'  (fp_line (start {x1} {y1}) (end {x2} {y2}) (layer {kl}) (width {w}))')

        elif t == 'arc':
            # endpoint canon: endpoints verbatim (Y-flipped into KiCad
            # space), only the mid point is derived
            x1, y1 = _mm(el.get('x1')), _mm(el.get('y1'))
            x2, y2 = _mm(el.get('x2')), _mm(el.get('y2'))
            mx, my = arc_mid(x1, y1, x2, y2, float(el.get('curve')))
            w      = _f(_mm(el.get('width', '120')))
            lines.append(f'  (fp_arc (start {_f(x1)} {_f(-y1)}) (mid {_f(mx)} {_f(-my)}) '
                         f'(end {_f(x2)} {_f(-y2)}) (layer {kl}) (width {w}))')

        elif t == 'shape':
            x, y = _mm(el.get('x')), _mm(el.get('y'))
            w, h = _mm(el.get('w', '0')), _mm(el.get('h', '0'))
            rnd  = int(el.get('roundness', 0))
            outline = _mm(el.get('outline', '0'))
            lw   = _f(outline)
            fill = 'none' if outline else 'solid'
            if rnd == 100:
                r = w / 2
                lines.append(f'  (fp_circle (center {_f(x)} {_f(-y)}) (end {_f(x+r)} {_f(-y)}) (layer {kl}) (width {lw}) (fill {fill}))')
            else:
                x1k = _f(x - w/2); y1k = _f(-(y - h/2))
                x2k = _f(x + w/2); y2k = _f(-(y + h/2))
                lines.append(f'  (fp_rect (start {x1k} {y1k}) (end {x2k} {y2k}) (layer {kl}) (width {lw}) (fill {fill}))')

        elif t == 'polygon':
            lw = _f(_mm(el.get('width', '0')))
            pts = ' '.join(f'(xy {_f(_mm(v.get("x","0")))} {_f(-_mm(v.get("y","0")))})'
                           for v in el.findall('vertex'))
            lines.append(f'  (fp_poly (pts {pts}) (layer {kl}) (width {lw}) (fill solid))')

        elif t == 'text':
            text = el.text or ''
            if text.startswith('>'):
                continue
            kx  = _f(_mm(el.get('x')));  ky_ = _f(_ky(el.get('y')))
            sz_f   = _mm(el.get('size', '1000'))
            sz     = _f(sz_f)
            rot = float(el.get('rot', 0))
            kr  = _f(rot % 360)   # no negation — see _fp_text_style
            ratio_ = int(el.get('ratio', '8'))
            t      = _f(sz_f * ratio_ / 100)
            b      = ' (bold yes)' if ratio_ >= 15 else ' (bold no)'
            j      = _justify(el.get('align', 'bottom-left'), flip_v=True)
            lines += [f'  (fp_text user {_q(text)} (at {kx} {ky_} {kr}) (layer {kl})',
                      f'    (effects (font (size {sz} {sz}) (thickness {t}){b}){j})',
                      f'  )']

        elif t == 'smd':
            x, y = _mm(el.get('x')), _mm(el.get('y'))
            w, h = _mm(el.get('width')), _mm(el.get('height'))
            name = el.get('name', '')
            # IR roundness 0-100 means radius = (roundness/100) * min_dim/2
            # (see svg_renderer._smd); KiCad roundrect_rratio = radius / min_dim.
            rnd  = float(el.get('roundness', 0)) / 200
            rot  = float(el.get('rot', 0))
            kx = _f(x); ky_ = _f(-y); kw = _f(w); kh = _f(h)
            at = f'{kx} {ky_} {_f(rot % 360)}' if rot else f'{kx} {ky_}'   # no negation — see _fp_text_style
            smd_layers = '"B.Cu" "B.Paste" "B.Mask"' if far else '"F.Cu" "F.Paste" "F.Mask"'
            if rnd > 0:
                lines += [f'  (pad {_q(name)} smd roundrect (at {at}) (size {kw} {kh})',
                          f'    (layers {smd_layers})',
                          f'    (roundrect_rratio {_f(rnd)})',
                          f'  )']
            else:
                lines += [f'  (pad {_q(name)} smd rect (at {at}) (size {kw} {kh})',
                          f'    (layers {smd_layers})',
                          f'  )']

        elif t == 'pad':
            x, y   = _mm(el.get('x')), _mm(el.get('y'))
            drill  = _mm(el.get('drill', '1000'))
            shape  = el.get('shape', 'round')
            name   = el.get('name', '')
            od     = _mm(el.get('diameter')) if el.get('diameter') else drill * 1.8
            kshape = 'rect' if shape == 'square' else 'circle'
            lines += [f'  (pad {_q(name)} thru_hole {kshape} (at {_f(x)} {_f(-y)}) (size {_f(od)} {_f(od)})',
                      f'    (drill {_f(drill)})',
                      f'    (layers "*.Cu" "*.Mask")',
                      f'  )']

        elif t == 'hole':
            x, y = _mm(el.get('x')), _mm(el.get('y'))
            d    = _mm(el.get('drill'))
            lines += [f'  (pad "" np_thru_hole circle (at {_f(x)} {_f(-y)}) (size {_f(d)} {_f(d)})',
                      f'    (drill {_f(d)})',
                      f'    (layers "*.Cu" "*.Mask")',
                      f'  )']

    m3 = fp_el.find('model3d')
    if m3 is not None and model_path:
        tx, ty, tz, rx, ry, rz = model3d_kicad_xyz(m3)
        lines += [
            f'  (model {_q(model_path)}',
            f'    (offset (xyz {_f(tx)} {_f(ty)} {_f(tz)}))',
            f'    (scale (xyz 1 1 1))',
            f'    (rotate (xyz {_f(rx)} {_f(ry)} {_f(rz)}))',
            f'  )',
        ]

    lines.append(')')
    return '\n'.join(lines)


def _resolve_model3d(fp_el, sidecar_src, shapes_dir, lib_name):
    """Copy a footprint's sidecar STEP file into <lib_name>.3dshapes/, sibling
    to .pretty/. Returns the model path to put in the .kicad_mod `(model ...)`
    block (relative to that file), or None if there's no model3d or no
    matching sidecar file.
    """
    src = resolve_model3d_file(fp_el, sidecar_src)
    if src is None:
        return None
    shapes_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, shapes_dir / src.name)
    return f'../{lib_name}.3dshapes/{src.name}'


# ---------------------------------------------------------------------------
# Top-level export
# ---------------------------------------------------------------------------

def export(ir_path, output_dir=None, lib_name=None, skip_components=None):
    """
    Export IR library to KiCad format.
    Creates {lib_name}.kicad_sym and {lib_name}.pretty/ in output_dir.
    Returns (sym_path, pretty_dir).

    lib_name overrides the library nickname/filenames (default: the IR
    root's own name); skip_components is a set of component names to leave
    out entirely — used by kicad_project_exporter to exclude synthesized
    Frame components (they become paper/title_block, not library parts).
    """
    root     = ET.parse(ir_path).getroot()
    if lib_name is None:
        lib_name = root.get('name', Path(ir_path).stem)
    skip_components = skip_components or set()

    out_dir = Path(output_dir) if output_dir else Path(ir_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    sym_path   = out_dir / f'{lib_name}.kicad_sym'
    pretty_dir = out_dir / f'{lib_name}.pretty'
    pretty_dir.mkdir(exist_ok=True)

    # ── Symbol library ────────────────────────────────────────────────────────
    sym_parts = [
        f'(kicad_symbol_lib',
        f'  (version {_SYM_VERSION})',
        f'  (generator "babel")',
        f'  (generator_version "1.0")',
    ]
    for comp in root.findall('component'):
        if comp.get('name') in skip_components:
            continue
        if needs_variant_split(comp):
            names = []
            for fp in comp.findall('footprint'):
                nm = variant_symbol_name(comp, fp)
                sexp = export_symbol(comp, root, lib_name,
                                     variant_fp=fp, sym_name=nm)
                if sexp:
                    sym_parts.append(sexp)
                    names.append(nm)
            import_log.log(comp.get('name'), '',
                           f'VARIANT_SPLIT divergent pin-mappings -> '
                           f'{len(names)} atomic symbol(s): '
                           f'{", ".join(names)}')
            continue
        sexp = export_symbol(comp, root, lib_name)
        if sexp:
            sym_parts.append(sexp)
    sym_parts.append(')')
    sym_path.write_text('\n'.join(sym_parts), encoding='utf-8')

    # ── Footprints ────────────────────────────────────────────────────────────
    # Sidecar STEP files live next to the IR file, in a directory named after
    # its stem (same convention eagle_parser._copy_step_files writes into).
    sidecar_src = Path(ir_path).parent / Path(ir_path).stem
    shapes_dir  = out_dir / f'{lib_name}.3dshapes'

    seen = set()
    n_models = 0
    # ONE library for the whole project keys its entries by NAME, while the
    # IR nests a private copy of the footprint under every component. Two
    # source libraries can offer the same name with DIFFERENT land patterns
    # (tolmach's R1206 in Eagle's own `rc` and `rc@1`), so the diverging
    # copies take Eagle's own `@N` suffix instead of silently overwriting
    # each other — same rule the symbol pool follows on import.
    pool = footprint_pool(root)

    def _write_fp(fp_el):
        nonlocal n_models
        fp_id = pool.get(id(fp_el), fp_el.get('name'))
        if fp_id != fp_el.get('name'):
            import_log.log(fp_el.get('name'), '',
                           'FOOTPRINT name collision (diverged land pattern), '
                           'written as', fp_id)
        if fp_id in seen:
            return
        seen.add(fp_id)
        safe = sanitize_filename(fp_id)
        model_path = _resolve_model3d(fp_el, sidecar_src, shapes_dir, lib_name)
        if model_path:
            n_models += 1
        (pretty_dir / f'{safe}.kicad_mod').write_text(
            export_footprint(fp_el, model_path), encoding='utf-8')

    for comp in root.findall('component'):
        if comp.get('name') in skip_components:
            continue
        for fp in comp.findall('footprint'):
            _write_fp(fp)

    for fp in root.findall('footprint'):   # orphan packages
        _write_fp(fp)

    # Layout-embedded footprints (board-only elements: logo/mechanical,
    # `<element footprint=...>` — Komar YOBA_17X17). The board references
    # them by lib_id like any placed part, so the .pretty must carry them
    # or the re-import's fp-lib-table resolution has nothing to find.
    for lay in root.findall('layout'):
        for fp in lay.findall('footprint'):
            _write_fp(fp)

    print(f'Written: {sym_path}')
    print(f'Written: {pretty_dir}/ ({len(seen)} footprints)')
    if n_models:
        print(f'Written: {shapes_dir}/ ({n_models} models)')
    return str(sym_path), str(pretty_dir)


if __name__ == '__main__':
    import sys
    ir   = sys.argv[1] if len(sys.argv) > 1 else 'testData/r.swlib'
    out  = sys.argv[2] if len(sys.argv) > 2 else 'testData/kicad_out'
    export(ir, out)
