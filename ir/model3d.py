"""<model3d> — model3d.md. Lives on the footprint, not the device: devices
sharing one footprint share its model too."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .units import validate_length

EXTENSIONS = (".step", ".stp")
"""model3d.md #файлов-два: STEP is the only input format."""


def base_name(footprint_name: str, key: str | None) -> str:
    """model3d.md #имя-файла-модели-в-ir-не-хранится: the file is named
    after the FOOTPRINT, plus the dispatch key when there is one."""
    return f"{footprint_name}_{key}" if key else footprint_name


def find_file(models_dir: Path | None, footprint_name: str, key: str | None) -> Path | None:
    if models_dir is None:
        return None
    base = base_name(footprint_name, key)
    for ext in EXTENSIONS:
        candidate = Path(models_dir) / f"{base}{ext}"
        if candidate.is_file():
            return candidate
    return None


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

    def dialog_rotation(self) -> tuple[float, float, float]:
        """The same orientation, spelled the way a 3D-model DIALOG asks for
        it — degrees, `Rz·Ry·Rx`.

        model3d.md #размещение-полная-матрица fixes the IR's own
        composition as **intrinsic `Rx·Ry·Rz`**. KiCad and Altium both
        describe the identical orientation as `Rz·Ry·Rx`, so the three
        angles CANNOT be handed over axis by axis: the two agree only for
        a pure-Z rotation, which is exactly why flat parts looked right
        for months while a genuinely two-axis model did not. Ground truth
        twice over, from the previous Babel: `DD1` (LQFP100) is IR
        `(0, -90, -90)` and has to be entered as `(90, 0, -90)` — verified
        visually in KiCad and, separately, by the user typing those three
        numbers into Altium by hand.

        This lives on the model, not in a target's exporter, because every
        target asks the same question ("один факт — одно место").
        Whether the FILE then stores these angles or their negation is the
        target's own business — KiCad negates."""
        ex, ey, ez = (math.radians(v / 1000) for v in (self.rx, self.ry, self.rz))
        cx, sx = math.cos(ex), math.sin(ex)
        cy, sy = math.cos(ey), math.sin(ey)
        cz, sz = math.cos(ez), math.sin(ez)
        # R = Rx(ex) @ Ry(ey) @ Rz(ez), row-major — only the cells the
        # decomposition below reads.
        r00 = cy * cz
        r01 = -cy * sz
        r10 = cx * sz + sx * sy * cz
        r11 = cx * cz - sx * sy * sz
        r20 = sx * sz - cx * sy * cz
        r21 = sx * cz + cx * sy * sz
        r22 = cx * cy
        # ...decomposed as R = Rz(g) @ Ry(b) @ Rx(a), where R[2][0] = -sin b.
        if abs(r20) < 1 - 1e-9:
            a = math.degrees(math.atan2(r21, r22))
            b = math.degrees(math.asin(-r20))
            g = math.degrees(math.atan2(r10, r00))
        else:                        # gimbal lock: b = ±90, a taken as 0
            a = 0.0
            b = 90.0 if r20 < 0 else -90.0
            g = math.degrees(math.atan2(-r01, r11))
        return a, b, g


def from_dialog_rotation(a: float, b: float, g: float) -> tuple[int, int, int]:
    """Dialog angles (`Rz·Ry·Rx`, degrees) -> the IR's own `rx`/`ry`/`rz`
    in mdeg — the exact inverse of `Model3D.dialog_rotation`.

    Every importer needs it for the same reason the exporter needs the
    forward one: KiCad and Altium describe an orientation as `Rz·Ry·Rx`
    while model3d.md composes it as intrinsic `Rx·Ry·Rz`, and the three
    numbers cannot be handed over axis by axis."""
    ra, rb, rg = (math.radians(v) for v in (a, b, g))
    ca, sa = math.cos(ra), math.sin(ra)
    cb, sb = math.cos(rb), math.sin(rb)
    cg, sg = math.cos(rg), math.sin(rg)
    # R = Rz(g) @ Ry(b) @ Rx(a), row-major — the cells the decomposition
    # below reads.
    r00 = cg * cb
    r01 = -sg * ca + cg * sb * sa
    r02 = sg * sa + cg * sb * ca
    r12 = -cg * sa + sg * sb * ca
    r22 = cb * ca
    # ...decomposed as R = Rx(ex) @ Ry(ey) @ Rz(ez), where R[0][2] = sin ey.
    ey = math.degrees(math.asin(max(-1.0, min(1.0, r02))))
    ex = math.degrees(math.atan2(-r12, r22))
    ez = math.degrees(math.atan2(-r01, r00))
    return (round(ex * 1000) % 360000, round(ey * 1000) % 360000,
            round(ez * 1000) % 360000)
