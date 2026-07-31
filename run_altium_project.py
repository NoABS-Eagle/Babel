"""Full pipeline smoke run: source project -> IR .swprj -> Altium project.

Source is an Eagle .sch or a KiCad project DIRECTORY — the importer is picked
from that, nothing else changes. Always regenerates the IR from the SOURCE
(never reuses a cached .swprj). Prints a compact summary only — never dumps
file contents.
"""
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from babel.altium_project_exporter import export_project

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else 'testData/new/tolmach.sch')
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else 'outputs/altium_sch_export')

# Which importer: a .PrjPcb (or a directory holding exactly one) is an
# Altium project, any other directory is a KiCad one, anything else an
# Eagle .sch.
_prjpcb = None
if SRC.suffix.lower() == '.prjpcb':
    _prjpcb = SRC
elif SRC.is_dir():
    _found = sorted(SRC.glob('*.PrjPcb'))
    if len(_found) > 1:
        raise SystemExit(f'{SRC}: {len(_found)} .PrjPcb files — name the one '
                         f'to import explicitly')
    _prjpcb = _found[0] if _found else None

if _prjpcb is not None:
    from babel.altium_project_parser import convert_project as _convert
    SRC = _prjpcb

    def convert_project_full(src, dst):
        return _convert(src, dst)
elif SRC.is_dir():
    from babel.kicad_project_parser import convert_project_full
else:
    from babel.eagle_project_parser import convert_project_full

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

# The IR and everything the importer emits beside it (per-library .swlib,
# the 3D sidecar copy, the import log) are a SEPARATE result of the run, not
# part of the exported project: the output directory must be a KiCad/Altium
# project and nothing else. They go to a sibling `ir_<stem>` folder.
IR_DIR = OUT.parent / f'ir_{SRC.stem}'
if IR_DIR.exists():
    shutil.rmtree(IR_DIR)
IR_DIR.mkdir(parents=True)
swprj = IR_DIR / f'{SRC.stem}.swprj'
convert_project_full(SRC, swprj)

root = ET.parse(swprj).getroot()
sch = root.find('schematic')
print(f'IR: {len(root.findall("component"))} components, '
      f'{len(root.findall("symbols/symbol"))} symbols, '
      f'{len(root.findall("module"))} modules, '
      f'{len(sch.findall("instance"))} instances, '
      f'{len(sch.findall("net"))} nets')

export_project(swprj, OUT)

for f in sorted(OUT.iterdir()):
    print(f'  {f.name}  {f.stat().st_size} bytes')
