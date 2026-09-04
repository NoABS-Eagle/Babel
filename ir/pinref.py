"""<pinref> — pinref.md. Connects one net segment to a pin, on a part's
section or on a module channel — same tag, either target."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PinRef:
    inst: str
    """Name of a <part> or a <modinst> — shared namespace, see pinref.md."""
    pin: str
    gate: str | None = None
    """Written iff `inst` is a part: a channel has no sections at all."""

    def __post_init__(self) -> None:
        if not self.inst:
            raise ValueError("pinref.inst must not be empty")
        if any(ch.isspace() or ch in "{}" for ch in self.pin):
            raise ValueError(f"pinref pin name out of domain: {self.pin!r}")
