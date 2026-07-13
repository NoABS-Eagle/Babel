"""Eagle board round-trip check: .sch+.brd -> IR -> .brd, structural compare.

Byte identity is not the bar (attribute order, pretty-printing, library
regrouping differ legitimately); what must survive is the STRUCTURE: every
element at the same place/rot/side with the same package, every signal with
the same contactrefs and the same copper geometry, plain geometry per layer.
Usage: python roundtrip_eagle_board.py testData/luminoso testData/modtest
"""
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from babel.eagle_project_parser import convert_project_full
from babel.eagle_board_exporter import export_board


def _f(v, nd=3):
    return round(float(v or 0), nd)


# 2 µm tolerance: Eagle stores sub-µm coordinates (arbitrary-angle routing
# leaves values like 10.94495), the IR canon is integer µm, and the arc
# representation (center/radius/angles) reconstructs endpoints with ~1 µm
# wobble. Quantized rounding flaps at bucket boundaries, so geometry is
# paired with a tolerance instead.
_TOL = 0.002


def _match(ca, cb):
    """Approximate multiset equality: every item of ca pairs with one item
    of cb whose numeric fields all agree within _TOL (non-numeric: exact)."""
    rest = list(cb.elements())
    for item in ca.elements():
        for j, cand in enumerate(rest):
            if len(cand) == len(item) and all(
                    (abs(x - y) <= _TOL if isinstance(x, float) else x == y)
                    for x, y in zip(item, cand)):
                rest.pop(j)
                break
        else:
            return False
    return not rest


def _board(path):
    return ET.parse(path).getroot().find('.//board')


def _elements(board):
    out = {}
    for e in board.find('elements'):
        out[e.get('name')] = (e.get('package'), _f(e.get('x')), _f(e.get('y')),
                              e.get('rot') or 'R0', e.get('value') or '')
    return out


def _element_attrs(board):
    """element -> {(attr name, value)}, NAME/VALUE excluded (placeholders)."""
    return {e.get('name'): {(a.get('name'), a.get('value') or '')
                            for a in e.findall('attribute')
                            if a.get('name') not in ('NAME', 'VALUE')}
            for e in board.find('elements')}


def _signal_geom(board):
    """signal name -> (contactref set, wire multiset, via multiset, polygon count)."""
    out = {}
    for s in board.find('signals'):
        crefs, wires, vias, polys = set(), Counter(), Counter(), 0
        for c in s:
            if c.tag == 'contactref':
                crefs.add((c.get('element'), c.get('pad')))
            elif c.tag == 'wire':
                if c.get('layer') == '19':
                    continue                      # airwires: dropped by design
                wires[(_f(c.get('x1')), _f(c.get('y1')), _f(c.get('x2')),
                       _f(c.get('y2')), c.get('layer'), _f(c.get('width')))] += 1
            elif c.tag == 'via':
                vias[(_f(c.get('x')), _f(c.get('y')), _f(c.get('drill')))] += 1
            elif c.tag == 'polygon':
                polys += 1
        out[s.get('name')] = (crefs, wires, vias, polys)
    return out


def _stack_params(board):
    """layerSetup/mtCopper/mtIsolate from designrules — the stack facts the
    IR formula carries; must come back cell-for-cell (unused cells ride the
    passthrough verbatim, used cells are rewritten from the formula and
    must land on the same values)."""
    out = {}
    dr = board.find('designrules')
    for p in (dr.findall('param') if dr is not None else ()):
        if p.get('name') in ('layerSetup', 'mtCopper', 'mtIsolate'):
            out[p.get('name')] = p.get('value')
    return out


def _globals(board):
    """Board-level global attributes (user tool bags like NOABS_*) — must
    survive verbatim, uninterpreted."""
    attrs = board.find('attributes')
    return {(a.get('name'), a.get('value') or '')
            for a in (attrs if attrs is not None else ())}


def _plain_layers(board):
    plain = board.find('plain')
    items = []
    for c in (plain if plain is not None else ()):
        if c.tag == 'dimension':
            continue
        if c.tag == 'text':
            # rot string carries the M(irror) flag — its loss must FAIL
            items.append(('text', c.get('layer'), c.get('rot') or 'R0'))
        else:
            items.append(c.get('layer'))
    return Counter(items)


def check(stem):
    stem = Path(stem)
    sch, brd = stem.with_suffix('.sch'), stem.with_suffix('.brd')
    ir = Path('outputs') / f'{stem.name}.swprj'
    rt = Path('outputs') / f'{stem.name}_rt.brd'
    convert_project_full(sch, ir)
    export_board(ir, rt)

    a, b = _board(brd), _board(rt)
    ok = True

    ea, eb = _elements(a), _elements(b)
    if set(ea) != set(eb):
        ok = False
        print(f'  FAIL element sets differ: only-src={sorted(set(ea)-set(eb))[:5]} '
              f'only-rt={sorted(set(eb)-set(ea))[:5]}')
    else:
        diff = [n for n in ea if ea[n] != eb[n]]
        if diff:
            ok = False
            for n in diff[:5]:
                print(f'  FAIL element {n}: {ea[n]} != {eb[n]}')
        else:
            print(f'  ok  {len(ea)} elements (package/x/y/rot)')

    # attribute VALUES must survive: src set ⊆ rt set (the import's union
    # merge may legitimately ADD schematic-side attrs to the board copies)
    aa, ab = _element_attrs(a), _element_attrs(b)
    lost = {n: aa[n] - ab.get(n, set()) for n in aa if aa[n] - ab.get(n, set())}
    if lost:
        ok = False
        for n, d in list(lost.items())[:5]:
            print(f'  FAIL element {n}: attrs lost: {sorted(d)[:4]}')
    else:
        n_at = sum(len(v) for v in aa.values())
        extra_names = Counter(k for n in aa for k, _ in ab.get(n, set()) - aa[n])
        extra = sum(extra_names.values())
        # extras are legitimate ONLY as sch->board merge products; name the
        # names so injected garbage is visible to the eye (a synthesized
        # 'DESCRIPTION' slipped through a bare count once)
        names = (' [' + ', '.join(f'{k}x{v}' for k, v in extra_names.most_common(6)) + ']'
                 if extra_names else '')
        print(f'  ok  element attributes ({n_at} records survive'
              + (f', +{extra} merged from sch{names})' if extra else ')'))

    sa, sb = _signal_geom(a), _signal_geom(b)
    if set(sa) != set(sb):
        ok = False
        print(f'  FAIL signal sets differ: {sorted(set(sa) ^ set(sb))[:6]}')
    else:
        bad = 0
        for n in sa:
            for i, what in ((0, 'contactrefs'), (1, 'wires'), (2, 'vias'),
                            (3, 'polygons')):
                same = (_match(sa[n][i], sb[n][i]) if i in (1, 2)
                        else sa[n][i] == sb[n][i])
                if not same:
                    bad += 1
                    if bad <= 5:
                        print(f'  FAIL signal {n}: {what} differ')
        if not bad:
            nw = sum(sum(w.values()) for _, w, _, _ in sa.values())
            nv = sum(sum(v.values()) for _, _, v, _ in sa.values())
            print(f'  ok  {len(sa)} signals ({nw} wires, {nv} vias)')
        ok = ok and not bad

    ga, gb = _globals(a), _globals(b)
    if ga != gb:
        ok = False
        print(f'  FAIL board globals: lost={sorted(ga - gb)[:5]} '
              f'extra={sorted(gb - ga)[:5]}')
    else:
        print(f'  ok  board globals ({len(ga)} attrs)')

    ka, kb = _stack_params(a), _stack_params(b)
    if ka != kb:
        ok = False
        for k in sorted(set(ka) | set(kb)):
            if ka.get(k) != kb.get(k):
                print(f'  FAIL stack param {k}: src={ka.get(k)!r} rt={kb.get(k)!r}')
    else:
        print(f'  ok  stack params ({", ".join(sorted(ka)) or "none"})')

    pa, pb = _plain_layers(a), _plain_layers(b)
    if pa != pb:
        ok = False
        print(f'  FAIL plain per-layer counts: src={dict(pa)} rt={dict(pb)}')
    else:
        print(f'  ok  plain geometry ({sum(pa.values())} objects)')
    return ok


if __name__ == '__main__':
    stems = sys.argv[1:] or ['testData/luminoso', 'testData/modtest']
    good = True
    for stem in stems:
        print(f'=== {stem}')
        good &= check(stem)
    print('\nBOARD ROUND-TRIP OK' if good else '\nBOARD ROUND-TRIP FAILED')
    sys.exit(0 if good else 1)
