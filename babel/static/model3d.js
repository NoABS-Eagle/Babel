/**
 * STEP 3D viewer.
 * Depends on THREE (window.THREE) and occtimportjs (window.occtimportjs)
 * loaded as classic <script> tags before this file.
 */
async function initModel3d(canvas) {
    const stepUrl = canvas.dataset.stepUrl;
    const wrap    = canvas.parentElement;

    function showError(msg) {
        console.error('[model3d]', msg);
        const div = document.createElement('div');
        div.style.cssText = 'color:#cc4444;font-size:12px;padding:6px 0';
        div.textContent   = msg;
        wrap.insertBefore(div, canvas.nextSibling);
        canvas.style.display = 'none';
    }

    if (typeof THREE === 'undefined') {
        showError('Three.js not loaded'); return;
    }
    if (typeof occtimportjs === 'undefined') {
        showError('occt-import-js not loaded'); return;
    }

    // ── Renderer ──────────────────────────────────────────────────────────────
    const W = canvas.clientWidth  || canvas.width;
    const H = canvas.clientHeight || canvas.height;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(W, H);
    renderer.setClearColor(0x1e1e36);

    const scene  = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, W / H, 0.0001, 1e6);

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.0);
    sun.position.set(1, 2, 3);
    scene.add(sun);
    const backLight = new THREE.DirectionalLight(0xaaccff, 0.35);
    backLight.position.set(-2, -1, -1);
    scene.add(backLight);

    // ── Load + parse STEP ─────────────────────────────────────────────────────
    let result;
    try {
        const occt = await occtimportjs();
        const resp = await fetch(stepUrl);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        result = occt.ReadStepFile(new Uint8Array(await resp.arrayBuffer()), null);
    } catch (e) {
        showError('Load failed: ' + e.message); return;
    }

    if (!result || !result.success || !result.meshes || !result.meshes.length) {
        showError('No geometry in STEP'); return;
    }

    // ── Build geometry ────────────────────────────────────────────────────────
    const group = new THREE.Group();
    for (const m of result.meshes) {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position',
            new THREE.Float32BufferAttribute(m.attributes.position.array, 3));
        if (m.attributes.normal)
            geo.setAttribute('normal',
                new THREE.Float32BufferAttribute(m.attributes.normal.array, 3));
        if (m.index)
            geo.setIndex(new THREE.Uint32BufferAttribute(m.index.array, 1));
        geo.computeVertexNormals();

        const color = m.color
            ? new THREE.Color(m.color[0], m.color[1], m.color[2])
            : new THREE.Color(0x5599cc);
        group.add(new THREE.Mesh(geo,
            new THREE.MeshPhongMaterial({
                color, specular: 0x222222, shininess: 50, side: THREE.DoubleSide
            })));
    }
    scene.add(group);

    // Centre + fit camera
    const box    = new THREE.Box3().setFromObject(group);
    const center = box.getCenter(new THREE.Vector3());
    group.position.sub(center);
    const span = box.getSize(new THREE.Vector3()).length();
    camera.position.set(span * 0.7, span * 0.5, span * 1.1);
    camera.lookAt(0, 0, 0);

    // ── Mouse orbit ───────────────────────────────────────────────────────────
    let drag = false, ox = 0, oy = 0, rotX = 0.3, rotY = 0.4;
    canvas.addEventListener('mousedown', e => {
        drag = true; ox = e.clientX; oy = e.clientY;
        canvas.style.cursor = 'grabbing';
    });
    window.addEventListener('mouseup', () => { drag = false; canvas.style.cursor = 'grab'; });
    canvas.addEventListener('mousemove', e => {
        if (!drag) return;
        rotY += (e.clientX - ox) * 0.008;
        rotX  = Math.max(-Math.PI/2, Math.min(Math.PI/2,
                    rotX + (e.clientY - oy) * 0.008));
        ox = e.clientX; oy = e.clientY;
    });
    canvas.addEventListener('wheel', e => {
        e.preventDefault();
        camera.position.multiplyScalar(
            Math.max(0.1, Math.min(50, 1 + e.deltaY * 0.001)));
    }, { passive: false });
    canvas.style.cursor = 'grab';

    // ── Render loop ───────────────────────────────────────────────────────────
    (function animate() {
        requestAnimationFrame(animate);
        group.rotation.x = rotX;
        group.rotation.y = rotY;
        renderer.render(scene, camera);
    })();
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('canvas[data-step-url]').forEach(c =>
        initModel3d(c).catch(e => console.error('[model3d]', e)));
});
