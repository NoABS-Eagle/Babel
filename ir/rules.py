"""<rules> — rules.md. Six numbers from the fab: a floor, not a rule
engine. Every number optional; absence means "not known", never zero."""

from __future__ import annotations

from dataclasses import dataclass

from .units import validate_length


@dataclass
class Rules:
    clearance: int | None = None
    edge_clearance: int | None = None
    min_width: int | None = None
    min_drill: int | None = None
    min_annular: int | None = None
    min_drill_web: int | None = None

    def __post_init__(self) -> None:
        for n, v in (
            ("clearance", self.clearance),
            ("edge_clearance", self.edge_clearance),
            ("min_width", self.min_width),
            ("min_drill", self.min_drill),
            ("min_annular", self.min_annular),
            ("min_drill_web", self.min_drill_web),
        ):
            if v is not None:
                validate_length(v, name=n)
                if v < 0:
                    raise ValueError(f"{n} must not be negative: {v}")
