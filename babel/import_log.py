"""Shared text import-log accumulator — see ir_schema.md "Лог импорта".

Parsers call log() as they go (same fire-and-forget shape as the existing
`print(f'  ! ...')` warnings — not threaded through every function call as
a parameter); the CLI entrypoint calls write() once the run is done to also
flush the same lines to a sibling `<output>.import.log` file, so a headless/
batch run can be grepped/diffed without re-parsing console output.
"""
import sys
from pathlib import Path

_entries = []


def log(*parts):
    """Record one import-log line (e.g. footprint, pad, reason, before -> after)
    — printed immediately (matches the existing warning convention) and
    buffered for write().
    """
    line = ' '.join(str(p) for p in parts)
    try:
        print(f'  ! {line}')
    except UnicodeEncodeError:
        # A legacy Windows console codepage (cp1251 etc.) can't encode
        # every Unicode character a source label may legally contain (real
        # case: a proper U+2212 MINUS SIGN in board silkscreen text) — the
        # LOG must never be what crashes an otherwise-successful
        # conversion, so degrade this one line instead of losing the run.
        enc = sys.stdout.encoding or 'ascii'
        print(f'  ! {line}'.encode(enc, errors='replace').decode(enc))
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
