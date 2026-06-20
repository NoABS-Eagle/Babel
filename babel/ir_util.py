"""Shared helpers for reading the Babel IR symbol pool / gate structure."""


def symbol_pool(root):
    """Return {symbol_name: <symbol> element} from the library-level pool."""
    syms_el = root.find('symbols')
    if syms_el is None:
        return {}
    return {s.get('name'): s for s in syms_el.findall('symbol')}


def component_gates(comp_el):
    """Resolve a component's gates.

    Returns a list of (gate_name, symbol_name) tuples:
      - single-mode component (has `symbol` attr): [(None, symbol_name)]
      - multi-mode component (has <gate> children): [(gate_name, symbol_name), ...]
    """
    sym_attr = comp_el.get('symbol')
    if sym_attr is not None:
        return [(None, sym_attr)]
    return [(g.get('name'), g.get('symbol')) for g in comp_el.findall('gate')]


def is_multi_gate(comp_el):
    """True if the component routes symbols through explicit <gate> elements."""
    return comp_el.get('symbol') is None and comp_el.find('gate') is not None
