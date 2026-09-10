"""CLI entry point: <path-to-project-file> -> output/<name>.siprj"""

import sys
from pathlib import Path

from ir.xml import write_project

EXTENSION_TO_TOOL = {
    ".PrjPcb": "altium",
    ".kicad_pro": "kicad",
    ".sch": "eagle",
}


def import_project(path: Path):
    tool = EXTENSION_TO_TOOL.get(path.suffix)
    if tool is None:
        raise SystemExit(f"unrecognized project file: {path}")
    if tool == "altium":
        from altium.project import import_project as impl
    elif tool == "kicad":
        from kicad.project import import_project as impl
    elif tool == "eagle":
        from eagle.project import import_project as impl
    return tool, impl(path)


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(f"usage: {argv[0]} <path-to-project-file>")
    path = Path(argv[1])
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")

    _tool, project = import_project(path)

    out_path = Path("output") / f"{path.stem}.siprj"
    out_path.parent.mkdir(exist_ok=True)
    write_project(project, out_path)
    print(f"-> {out_path}")

    # conversion.md "не теряет молча": every simplification and loss on any
    # path goes here.
    from import_log import write as write_log
    write_log(out_path)


if __name__ == "__main__":
    main(sys.argv)
