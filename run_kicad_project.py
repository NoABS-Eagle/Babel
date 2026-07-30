"""Full pipeline smoke run: source project -> IR .swprj -> KiCad project.

Mirror of run_altium_project.py, same two arguments and the same rule for
picking the importer: a directory is a KiCad project, anything else is an
Eagle .sch. Always regenerates the IR from the SOURCE (never reuses a cached
.swprj). Prints a compact summary only — never dumps file contents.
"""
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from babel.kicad_project_exporter import export_project

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else 'testData/new/tolmach.sch')
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else 'outputs/kicad_sch_export')

if SRC.is_dir():
    from babel.kicad_project_parser import convert_project_full
else:
    from babel.eagle_project_parser import convert_project_full

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

swprj = OUT / f'{SRC.stem}.swprj'
convert_project_full(SRC, swprj)

root = ET.parse(swprj).getroot()
sch = root.find('schematic')
layout = root.find('layout')
print(f'IR: {len(root.findall("component"))} components, '
      f'{len(root.findall("symbols/symbol"))} symbols, '
      f'{len(root.findall("module"))} modules, '
      f'{len(sch.findall("instance"))} instances, '
      f'{len(sch.findall("net"))} nets'
      + (f', layout {len(layout.findall("element"))} elements, '
         f'{len(layout.findall("signal"))} signals' if layout is not None
         else ', no layout'))

export_project(swprj, OUT)

for f in sorted(OUT.iterdir()):
    print(f'  {f.name}  {f.stat().st_size} bytes')
