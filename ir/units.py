"""Scalar domains shared across the whole IR: units.md, layer-model.md.

Every quantity in the format is an integer — no floats anywhere except the
dielectric epsilon/tanD in a board stack formula (units.md #8), which lives
in layout.py, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

MICRON_LIMIT = 2_000_000


def validate_length(value: int, *, name: str = "length") -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int (µm): {value!r}")
    if not (-MICRON_LIMIT <= value <= MICRON_LIMIT):
        raise ValueError(f"{name} out of range (±{MICRON_LIMIT} µm): {value}")


def validate_angle(value: int, *, name: str = "rot") -> None:
    if not (0 <= value <= 359999):
        raise ValueError(f"{name} out of range (0..359999 mdeg): {value}")


def validate_orthogonal_angle(value: int, *, name: str = "rot") -> None:
    """Schematic/symbol space: rotation is a multiple of 90000 mdeg."""
    validate_angle(value, name=name)
    if value % 90000 != 0:
        raise ValueError(f"{name} must be a multiple of 90000 in schematic space: {value}")


def validate_curve(value: int, *, name: str = "curve") -> None:
    if value == 0:
        raise ValueError(f"{name} == 0 is a <line>, not an arc/bulged edge")
    if not (-359999 <= value <= 359999):
        raise ValueError(f"{name} out of range (±359999 mdeg, ±360000 excluded): {value}")


def validate_range(value: int, lo: int, hi: int, *, name: str) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name} out of range ({lo}..{hi}): {value}")


def validate_bool01(value: int, *, name: str) -> None:
    if value not in (0, 1):
        raise ValueError(f"{name} must be 0 or 1, not a C-style truthy int: {value}")


@dataclass(frozen=True)
class Layer:
    """[!][-]N — layer-model.md. `number` is always the canonical positive N."""

    number: int
    side: int = 1
    anti: bool = False

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError(f"layer number must be positive (side is a separate field): {self.number}")
        if self.side not in (1, -1):
            raise ValueError(f"layer side must be 1 or -1: {self.side}")

    @property
    def is_copper(self) -> bool:
        return self.number < 100

    @property
    def signed(self) -> int:
        return self.number * self.side

    def mirrored(self) -> "Layer":
        return Layer(self.number, -self.side, self.anti)

    def __str__(self) -> str:
        return f"{'!' if self.anti else ''}{self.signed}"

    @classmethod
    def parse(cls, text: str) -> "Layer":
        anti = text.startswith("!")
        if anti:
            text = text[1:]
        n = int(text)
        if n == 0:
            raise ValueError("layer number 0 is illegal (0 cannot carry a sign)")
        return cls(abs(n), 1 if n > 0 else -1, anti)
