"""Shared text import-log accumulator — see ir_schema.md "Лог импорта".

Parsers call log() as they go (same fire-and-forget shape as the existing
`print(f'  ! ...')` warnings — not threaded through every function call as
a parameter); the CLI entrypoint calls write() once the run is done to also
flush the same lines to a sibling `<output>.import.log` file, so a headless/
batch run can be grepped/diffed without re-parsing console output.
"""
from pathlib import Path

_entries = []


def log(*parts):
    """Record one import-log line (e.g. footprint, pad, reason, before -> after)
    — printed immediately (matches the existing warning convention) and
    buffered for write().
    """
    line = ' '.join(str(p) for p in parts)
    print(f'  ! {line}')
    _entries.append(line)


def write(output_path):
    """Flush the buffered lines to '<output>.import.log' and clear the
    buffer for the next run. No-op (no file written) if nothing was logged.
    """
    if not _entries:
        return
    log_path = Path(str(output_path) + '.import.log')
    log_path.write_text('\n'.join(_entries) + '\n', encoding='utf-8')
    print(f'Import log: {log_path}  ({len(_entries)} entries)')
    _entries.clear()
