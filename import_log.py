"""Shared import-log accumulator — conversion.md "Каждое упрощение попадает
в лог" / "Не теряет молча". Converters call `log()` as they go; the CLI
entrypoint calls `write()` once the run is done to flush the same lines to
a sibling `<output>.import.log` file.

Lives at the top level, not under any one tool's package: the obligation
is the contract's, and every path — Eagle, KiCad, Altium — owes it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_entries: list[str] = []


def log(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    try:
        print(f"  ! {line}")
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(f"  ! {line}".encode(enc, errors="replace").decode(enc))
    _entries.append(line)


def write(output_path) -> None:
    if not _entries:
        return
    log_path = Path(str(output_path) + ".import.log")
    log_path.write_text("\n".join(_entries) + "\n", encoding="utf-8")
    print(f"Import log: {log_path}  ({len(_entries)} entries)")
    _entries.clear()
