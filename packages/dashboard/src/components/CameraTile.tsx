import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import { api, type Camera, type Pixel } from '../api';

export interface Marker {
  key: string;
  n: number;
  at: Pixel;
  kind: 'pick' | 'place';
  /** plan markers come from Muse; click markers from the user */
  source: 'click' | 'plan';
}

interface Props {
  camera: Camera;
  nonce: number;
  reachable: boolean;
  clickable: boolean;
  markers: Marker[];
  mask: string | null;
  busy: string | null;
  onPick: (p: Pixel) => void;
  onSize: (w: number, h: number) => void;
  awaiting: 'pick' | 'place';
  /** a run is streaming: the frame refreshes on its own */
  live?: boolean;
}

const MINT: [number, number, number] = [56, 242, 196];

const INK: [number, number, number] = [27, 22, 34];

/** Paint a SAM mask PNG (luminance or alpha) as a light mint fill with a dashed cut line on its edge. */
function paintMask(canvas: HTMLCanvasElement, img: HTMLImageElement) {
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0);
  const src = ctx.getImageData(0, 0, w, h).data;
  const on = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const r = src[i * 4], g = src[i * 4 + 1], b = src[i * 4 + 2], a = src[i * 4 + 3];
    on[i] = a > 127 && (r + g + b) / 3 > 127 ? 1 : 0;
  }
  const out = ctx.createImageData(w, h);
  const d = out.data;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!on[i]) continue;
      const edge = x === 0 || y === 0 || x === w - 1 || y === h - 1 || !on[i - 1] || !on[i + 1] || !on[i - w] || !on[i + w];
      // The edge alternates mint and ink every 3 px: a cut line that reads on the pale table and the dark tray.
      const c = edge && (x + y) % 6 >= 3 ? INK : MINT;
      d[i * 4] = c[0];
      d[i * 4 + 1] = c[1];
      d[i * 4 + 2] = c[2];
      d[i * 4 + 3] = edge ? 255 : 80;
    }
  }
  ctx.putImageData(out, 0, 0);
}

export function CameraTile(p: Props) {
  const [shown, setShown] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [cursor, setCursor] = useState<Pixel | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const maskRef = useRef<HTMLCanvasElement>(null);
  const { onSize } = p;

  // Double-buffer: load the next frame off-screen, swap when it has decoded, so a streaming run never flickers.
  useEffect(() => {
    if (!p.reachable) return;
    const url = api.frameUrl(p.camera, p.nonce);
    const next = new Image();
    let live = true;
    next.onload = () => {
      if (!live) return;
      setShown(url);
      setFailed(false);
      setDims({ w: next.naturalWidth, h: next.naturalHeight });
      if (p.camera === 'overhead') onSize(next.naturalWidth, next.naturalHeight);
    };
    next.onerror = () => live && setFailed(true);
    next.src = url;
    return () => {
      live = false;
    };
  }, [p.camera, p.nonce, p.reachable, onSize]);

  useEffect(() => {
    const canvas = maskRef.current;
    if (!canvas) return;
    if (!p.mask || p.camera !== 'overhead') {
      canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    const img = new Image();
    img.onload = () => paintMask(canvas, img);
    img.src = p.mask.startsWith('data:') ? p.mask : `data:image/png;base64,${p.mask}`;
  }, [p.mask, p.camera]);

  const toPixel = (e: MouseEvent<HTMLDivElement>): Pixel | null => {
    const img = imgRef.current;
    if (!img || !dims) return null;
    const r = img.getBoundingClientRect();
    const u = ((e.clientX - r.left) / r.width) * dims.w;
    const v = ((e.clientY - r.top) / r.height) * dims.h;
    if (u < 0 || v < 0 || u >= dims.w || v >= dims.h) return null;
    return [Math.round(u), Math.round(v)];
  };

  const canClick = p.clickable && p.camera === 'overhead' && !!dims && !p.busy;

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!canClick || !dims) return;
    const step = e.shiftKey ? 32 : 8;
    const c = cursor ?? [Math.round(dims.w / 2), Math.round(dims.h / 2)];
    const move: Record<string, Pixel> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    if (move[e.key]) {
      e.preventDefault();
      setCursor([Math.min(dims.w - 1, Math.max(0, c[0] + move[e.key][0])), Math.min(dims.h - 1, Math.max(0, c[1] + move[e.key][1]))]);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      p.onPick(c);
    }
  };

  const pct = (q: Pixel) => (dims ? { left: `${(q[0] / dims.w) * 100}%`, top: `${(q[1] / dims.h) * 100}%` } : {});
  const aspect = dims ? `${dims.w} / ${dims.h}` : '4 / 3';

  return (
    <div className="vf">
    <span className="vf-cm" aria-hidden="true" />
    <div className="cam" style={{ aspectRatio: aspect }}>
      {!p.reachable ? (
        <div className="cam-empty">
          <b>No camera feed</b>
          <span>The frame comes from the running server.</span>
        </div>
      ) : failed && !shown ? (
        <div className="cam-empty">
          <b>Frame failed to load</b>
          <span className="mono">GET /api/frame.png?camera={p.camera}</span>
        </div>
      ) : !shown ? (
        <div className="cam-empty cam-loading" aria-live="polite">
          <b>Rendering the {p.camera} camera…</b>
        </div>
      ) : null}
      {shown && p.reachable && (
        <div
          className={`cam-stage${canClick ? ' is-click' : ''}`}
          role={canClick ? 'button' : undefined}
          tabIndex={canClick ? 0 : -1}
          aria-label={
            canClick
              ? `Overhead camera. Click or press Enter to mark the ${p.awaiting === 'pick' ? 'object to pick' : 'place target'}. Arrow keys move the crosshair.`
              : `${p.camera} camera frame`
          }
          onClick={(e) => {
            if (!canClick) return;
            const q = toPixel(e);
            if (q) {
              setCursor(q);
              p.onPick(q);
            }
          }}
          onKeyDown={onKey}
        >
          <img ref={imgRef} src={shown} alt={`Simulated ${p.camera} camera frame`} draggable={false} />
          <canvas ref={maskRef} className="cam-mask" aria-hidden="true" />
          {p.camera === 'overhead' &&
            p.markers.map((m) => (
              <span key={m.key} className={`mk mk-${m.kind} mk-${m.source}`} style={pct(m.at)} aria-hidden="true">
                {m.n}
              </span>
            ))}
          {canClick && cursor && <span className="xhair" style={pct(cursor)} aria-hidden="true" />}
        </div>
      )}
      <div className="osd osd-top" aria-hidden="true">
        <span className="osd-cell">
          {p.camera}
          {dims ? ` · ${dims.w}×${dims.h}` : ''}
        </span>
        {p.reachable && shown && <span className={`osd-cell osd-rec${p.live ? ' is-live' : ''}`}>{p.live ? 'live' : 'still'}</span>}
      </div>
      <div className="cam-hud" aria-live="polite">
        {p.busy ? (
          <span className="hud hud-busy">{p.busy}</span>
        ) : p.camera === 'demo' ? (
          <span className="hud">Clicks work on the overhead camera</span>
        ) : p.clickable && dims ? (
          <span className="hud">{p.awaiting === 'pick' ? 'Click an object to pick' : 'Now click where it goes'}</span>
        ) : null}
        <span className="hud hud-sim">MuJoCo simulation · official SO-101 model</span>
      </div>
    </div>
    </div>
  );
}
