"""Full pipeline smoke run: Eagle source -> IR .swprj -> Altium project.

Always regenerates the IR from the SOURCE (never reuses a cached .swprj).
Prints a compact summary only — never dumps file contents.
"""
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from babel.eagle_project_parser import convert_project_full
from babel.altium_project_exporter import export_project

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else 'testData/new/tolmach.sch')
OUT = Path('outputs/altium_sch_export')

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

swprj = OUT / f'{SRC.stem}.swprj'
convert_project_full(SRC, swprj)

root = ET.parse(swprj).getroot()
sch = root.find('schematic')
print(f'IR: {len(root.findall("component"))} components, '
      f'{len(root.findall("symbols/symbol"))} symbols, '
      f'{len(sch.findall("instance"))} instances, '
      f'{len(sch.findall("net"))} nets')

export_project(swprj, OUT)

for f in sorted(OUT.iterdir()):
    print(f'  {f.name}  {f.stat().st_size} bytes')
