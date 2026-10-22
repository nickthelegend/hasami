// Real camera: a webcam frame (or an uploaded photo) of a real table, an A4 sheet for scale, and SAM 2.1
// on a clicked object -> its x / y / yaw on the table plane. Segment and locate only: the arm is simulated
// and is not connected to this camera.
import { useCallback, useRef, useState } from 'react';
import { withWriteHeader } from '../api';
import { deg } from '../format';

type Pt = [number, number];
interface PlaneTarget { x_mm: number; y_mm: number; yaw: number; extent_mm: number[]; round: boolean; height_mm: number; parallax_corrected: boolean }
interface SegRes { mask_png: string; target: PlaneTarget | null; target_error: string | null; latency_ms: number; segmenter: string }

const CORNERS = ['top-left', 'top-right', 'bottom-right', 'bottom-left'];
type Sheet = 'auto' | 'a4' | 'a4-landscape';
interface Cal { orientation: 'portrait' | 'landscape'; orientation_source: string; size_mm: number[] }

async function call(path: string, init?: RequestInit) {
  let r: Response;
  try {
    r = await fetch(path, withWriteHeader(init));
  } catch {
    throw new Error('The Hasami server is not reachable.');
  }
  const body = await r.json().catch(() => null);
  if (r.status === 503 && body?.setup_required) throw new Error(`Setup required: ${body.setup_required}. ${body.message ?? ''}`);
  if (!r.ok) throw new Error(body?.error ?? `HTTP ${r.status}`);
  return body;
}

export function RealCam() {
  const [nonce, setNonce] = useState(0);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [corners, setCorners] = useState<Pt[]>([]);
  const [calibrated, setCalibrated] = useState(false);
  const [sheet, setSheet] = useState<Sheet>('auto');
  const [cal, setCal] = useState<Cal | null>(null);
  const [height, setHeight] = useState('30');
  const [res, setRes] = useState<SegRes | null>(null);
  const [click, setClick] = useState<Pt | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loaded = (w: number, h: number) => {
    setSize({ w, h });
    setCorners([]);
    setCalibrated(false);
    setCal(null);
    setRes(null);
    setClick(null);
    setNonce(Date.now());
  };

  const grab = useCallback(async () => {
    setErr(null);
    setBusy('Grabbing a frame…');
    try {
      const b = await call('/api/realcam/grab', { method: 'POST' });
      loaded(b.width, b.height);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const upload = useCallback(async (f: File) => {
    setErr(null);
    setBusy('Uploading…');
    try {
      const b = await call('/api/realcam/upload', { method: 'POST', body: f });
      loaded(b.width, b.height);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const calibrate = useCallback(async (pts: Pt[], as: Sheet) => {
    setBusy('Fitting the table plane…');
    try {
      const c = await call('/api/realcam/calibrate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ corners: pts, sheet: as, hfov_deg: 70 }),
      });
      setCal(c);
      setCalibrated(true);
      setRes(null);
      setClick(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setCorners([]);
      setCalibrated(false);
      setCal(null);
    } finally {
      setBusy(null);
    }
  }, []);

  const changeSheet = (v: Sheet) => {
    setSheet(v);
    setErr(null);
    // already calibrated: refit the same four clicks the new way, so the clicks are not lost
    if (calibrated && corners.length === 4) void calibrate(corners, v);
  };

  const onClick = useCallback(
    async (ev: React.MouseEvent<HTMLImageElement>) => {
      if (!size || busy) return;
      const rect = ev.currentTarget.getBoundingClientRect();
      const p: Pt = [((ev.clientX - rect.left) / rect.width) * size.w, ((ev.clientY - rect.top) / rect.height) * size.h];
      setErr(null);
      if (!calibrated) {
        const next = [...corners, p];
        setCorners(next);
        if (next.length === 4) await calibrate(next, sheet);
        return;
      }
      const h = Number(height);
      if (!Number.isFinite(h) || h < 0 || h > 300) {
        setErr('Type the object height in mm (0-300) first.');
        return;
      }
      setClick(p);
      setBusy('Segmenting with SAM 2.1…');
      try {
        setRes(
          await call('/api/realcam/segment', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ point: p, height_mm: h }),
          }),
        );
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [size, busy, calibrated, corners, height, sheet, calibrate],
  );

  const pct = (p: Pt) => (size ? { left: `${(p[0] / size.w) * 100}%`, top: `${(p[1] / size.h) * 100}%` } : {});
  const t = res?.target;
  const step = !size ? 'Grab a webcam frame or upload a photo of the table with an A4 sheet on it.' : !calibrated
    ? `Click the sheet's ${CORNERS[corners.length]} corner (${corners.length + 1} of 4, as you look at it).`
    : 'Type the object height, then click the object.';

  return (
    <section className="realcam" aria-labelledby="rc-h">
      <div className="sec-head">
        <h2 id="rc-h">Real camera</h2>
        <span className="rc-arm">arm: simulation · not connected to this camera</span>
      </div>
      <div className="rc-act">
        <button type="button" className="btn btn-ghost btn-sm" onClick={grab} disabled={!!busy}>
          Grab webcam frame
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()} disabled={!!busy}>
          Upload photo
        </button>
        <input ref={fileRef} type="file" accept="image/png,image/jpeg" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <label className="rc-h">
          height
          <input inputMode="decimal" value={height} onChange={(e) => setHeight(e.target.value)} aria-label="Object height in millimetres" />
          mm
        </label>
        <label className="rc-h">
          sheet
          <select value={sheet} onChange={(e) => changeSheet(e.target.value as Sheet)} disabled={!!busy} aria-label="A4 sheet orientation, as you look at it">
            <option value="auto">auto (from the corners)</option>
            <option value="a4">portrait</option>
            <option value="a4-landscape">landscape</option>
          </select>
        </label>
        {/* Kept in the layout (hidden) until there is a click to undo, so the first click on the photo
            never pushes the photo down under the next one on a narrow screen. */}
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          style={corners.length > 0 || calibrated ? undefined : { visibility: 'hidden' }}
          onClick={() => { setCorners([]); setCalibrated(false); setCal(null); setRes(null); }}
          disabled={!!busy || !(corners.length > 0 || calibrated)}
        >
          Re-click sheet
        </button>
      </div>
      <p className="muted small rc-step" aria-live="polite">{busy ?? step}</p>
      {err && <div className="notice notice-error" role="alert">{err}</div>}
      {size ? (
        <div className="rc-view">
          <img ref={imgRef} src={`/api/realcam/frame.png?t=${nonce}`} alt="Real camera frame" onClick={onClick} width={size.w} height={size.h} />
          {res?.mask_png && <img className="rc-mask" src={`data:image/png;base64,${res.mask_png}`} alt="" aria-hidden="true" />}
          {corners.map((c, i) => (
            <span key={i} className="rc-dot" style={pct(c)}>{i + 1}</span>
          ))}
          {click && <span className="rc-dot rc-click" style={pct(click)} />}
        </div>
      ) : (
        <div className="empty-box">
          <b>No real frame yet</b>
          <span>The webcam is opened only when you press Grab (needs HASAMI_REALCAM on the server). A photo works too.</span>
        </div>
      )}
      {cal && (
        <p className="muted small rc-sheet">
          Sheet: A4 {cal.orientation} ({cal.size_mm.map((v) => v.toFixed(0)).join(' × ')} mm across × down), {cal.orientation_source}.
        </p>
      )}
      {res && (
        <dl className="rc-out">
          {t ? (
            <>
              <div><dt>x</dt><dd>{t.x_mm.toFixed(1)} mm</dd></div>
              <div><dt>y</dt><dd>{t.y_mm.toFixed(1)} mm</dd></div>
              <div><dt>yaw</dt><dd>{t.round ? 'round' : `${deg(t.yaw)}°`}</dd></div>
              <div><dt>size</dt><dd>{t.extent_mm.map((v) => v.toFixed(0)).join(' × ')} mm</dd></div>
              <div><dt>height</dt><dd>{t.height_mm} mm (typed)</dd></div>
              <div><dt>parallax</dt><dd>{t.parallax_corrected ? 'corrected (assumed 70° FOV)' : 'not corrected'}</dd></div>
            </>
          ) : (
            <div><dt>target</dt><dd className="bad-t">{res.target_error}</dd></div>
          )}
        </dl>
      )}
      <p className="muted small">x / y are measured from the sheet's first clicked corner, along its top and left edges as you clicked them. Unverified against a ruler yet.</p>
    </section>
  );
}
