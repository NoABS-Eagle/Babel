"""<contactref> — contactref.md. The board's twin of <pinref>: an
explicit, recorded end of a net at a placed element's pad. Never inferred
from copper touching a pad — connectivity is declared, not measured."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ContactRef:
    element: str
    pad: str

    def __post_init__(self) -> None:
        if not self.element:
            raise ValueError("contactref.element must not be empty")
        if any(ch.isspace() or ch in "{}" for ch in self.pad):
            raise ValueError(f"contactref pad name out of domain: {self.pad!r}")
