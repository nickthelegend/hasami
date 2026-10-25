// Real camera: a webcam frame (or an uploaded photo) of a real table, an A4 sheet for scale, and SAM 2.1
// on a clicked object -> its x / y / yaw on the table plane. Segment and locate only: the arm is simulated
// and is not connected to this camera.
import { useCallback, useEffect, useRef, useState } from 'react';
import { withWriteHeader } from '../api';
import { deg } from '../format';

type Pt = [number, number];
interface PlaneTarget { x_mm: number; y_mm: number; yaw: number; extent_mm: number[]; round: boolean; height_mm: number; parallax_corrected: boolean }
interface SegRes { mask_png: string; target: PlaneTarget | null; target_error: string | null; latency_ms: number; segmenter: string }

const CORNERS = ['top-left', 'top-right', 'bottom-right', 'bottom-left'];
type Sheet = 'auto' | 'a4' | 'a4-landscape';
interface Cal { orientation: 'portrait' | 'landscape'; orientation_source: string; size_mm: number[]; corners?: Pt[]; requested?: Sheet }
/** GET /api/realcam: the one frame the server holds (shared by every tab) and its calibration */
interface Frame { frame_id: string | null; width?: number; height?: number; source?: string; calibration?: Cal | null }

/** A 409 because another tab (or client) replaced the server's frame since this tab loaded it. */
class Replaced extends Error {}
/** A 503 naming a missing setting (HASAMI_REALCAM): shown as Setup required, not as a failure. */
class SetupRequired extends Error {}

async function call(path: string, init?: RequestInit) {
  let r: Response;
  try {
    r = await fetch(path, withWriteHeader(init));
  } catch {
    throw new Error('The Hasami server is not reachable.');
  }
  const body = await r.json().catch(() => null);
  if (r.status === 503 && body?.setup_required) {
    // the server's message already reads "Setup required: NAME (...)"; say it once
    const msg = typeof body.message === 'string' && body.message.startsWith('Setup required') ? body.message : `Setup required: ${body.setup_required}`;
    throw new SetupRequired(msg);
  }
  if (r.status === 409 && body?.replaced) throw new Replaced(body.error);
  if (!r.ok) throw new Error(body?.error ?? `HTTP ${r.status}`);
  return body;
}

export function RealCam() {
  const [frameId, setFrameId] = useState<string | null>(null);
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
  const [setup, setSetup] = useState<string | null>(null); // a Setup required answer (amber), not an error
  const [cursor, setCursor] = useState<Pt | null>(null); // the keyboard crosshair, in photo pixels
  const imgRef = useRef<HTMLImageElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Show the frame the server holds (after a grab / upload, on load, or when another tab replaced it),
  // with its calibration when it has one, so a reload does not lose the clicked corners.
  const show = useCallback((f: Frame) => {
    const c = f.frame_id ? f.calibration ?? null : null;
    setFrameId(f.frame_id);
    setSize(f.frame_id && f.width && f.height ? { w: f.width, h: f.height } : null);
    setCursor(null);
    setCorners(c?.corners ?? []);
    setCalibrated(!!c);
    setCal(c);
    if (c?.requested) setSheet(c.requested);
    setRes(null);
    setClick(null);
  }, []);

  // The server's copy is the truth: re-read it, say why the view changed.
  const resync = useCallback(async (why: string) => {
    try {
      show(await call('/api/realcam'));
    } catch {
      show({ frame_id: null });
    }
    setErr(`${why.charAt(0).toUpperCase()}${why.slice(1)}.`);
  }, [show]);

  useEffect(() => {
    let live = true;
    call('/api/realcam')
      .then((f: Frame) => live && show(f))
      .catch((e) => live && setErr(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [show]);

  const grab = useCallback(async () => {
    setErr(null);
    setSetup(null);
    setBusy('Grabbing a frame…');
    try {
      show(await call('/api/realcam/grab', { method: 'POST' }));
    } catch (e) {
      if (e instanceof SetupRequired) setSetup(e.message);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [show]);

  const upload = useCallback(async (f: File) => {
    setErr(null);
    setSetup(null);
    setBusy('Uploading…');
    try {
      show(await call('/api/realcam/upload', { method: 'POST', body: f }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [show]);

  const forget = useCallback(async () => {
    setErr(null);
    setBusy('Forgetting the photo…');
    try {
      await call('/api/realcam/clear', { method: 'POST' });
      show({ frame_id: null });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [show]);

  const calibrate = useCallback(async (pts: Pt[], as: Sheet) => {
    setBusy('Fitting the table plane…');
    try {
      const c = await call('/api/realcam/calibrate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ frame_id: frameId, corners: pts, sheet: as, hfov_deg: 70 }),
      });
      setCal(c);
      setCalibrated(true);
      setRes(null);
      setClick(null);
    } catch (e) {
      if (e instanceof Replaced) return void (await resync(e.message));
      setErr(e instanceof Error ? e.message : String(e));
      setCorners([]);
      setCalibrated(false);
      setCal(null);
    } finally {
      setBusy(null);
    }
  }, [frameId, resync]);

  const changeSheet = (v: Sheet) => {
    setSheet(v);
    setErr(null);
    // already calibrated: refit the same four clicks the new way, so the clicks are not lost
    if (calibrated && corners.length === 4) void calibrate(corners, v);
  };

  // A click or Enter / Space at the crosshair: the next sheet corner, or (once calibrated) the object.
  const mark = useCallback(
    async (p: Pt) => {
      if (!size || busy) return;
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
      // the pixel the click is in, whole and inside the photo (x = w - 0.3 is column w - 1, never w)
      const px: Pt = [Math.min(size.w - 1, Math.max(0, Math.floor(p[0]))), Math.min(size.h - 1, Math.max(0, Math.floor(p[1])))];
      setClick(px);
      setBusy('Segmenting with SAM 2.1…');
      try {
        setRes(
          await call('/api/realcam/segment', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frame_id: frameId, point: px, height_mm: h }),
          }),
        );
      } catch (e) {
        if (e instanceof Replaced) return void (await resync(e.message));
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [size, busy, calibrated, corners, height, sheet, calibrate, frameId, resync],
  );

  const onClick = (ev: React.MouseEvent<HTMLDivElement>) => {
    const img = imgRef.current;
    if (!size || !img) return;
    const rect = img.getBoundingClientRect();
    const p: Pt = [((ev.clientX - rect.left) / rect.width) * size.w, ((ev.clientY - rect.top) / rect.height) * size.h];
    if (p[0] < 0 || p[1] < 0 || p[0] >= size.w || p[1] >= size.h) return;
    setCursor([Math.round(p[0]), Math.round(p[1])]);
    void mark(p);
  };

  // The same with the keyboard, as on the overhead camera: arrows move a crosshair (8 px, Shift 32 px).
  const onKey = (ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (!size) return;
    const step = ev.shiftKey ? 32 : 8;
    const c: Pt = cursor ?? [Math.round(size.w / 2), Math.round(size.h / 2)];
    const move: Record<string, Pt> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (move[ev.key]) {
      ev.preventDefault();
      setCursor([Math.min(size.w - 1, Math.max(0, c[0] + move[ev.key][0])), Math.min(size.h - 1, Math.max(0, c[1] + move[ev.key][1]))]);
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      setCursor(c);
      void mark(c);
    }
  };

  const pct = (p: Pt) => (size ? { left: `${(p[0] / size.w) * 100}%`, top: `${(p[1] / size.h) * 100}%` } : {});
  const t = res?.target;
  const step = !size ? 'Grab a webcam frame or upload a photo of the table with an A4 sheet on it.' : !calibrated
    ? `Click the sheet's ${CORNERS[corners.length]} corner (${corners.length + 1} of 4, as you look at it).`
    : 'Type the object height, then click the object.';
  const keysHelp = 'Arrow keys move the crosshair (Shift: 32 px), Enter or Space marks it.';
  const photoLabel = !calibrated
    ? `Photo. Mark the sheet's ${CORNERS[Math.min(corners.length, 3)]} corner, ${Math.min(corners.length + 1, 4)} of 4. ${keysHelp}`
    : `Photo. Mark the object to locate (height ${height || '?'} mm). ${keysHelp}`;

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
        {frameId && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={forget} disabled={!!busy}
            title="Remove this photo from the server, not only from this page">
            Forget photo
          </button>
        )}
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
      {setup && <div className="notice notice-setup" role="status">{setup}</div>}
      {err && <div className="notice notice-error" role="alert">{err}</div>}
      {size && frameId ? (
        <div className="rc-view" role="button" tabIndex={0} aria-label={photoLabel} aria-disabled={!!busy || undefined}
          onClick={onClick} onKeyDown={onKey}>
          <img ref={imgRef} src={`/api/realcam/frame.png?frame_id=${frameId}`} alt="Real camera frame" width={size.w} height={size.h} draggable={false} />
          {res?.mask_png && <img className="rc-mask" src={`data:image/png;base64,${res.mask_png}`} alt="" aria-hidden="true" />}
          {corners.map((c, i) => (
            <span key={i} className="rc-dot" style={pct(c)} aria-hidden="true">{i + 1}</span>
          ))}
          {click && <span className="rc-dot rc-click" style={pct(click)} aria-hidden="true" />}
          {cursor && <span className="xhair" style={pct(cursor)} aria-hidden="true" />}
        </div>
      ) : (
        <div className="empty-box">
          <b>No real frame yet</b>
          <span>The webcam is opened only when you press Grab (needs HASAMI_REALCAM on the server). A photo works too. The server keeps one frame, shared by every tab, until you press Forget photo or it stops.</span>
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
