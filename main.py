import sys
from pathlib import Path

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
    return impl(path)


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(f"usage: {argv[0]} <path-to-project-file>")
    path = Path(argv[1])
    project = import_project(path)
    out_path = Path("output") / f"{path.stem}.siprj"
    out_path.parent.mkdir(exist_ok=True)
    project.write(out_path)


if __name__ == "__main__":
    main(sys.argv)
