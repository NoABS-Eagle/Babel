"""CLI: Eagle -> IR -> Eagle, for checking the exporter against the
importer's own output. <path.sch> [output.sch] (default: output/<name>.sch)
Writes a project FOLDER (named after the output stem) holding the .sch,
its .brd if any, and one .lbr per library used — see eagle/export.py.
"""

import sys
from pathlib import Path

from eagle.export import write_project
from eagle.project import import_project


def main(argv: list[str]) -> None:
    if len(argv) not in (2, 3):
        raise SystemExit(f"usage: {argv[0]} <path-to-.sch> [output-.sch]")
    path = Path(argv[1])
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")

    out_path = Path(argv[2]) if len(argv) == 3 else Path("output") / f"{path.stem}.sch"

    project = import_project(path)
    out_dir = write_project(project, out_path, source_models_dir=path.parent / path.stem)
    print(f"-> {out_dir}")

    from import_log import write as write_log
    write_log(out_dir / f"{out_path.stem}.sch")


if __name__ == "__main__":
    main(sys.argv)
