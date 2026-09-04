"""<modinst> — module-instance.md. An instance of a module: a channel of
the hierarchy, not a kind of <compinst> — different address, different
naming rules, different everything except the tag family.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

from .attr import Attr, validate_unique_attrs
from .graphics import Text
from .units import validate_bool01, validate_length, validate_orthogonal_angle

_TRAILING_DIGITS = re.compile(r"^(.*?)(\d*)$")


def expand_designator(base_name: str, channel_name: str, offset: int | None) -> str:
    """A part named `base_name` inside a channel, as it's addressed
    outside the module — module-instance.md #как-называются-детали-внутри-канала.
    """
    if offset is None:
        return f"{channel_name}:{base_name}"
    head, digits = _TRAILING_DIGITS.match(base_name).groups()
    number = int(digits) if digits else 0
    return f"{head}{number + offset}"


def expand_net_name(net_name: str, channel_name: str) -> str:
    """An internal net's name outside the module — always prefixed, `offset`
    never applies to it (a net's name may carry no digits at all).
    """
    return f"{channel_name}:{net_name}"


@dataclass
class ModuleInstance:
    module: str
    name: str
    x: int
    y: int
    rot: int = 0
    mirror: int = 0
    offset: int | None = None
    variant: str | None = None
    """Which variant of `module` this channel builds — None means the
    module's own base."""
    attrs: list[Attr] = field(default_factory=list)
    """The parametric interface — module-instance.md #атрибуты-канала."""
    texts: list[Text] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.module:
            raise ValueError("modinst.module must not be empty")
        if not self.name or any(ch.isspace() or ch in "@{}" for ch in self.name):
            raise ValueError(f"channel name out of domain: {self.name!r}")
        if self.name != self.name.upper():
            raise ValueError(f"channel name must be upper-case, like a designator: {self.name!r}")
        validate_length(self.x, name="x")
        validate_length(self.y, name="y")
        validate_orthogonal_angle(self.rot)
        validate_bool01(self.mirror, name="mirror")
        if self.offset is not None and self.offset < 0:
            raise ValueError(f"offset must be a non-negative integer: {self.offset}")
        validate_unique_attrs(self.attrs)
