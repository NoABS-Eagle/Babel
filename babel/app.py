"""Babel — EDA format converter and viewer (Flask local server)."""
import io
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from urllib.parse import quote
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, abort, send_file

from babel.svg_renderer import render_symbol, render_footprint
from babel.ir_util import symbol_pool, component_gates, resolve_model3d_file, resolved_attrs
from babel.eagle_parser import convert as eagle_parse
from babel.altium_parser import convert as altium_parse
from babel.eagle_exporter import export as eagle_export
from babel.kicad_exporter import export as kicad_export, \
    needs_variant_split, variant_symbol_name
from babel.detector import scan

app = Flask(__name__, template_folder='templates', static_folder='static')
app.jinja_env.filters['urlencode'] = lambda s: quote(str(s), safe='')


def _ensure_3d_assets():
    """Download occt-import-js JS+WASM to static/ on first run."""
    import urllib.request
    static = Path(__file__).parent / 'static'
    static.mkdir(exist_ok=True)
    base  = 'https://cdn.jsdelivr.net/npm/occt-import-js@0.0.18/dist/'
    for fname in ('occt-import-js.js', 'occt-import-js.wasm'):
        dest = static / fname
        if not dest.exists():
            try:
                print(f'Downloading {fname} ...')
                urllib.request.urlretrieve(base + fname, dest)
                print(f'  OK: {dest}')
            except Exception as e:
                print(f'  ! Could not download {fname}: {e}')

_ensure_3d_assets()

# ── Active IR state (single-library) ────────────────────────────────────────

_SESSION_IR       = Path(__file__).parent / '.session.ir.xml'
_SESSION_STEP_DIR = Path(__file__).parent / '.session.3d'   # STEP files for current session
_SESSION_SRC      = Path(__file__).parent / '.session.src'  # original source path, for the header

_ir_root: ET.Element = None
_lib_name: str = ''
_src_path: str = ''   # what the user actually opened (survives the session copy)


def _load_ir(ir_path: str, src_path: str = None):
    global _ir_root, _lib_name, _src_path
    import shutil
    tree = ET.parse(ir_path)
    _ir_root = tree.getroot()
    _lib_name = _ir_root.get('name', Path(ir_path).stem)
    _src_path = str(src_path or ir_path)
    _SESSION_SRC.write_text(_src_path, encoding='utf-8')
    shutil.copy2(ir_path, _SESSION_IR)

    # Copy 3D model files from sibling dir to session step dir
    src_step = Path(ir_path).parent / Path(ir_path).stem
    if src_step.is_dir():
        if _SESSION_STEP_DIR.exists():
            shutil.rmtree(_SESSION_STEP_DIR)
        shutil.copytree(src_step, _SESSION_STEP_DIR)
    elif _SESSION_STEP_DIR.exists():
        shutil.rmtree(_SESSION_STEP_DIR)


def _restore_session():
    """Load last session on server startup, if available."""
    global _src_path
    if _SESSION_IR.exists():
        try:
            src = (_SESSION_SRC.read_text(encoding='utf-8').strip()
                   if _SESSION_SRC.exists() else None)
            _load_ir(str(_SESSION_IR), src_path=src)
        except Exception:
            pass


def _components():
    return _ir_root.findall('component') if _ir_root is not None else []


def _component(cid):
    return _ir_root.find(f'component[@name="{cid}"]') if _ir_root is not None else None


@app.context_processor
def _globals():
    # A <project> root is the merged pool of ALL the project's libraries —
    # label it so the user knows they're not looking at one source library.
    kind = ('project' if _ir_root is not None and _ir_root.tag == 'project'
            else 'library')
    return {'lib_name': _lib_name, 'lib_kind': kind, 'lib_src': _src_path}


# ── Workspace ────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    path = request.args.get('path', '').strip()
    artifacts = error = None
    if path:
        if not Path(path).exists():
            error = f'Путь не найден: {path}'
        else:
            artifacts = scan(path)
    return render_template('workspace.html', path=path, artifacts=artifacts, error=error)


# ── Open: native IR (.swlib / .swprj) ────────────────────────────────────────

@app.route('/open/swlib')
def open_swlib():
    """Open a .swlib — or the library half of a .swprj: the pool shape is
    identical (components + <symbols> at root), the viewer just never draws
    the project's canvases (schematic/module/layout)."""
    path = request.args.get('path', '')
    if not path or not Path(path).exists():
        abort(400)
    _load_ir(path)
    return redirect(url_for('library'))


# ── Import: Eagle ─────────────────────────────────────────────────────────────

@app.route('/import/eagle')
def import_eagle():
    lbr = request.args.get('path', '')
    if not lbr or not Path(lbr).exists():
        abort(400)
    tmp = tempfile.NamedTemporaryFile(suffix='.ir.xml', delete=False)
    tmp.close()
    eagle_parse(lbr, tmp.name)
    _load_ir(tmp.name, src_path=lbr)
    return redirect(url_for('library'))


# ── Import: Altium IntLib ────────────────────────────────────────────────────

@app.route('/import/altium')
def import_altium():
    intlib = request.args.get('path', '')
    if not intlib or not Path(intlib).exists():
        abort(400)
    tmp = tempfile.NamedTemporaryFile(suffix='.ir.xml', delete=False)
    tmp.close()
    altium_parse(intlib, tmp.name)
    _load_ir(tmp.name, src_path=intlib)
    return redirect(url_for('library'))


# ── Import: KiCad (2-step) ────────────────────────────────────────────────────

@app.route('/import/kicad')
def import_kicad():
    sym = request.args.get('sym', '')
    ws  = request.args.get('workspace', str(Path(sym).parent) if sym else '')
    if not sym or not Path(sym).exists():
        abort(400)
    fp_libs = scan(ws).get('kicad_fp_lib', []) if ws else []
    return render_template('kicad_import.html',
                           sym_path=sym, sym_name=Path(sym).name,
                           fp_libs=fp_libs, workspace=ws)


@app.route('/import/kicad', methods=['POST'])
def import_kicad_do():
    sym_path = request.form.get('sym_path', '')
    fp_path  = request.form.get('fp_path', '')
    # KiCad parser not yet implemented
    return render_template('kicad_import.html',
                           sym_path=sym_path, sym_name=Path(sym_path).name,
                           fp_libs=[], workspace='', not_yet=True)


# ── Export ───────────────────────────────────────────────────────────────────

def _serialize_ir():
    """Serialize _ir_root to a clean UTF-8 XML string (same style as eagle_parser)."""
    raw = minidom.parseString(ET.tostring(_ir_root, encoding='unicode')) \
                 .toprettyxml(indent='  ')
    lines = raw.splitlines()
    clean = '\n'.join(l for l in lines if l.strip())
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            + '\n'.join(clean.splitlines()[1:]))


@app.route('/export')
def export_lib():
    if _ir_root is None:
        abort(400)
    fmt  = request.args.get('format', '')
    name = _lib_name or 'library'

    if fmt == 'swlib':
        data = _serialize_ir().encode('utf-8')
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=f'{name}.swlib',
                         mimetype='application/xml')

    if fmt == 'lbr':
        tmp_ir  = tempfile.NamedTemporaryFile(suffix='.ir.xml',  delete=False)
        tmp_lbr = tempfile.NamedTemporaryFile(suffix='.lbr',     delete=False)
        tmp_ir.close(); tmp_lbr.close()
        Path(tmp_ir.name).write_text(_serialize_ir(), encoding='utf-8')
        eagle_export(tmp_ir.name, tmp_lbr.name)
        return send_file(tmp_lbr.name, as_attachment=True,
                         download_name=f'{name}.lbr',
                         mimetype='application/xml')

    if fmt == 'kicad':
        import zipfile
        tmp_dir = tempfile.mkdtemp()
        tmp_ir  = tempfile.NamedTemporaryFile(suffix='.ir.xml', delete=False)
        tmp_ir.close()
        Path(tmp_ir.name).write_text(_serialize_ir(), encoding='utf-8')
        sym_path, pretty_dir = kicad_export(tmp_ir.name, tmp_dir)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(sym_path, Path(sym_path).name)
            for mod in Path(pretty_dir).glob('*.kicad_mod'):
                zf.write(mod, f'{Path(pretty_dir).name}/{mod.name}')
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f'{name}_kicad.zip',
                         mimetype='application/zip')

    abort(400)


# ── 3D model serving ─────────────────────────────────────────────────────────

@app.route('/step/<path:filename>')
def serve_step(filename):
    """Serve a STEP/WRL file from the session 3D directory."""
    safe = Path(filename).name   # strip path traversal
    f = _SESSION_STEP_DIR / safe
    if not f.exists():
        abort(404)
    return send_file(f, mimetype='application/octet-stream')


# ── Library view ──────────────────────────────────────────────────────────────

@app.route('/library')
def library():
    if _ir_root is None:
        return redirect(url_for('home'))
    rows = []
    pool = symbol_pool(_ir_root)
    for comp in _components():
        gates = component_gates(comp)
        fps   = comp.findall('footprint')
        pin_count = sum(len(pool[sn].findall('pin'))
                        for _gn, sn in gates if sn in pool)
        rows.append({
            'id':          comp.get('name'),
            'has_symbol':  bool(gates),
            'footprints':  [fp.get('name') for fp in fps],
            'pin_count':   pin_count,
            'library':     comp.get('library', ''),
        })
    # Source-library column exists only in a project pool (components of a
    # standalone .swlib carry no library= — the file itself is the library).
    has_library_col = any(r['library'] for r in rows)
    # Standalone orphan footprints (no component)
    orphans = [fp.get('name') for fp in _ir_root.findall('footprint')]
    return render_template('library.html', components=rows, orphans=orphans,
                           has_library_col=has_library_col)


@app.route('/library/<path:comp_id>')
def component_detail(comp_id):
    """Eagle-library-editor-style master-detail: the symbol and the FULL
    variant list are always visible; selecting a variant reveals its
    footprint, pin-mapping and RESOLVED attributes (ir_schema.md I9 — here
    only two levels exist, component/variant: a library carries no instance
    overrides)."""
    comp = _component(comp_id)
    if comp is None:
        abort(404)
    gates = component_gates(comp)
    sym_svgs = [{'gate': gname or '',
                 'svg': render_symbol(comp, _ir_root, scale=30, gate=gname)}
                for gname, _sname in gates]

    # Family schema (I10): the component's <attributes> carries every key —
    # empty value = declaration, non-empty = family-wide fact.
    attrs_el = comp.find('attributes')
    schema = [{'name': a.get('name'), 'value': a.get('value', '')}
              for a in (attrs_el.findall('attr') if attrs_el is not None else [])]
    schema_keys = {a['name'] for a in schema}

    fps = comp.findall('footprint')

    # Cross-variant pad divergence per pin (R1206 P$1/P$2 case) — the
    # granular "which pads diverge" view; the family-level verdict comes
    # from kicad_exporter.needs_variant_split (reused, not duplicated).
    pads_by_pin = {}
    for fp in fps:
        pm = fp.find('pin-mapping')
        for m in (pm.findall('map') if pm is not None else []):
            pads_by_pin.setdefault(m.get('pin'), set()).add(m.get('pad'))
    divergent_pins = {p for p, pads in pads_by_pin.items() if len(pads) > 1}

    split = needs_variant_split(comp)

    variants = []
    for fp in fps:
        fa = fp.find('attributes')
        own_nonempty = {a.get('name') for a in
                        (fa.findall('attr') if fa is not None else [])
                        if a.get('value', '')}
        resolved = resolved_attrs(comp, fp_el=fp)
        ordered = [a['name'] for a in schema] + \
                  [k for k in resolved if k not in schema_keys]
        # fp_desc is the STRUCTURAL per-variant description slot
        # (ir_schema.md «СХЕМА семейства» table: it lives on the variant by
        # design) — flagging it off-schema would be a false alarm.
        attr_rows = [{'name': k, 'value': resolved.get(k, ''),
                      'from_variant': k in own_nonempty,
                      'off_schema': k not in schema_keys and k != 'fp_desc'}
                     for k in ordered]
        pm = fp.find('pin-mapping')
        pin_map = [{'pin': m.get('pin'), 'pad': m.get('pad'),
                    'divergent': m.get('pin') in divergent_pins}
                   for m in (pm.findall('map') if pm is not None else [])]
        step_path = resolve_model3d_file(fp, _SESSION_STEP_DIR)
        variants.append({
            'id':        fp.get('name'),
            'variant':   fp.get('variant') or '',
            'fp_desc':   resolved.get('fp_desc', ''),
            'device_name': variant_symbol_name(comp, fp) if split else None,
            'svg':       render_footprint(fp, fixed_size=260),
            'pin_map':   pin_map,
            'step_url':  (url_for('serve_step', filename=step_path.name)
                          if step_path is not None else None),
            'attrs':     attr_rows,
            'diverges':  any(r['divergent'] for r in pin_map),
        })
    return render_template('component.html',
                           comp_id=comp_id, sym_svgs=sym_svgs,
                           renamed_from=comp.get('renamed-from'),
                           schema=schema, variants=variants, split=split)


# ── Dev entrypoint ────────────────────────────────────────────────────────────

_restore_session()

if __name__ == '__main__':
    if len(sys.argv) > 1:
        p = sys.argv[1]
        if p.endswith('.swlib') or p.endswith('.swprj') or p.endswith('.ir.xml'):
            _load_ir(p)
        elif p.endswith('.lbr'):
            tmp = tempfile.NamedTemporaryFile(suffix='.ir.xml', delete=False)
            tmp.close()
            eagle_parse(p, tmp.name)
            _load_ir(tmp.name)
    app.run(debug=True, port=5000)
