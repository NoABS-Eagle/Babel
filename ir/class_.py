"""<class> — class.md. A pool of named rule sets; membership lives on the
net as an attr (net.py), not here — this is only the rule-set half.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .units import validate_length


@dataclass
class Class:
    name: str
    width: int | None = None
    clearance: int | None = None
    drill: int | None = None
    attrs: list[Attr] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("class name must not be empty")
        for n, v in (("width", self.width), ("clearance", self.clearance), ("drill", self.drill)):
            if v is not None:
                validate_length(v, name=n)
        validate_unique_attrs(self.attrs)
