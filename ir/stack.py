"""The stack formula — layout.md #формула-стека. `copper[dielectric]copper…`,
strictly alternating, starting and ending with copper. Not a spec page of
its own, but load-bearing enough (copper layer numbering derives from it)
to deserve its own parser rather than living inline in layout.py.
"""

from __future__ import annotations

import re

DEFAULT_STACK = "35[1530]35"

_TOKEN_RE = re.compile(r"\d+|\[[^\[\]]*\]")
_DIELECTRIC_RE = re.compile(r"^(\d+)(?::(\d+(?:\.\d+)?))?(?::(\d+(?:\.\d+)?))?$")


class Dielectric:
    __slots__ = ("thickness", "epsilon", "loss_tangent")

    def __init__(self, thickness: int, epsilon: float | None = None, loss_tangent: float | None = None) -> None:
        self.thickness = thickness
        self.epsilon = epsilon
        self.loss_tangent = loss_tangent


def parse_stack(formula: str) -> tuple[list[int], list[Dielectric]]:
    """Returns (copper thicknesses top-to-bottom, dielectrics between them)
    — len(copper) == len(dielectrics) + 1, always."""
    pos = 0
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(formula):
        if m.start() != pos:
            raise ValueError(f"malformed stack formula: {formula!r}")
        tokens.append(m.group(0))
        pos = m.end()
    if pos != len(formula) or not tokens:
        raise ValueError(f"malformed stack formula: {formula!r}")
    if tokens[0].startswith("[") or tokens[-1].startswith("["):
        raise ValueError(f"stack formula must start and end with copper: {formula!r}")

    copper: list[int] = []
    dielectrics: list[Dielectric] = []
    for i, tok in enumerate(tokens):
        is_dielectric = tok.startswith("[")
        if is_dielectric != bool(i % 2):
            raise ValueError(f"stack formula must strictly alternate copper/dielectric: {formula!r}")
        if is_dielectric:
            m = _DIELECTRIC_RE.match(tok[1:-1])
            if not m:
                raise ValueError(f"malformed dielectric spec {tok!r} in {formula!r}")
            thickness_s, eps_s, tand_s = m.groups()
            dielectrics.append(
                Dielectric(
                    int(thickness_s),
                    float(eps_s) if eps_s is not None else None,
                    float(tand_s) if tand_s is not None else None,
                )
            )
        else:
            copper.append(int(tok))

    if len(copper) < 2:
        raise ValueError(
            f"a single-sided board is rejected — the stack formula needs at least two copper layers: {formula!r}"
        )
    if any(c <= 0 for c in copper):
        raise ValueError(f"copper thickness must be positive: {formula!r}")
    if any(d.thickness <= 0 for d in dielectrics):
        raise ValueError(f"dielectric thickness must be positive: {formula!r}")
    return copper, dielectrics


def copper_layer_numbers(copper_count: int) -> list[int]:
    """Top is always 1, bottom always -1, internals 2..N-1 top-to-bottom —
    never renumbered when the stack shrinks (layout.md
    #номера-не-переставляются-никогда)."""
    if copper_count < 2:
        raise ValueError("at least two copper layers required")
    if copper_count == 2:
        return [1, -1]
    return [1, *range(2, copper_count), -1]
