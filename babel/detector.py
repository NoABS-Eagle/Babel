"""EDA format detector and workspace scanner."""
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Artifact:
    kind: str
    label: str
    paths: list  # list[str]


def _head(path: Path, n: int = 256) -> str:
    try:
        return path.read_bytes()[:n].decode('utf-8', errors='ignore')
    except OSError:
        return ''


def detect_file(path: Path):
    """Return kind string for a single file, or None if unrecognized."""
    ext = path.suffix.lower()
    h = _head(path)

    if ext in ('.lbr', '.sch', '.brd'):
        if '<eagle' in h or 'eagle.dtd' in h:
            return 'eagle_' + ext[1:]       # eagle_lbr / eagle_sch / eagle_brd

    if ext == '.kicad_sym' and '(kicad_symbol_lib' in h:
        return 'kicad_sym'
    if ext == '.kicad_sch' and '(kicad_sch' in h:
        return 'kicad_sch'
    if ext == '.kicad_pcb' and '(kicad_pcb' in h:
        return 'kicad_pcb'
    if ext == '.kicad_pro':
        return 'kicad_pro'

    if ext in ('.schlib', '.pcblib', '.intlib'):
        raw = path.read_bytes()[:8]
        if raw == b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1':
            return 'altium_intlib' if ext == '.intlib' else 'altium_bin'

    return None


def _is_fp_folder(d: Path) -> bool:
    if d.suffix.lower() == '.pretty':
        return True
    return any(d.glob('*.kicad_mod'))


def scan(root: str) -> dict:
    """
    Scan root (file or folder) and return grouped artifacts dict:
      root, eagle_lbr, eagle_project, kicad_sym, kicad_fp_lib,
      kicad_project, altium_lib
    """
    p = Path(root)
    result = dict(root=str(p), eagle_lbr=[], eagle_project=[],
                  kicad_sym=[], kicad_fp_lib=[], kicad_project=[],
                  altium_intlib=[], altium_lib=[])

    if p.is_file():
        _add(result, p, detect_file(p))
        return result

    if not p.is_dir():
        return result

    sch, brd = {}, {}
    for child in sorted(p.iterdir()):
        if child.is_dir():
            if _is_fp_folder(child):
                n = sum(1 for _ in child.glob('*.kicad_mod'))
                result['kicad_fp_lib'].append(
                    Artifact('kicad_fp_lib', f'{child.name}  ({n} footprints)', [str(child)]))
        elif child.is_file():
            kind = detect_file(child)
            if kind == 'eagle_sch':
                sch[child.stem] = child
            elif kind == 'eagle_brd':
                brd[child.stem] = child
            else:
                _add(result, child, kind)

    for stem, s in sch.items():
        if stem in brd:
            result['eagle_project'].append(
                Artifact('eagle_project', stem, [str(s), str(brd[stem])]))

    return result


def _add(result, path, kind):
    if kind == 'eagle_lbr':
        result['eagle_lbr'].append(Artifact('eagle_lbr', path.name, [str(path)]))
    elif kind == 'kicad_sym':
        result['kicad_sym'].append(Artifact('kicad_sym', path.name, [str(path)]))
    elif kind == 'kicad_pro':
        result['kicad_project'].append(Artifact('kicad_project', path.name, [str(path)]))
    elif kind == 'altium_intlib':
        result['altium_intlib'].append(Artifact('altium_intlib', path.name, [str(path)]))
    elif kind == 'altium_bin':
        result['altium_lib'].append(Artifact('altium_lib', path.name, [str(path)]))
