"""<module> — module.md. A reusable schematic block: exactly two named
halves, an outward symbol and inner content, and nothing else lies
directly under it — a stray child would silently join neither half.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schematic import Schematic
from .symbol import Symbol


@dataclass
class Module:
    name: str
    prefix: str
    """Required, no default — unlike component.md's prefix. A module only
    exists for multichannel use, so a channel is always coming."""
    symbol: Symbol
    schematic: Schematic

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("module name must not be empty")
        if any(ch.isspace() or ch in "@{}:" for ch in self.prefix):
            raise ValueError(f"prefix out of the designator domain: {self.prefix!r}")
        if self.prefix != self.prefix.upper():
            raise ValueError(f"prefix must be upper-case, like a designator: {self.prefix!r}")

        if self.symbol.is_pooled:
            raise ValueError(
                f"module {self.name!r}: its own symbol carries no name/library — "
                "it lives inside the module, not a library pool"
            )
        if self.symbol.is_frame:
            raise ValueError(
                f"module {self.name!r}: its outward symbol must not be a sheet frame "
                "(layer 98) — a frame carries no pins, and this channel would connect to nothing"
            )
        if self.schematic.modinsts:
            raise ValueError(
                f"module {self.name!r}: hierarchy is exactly one level deep — "
                "its own schematic must contain no channels"
            )
