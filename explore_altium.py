import sys, io, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pyaltiumlib
from altium_monkey import AltiumIntLib

INTLIB = r'testData/GessorLib/Project Outputs for gessor_lib/gessor_lib.IntLib'
lib = AltiumIntLib(INTLIB)
tmpdir = tempfile.mkdtemp()
result = lib.extract_sources(tmpdir)
src_map = {s.stream_path: s.output_path for s in result.sources}

pcblib = pyaltiumlib.read(str(src_map['PCBLib/0.pcblib']))
for name in pcblib.list_parts():
    fp = pcblib.get_part(name)
    for r in fp.Records:
        if type(r).__name__ == 'PcbString':
            print('PcbString ALL non-callable attrs:')
            for a in dir(r):
                if a.startswith('_'): continue
                try:
                    v = getattr(r, a)
                    if not callable(v):
                        print(f'  {a} = {v!r}')
                except: pass
            break
    else:
        continue
    break
