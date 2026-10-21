/**
 * Model3DViewer — interactive 3D preview for CAD exports (STL / OBJ) in the
 * chat file previewer (roadmap P0 optional item: "no frontend 3D viewer —
 * users can't see what was built, only download files").
 *
 * `three` is dynamically imported inside the mount effect so the library
 * lands in its own lazy chunk and FilePreviewer stays light. The model is
 * fetched via authFetch (Authorization header), auto-centered and scaled to
 * fit, and orbit-controllable (rotate/zoom/pan). Everything is disposed on
 * unmount.
 */
import { useEffect, useRef, useState } from 'react';
import { Box, FileWarning } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

export const MODEL_EXTS = ['stl', 'obj'];

export function isModelFile(nameOrUrl) {
  const m = /\.([a-z0-9]+)(?:\?|#|$)/i.exec(nameOrUrl || '');
  return m ? MODEL_EXTS.includes(m[1].toLowerCase()) : false;
}

function extOf(nameOrUrl) {
  const m = /\.([a-z0-9]+)(?:\?|#|$)/i.exec(nameOrUrl || '');
  return m ? m[1].toLowerCase() : '';
}

async function loadObject(ext, buffer) {
  const THREE = await import('three');
  if (ext === 'stl') {
    const { STLLoader } = await import('three/examples/jsm/loaders/STLLoader.js');
    const geometry = new STLLoader().parse(buffer);
    return new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({ color: 0x8ab4f8, metalness: 0.35, roughness: 0.45 }),
    );
  }
  if (ext === 'obj') {
    const { OBJLoader } = await import('three/examples/jsm/loaders/OBJLoader.js');
    return new OBJLoader().parse(new TextDecoder().decode(buffer));
  }
  throw new Error(`Unsupported model format: .${ext}`);
}

export default function Model3DViewer({ url, name, className }) {
  const containerRef = useRef(null);
  const [phase, setPhase] = useState('loading'); // loading | ready | error
  const [errMsg, setErrMsg] = useState('');

  useEffect(() => {
    if (!url) {
      setPhase('error');
      setErrMsg('No model URL');
      return undefined;
    }
    let cancelled = false;
    const container = containerRef.current;
    setPhase('loading');

    (async () => {
      try {
        const res = await authFetch(url);
        if (!res || !res.ok) throw new Error(`HTTP ${res ? res.status : '?'}`);
        const buffer = await res.arrayBuffer();
        if (cancelled) return;

        const THREE = await import('three');
        const { OrbitControls } = await import(
          'three/examples/jsm/controls/OrbitControls.js'
        );
        const object = await loadObject(extOf(name) || extOf(url), buffer);
        if (cancelled) return;

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x111827);
        scene.add(new THREE.HemisphereLight(0xffffff, 0x30354a, 1.1));
        const dir = new THREE.DirectionalLight(0xffffff, 1.4);
        dir.position.set(4, 6, 5);
        scene.add(dir);
        scene.add(object);

        // Frame the object: center it and scale to fit the viewport.
        const box = new THREE.Box3().setFromObject(object);
        if (box.isEmpty()) throw new Error('Model contains no visible geometry');
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z) || 1;
        object.position.sub(center);
        const camera = new THREE.PerspectiveCamera(45, 1, 0.01, maxDim * 20);
        camera.position.set(maxDim * 1.2, maxDim * 0.9, maxDim * 1.6);

        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        container.innerHTML = '';
        container.appendChild(renderer.domElement);

        const grid = new THREE.GridHelper(maxDim * 4, 20, 0x374151, 0x1f2937);
        grid.position.y = box.min.y - center.y;
        scene.add(grid);

        const controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.target.set(0, 0, 0);

        const fit = () => {
          const w = container.clientWidth || 640;
          const h = container.clientHeight || 360;
          renderer.setSize(w, h, false);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
        };
        fit();
        const observer = new ResizeObserver(fit);
        observer.observe(container);

        renderer.setAnimationLoop(() => {
          controls.update();
          renderer.render(scene, camera);
        });

        setPhase('ready');
        return () => {
          observer.disconnect();
          renderer.setAnimationLoop(null);
          controls.dispose();
          renderer.dispose();
          container.innerHTML = '';
        };
      } catch (e) {
        if (!cancelled) {
          setPhase('error');
          setErrMsg(e && e.message ? e.message : 'Failed to load model');
        }
      }
      return undefined;
    })();

    return () => {
      cancelled = true;
      if (container) container.innerHTML = '';
    };
  }, [url, name]);

  if (phase === 'error') {
    return (
      <div
        data-testid="model3d-error"
        className={
          'flex flex-col items-center justify-center gap-3 rounded-lg border border-border bg-secondary/30 text-center ' +
          (className || 'h-[60vh]')
        }
      >
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
          <FileWarning className="h-6 w-6 text-muted-foreground" />
        </div>
        <p className="max-w-xs text-sm text-muted-foreground">
          Could not render the 3D model{errMsg ? ` — ${errMsg}` : ''}. Download it to
          open in your CAD tool.
        </p>
      </div>
    );
  }

  return (
    <div
      data-testid="model3d-viewer"
      className={
        'relative rounded-lg border border-border bg-background ' +
        (className || 'h-[60vh]')
      }
    >
      <div ref={containerRef} className="h-full w-full" />
      {phase === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
          Loading 3D model…
        </div>
      )}
      {phase === 'ready' && (
        <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-1.5 rounded bg-background/80 px-2 py-1 text-xs text-muted-foreground">
          <Box className="h-3.5 w-3.5" /> drag to rotate · scroll to zoom
        </div>
      )}
    </div>
  );
}
