"""<model3d> — model3d.md. Lives on the footprint, not the device: devices
sharing one footprint share its model too."""

from __future__ import annotations

from dataclasses import dataclass

from .units import validate_length


@dataclass
class Model3D:
    key: str | None = None
    """None = unconditional fallback, at most one per footprint. Otherwise
    the bare value of an attribute on the placed part (e.g. "RED"),
    compared case-insensitively — model3d.md #ключ-сравнивается-без-учёта-регистра."""
    tx: int = 0
    ty: int = 0
    tz: int = 0
    rx: int = 0
    ry: int = 0
    rz: int = 0
    """MCAD intrinsic XYZ rotation, mdeg."""

    def __post_init__(self) -> None:
        for n, v in (("tx", self.tx), ("ty", self.ty), ("tz", self.tz)):
            validate_length(v, name=n)
        for n, v in (("rx", self.rx), ("ry", self.ry), ("rz", self.rz)):
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"{n} must be an int (mdeg): {v!r}")
