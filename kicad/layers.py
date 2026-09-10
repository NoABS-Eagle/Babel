"""The non-copper layer table — conversion-kicad.md #слои.

Kept as a data file (`kicad_layers.tsv`) rather than a dict in code because
the spec says so and means it: a user who needs one more layer carried over
adds a row, without touching the converter.
"""

from __future__ import annotations

from pathlib import Path

_TABLE = Path(__file__).with_name("kicad_layers.tsv")

_FRONT_PREFIX = "F."
_BACK_PREFIX = "B."


def _read() -> list[tuple[str, int]]:
    rows = []
    for line in _TABLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, number = line.split("\t")
        rows.append((name.strip(), int(number)))
    return rows


_ROWS = _read()

_FORWARD: dict[int, str] = {}
_REVERSE: dict[str, int] = {}
for _name, _number in _ROWS:
    _FORWARD.setdefault(_number, _name)
    _REVERSE.setdefault(_name, _number)
    if _name.startswith(_FRONT_PREFIX):
        # One row, both sides: the sign of the IR number picks which.
        _back = _BACK_PREFIX + _name[len(_FRONT_PREFIX):]
        _FORWARD.setdefault(-_number, _back)
        _REVERSE.setdefault(_back, -_number)


def kicad_layer(ir_layer: int, log, what: str) -> str | None:
    """IR layer number -> KiCad layer name, or None (logged) when the table
    does not carry it. Copper is the caller's business: it depends on the
    board's own stack, which a table cannot know."""
    name = _FORWARD.get(ir_layer)
    if name is None:
        log(f"{what}: layer {ir_layer} has no row in kicad_layers.tsv — dropped "
            f"(add a row there to carry it)")
    return name


def ir_layer(kicad_layer_name: str) -> int | None:
    """The reverse direction, for the KiCad importer to come."""
    return _REVERSE.get(kicad_layer_name)
