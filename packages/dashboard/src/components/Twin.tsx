// Your desk, twinned: one photo with an A4 sheet on it becomes a MuJoCo scene, shown side by side with the
// photo, and the simulated SO-101 runs a task on it. Labelled everywhere: the arm is simulated.
import { useCallback, useEffect, useRef, useState } from 'react';
import { deg } from '../format';

export const TWIN_LABEL = 'digital twin from one photo · simulated arm';

interface TwinObj {
  index: number;
  kind: 'box' | 'cylinder';
  colour: string;
  size_mm: number[];
  height_source: 'typed' | 'estimated' | 'default';
  fit_iou: number;
  x_m: number;
  y_m: number;
  yaw_world: number;
  in_twin: boolean;
  reachable: boolean;
  graspable: boolean;
  twin_notes: string[];
  notes: string[];
}
interface TwinRes {
  twin_id: string;
  label: string;
  photo: { width: number; height: number; source: string };
  sheet: { corners_source: string; landscape: boolean; hfov_deg: number; camera_mm: number[] };
  objects: TwinObj[];
  stats: { masks: number; dropped_shadow: number; dropped_flat: number; dropped_loose: number; total_ms: number; in_twin: number };
  tasks: { id: string; instruction: string; steps: number }[];
  images: { side_by_side: string; photo: string };
}
interface Goal { ok: boolean; detail: Record<string, boolean> }

async function call(path: string, init?: RequestInit) {
  let r: Response;
  try {
    r = await fetch(path, init);
  } catch {
    throw new Error('The Hasami server is not reachable.');
  }
  const body = await r.json().catch(() => null);
  if (r.status === 409) throw new Error(`Busy: ${body?.error ?? 'a run is in progress'}.`);
  if (!r.ok) throw new Error(body?.error ?? `HTTP ${r.status}`);
  return body;
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;

const SIDES = [
  ['far', 'beyond the top edge'],
  ['near', 'beyond the bottom edge'],
  ['left', 'left of the sheet'],
  ['right', 'right of the sheet'],
] as const;

export function Twin({
  onRun,
  runPhase,
  runId,
  onLoaded,
}: {
  onRun: (twinId: string, task: string, label: string) => Promise<string | null>;
  runPhase: string;
  runId: string | null;
  onLoaded: () => void;
}) {
  const [twin, setTwin] = useState<TwinRes | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hfov, setHfov] = useState('70');
  const [side, setSide] = useState<string>('far');
  const [heights, setHeights] = useState<Record<number, string>>({});
  const [task, setTask] = useState('');
  const [nonce, setNonce] = useState(0);
  const [myRun, setMyRun] = useState<string | null>(null);
  const [goal, setGoal] = useState<Goal | null>(null);
  const [sampleSeed, setSampleSeed] = useState(0);
  const [restored, setRestored] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const built = useRef(false); // a twin was built in this tab: never replace it with the restored one

  const shown = (t: TwinRes, fromServer = false) => {
    if (!fromServer) built.current = true;
    setRestored(fromServer);
    setTwin(t);
    setHeights({});
    setGoal(null);
    setMyRun(null);
    setTask((cur) => (t.tasks.some((x) => x.id === cur) ? cur : t.tasks[0]?.id ?? ''));
    setNonce(Date.now());
  };

  const build = useCallback(
    async (body: Blob, opts: { hfov: number; source: 'upload' | 'synthetic' }) => {
      setErr(null);
      setBusy('Finding the sheet, SAM 2.1 automatic masks, fitting a box or cylinder to each (a few seconds on CPU)…');
      try {
        const q = new URLSearchParams({ hfov_deg: String(opts.hfov), side, source: opts.source });
        shown(await call(`/api/twin?${q}`, { method: 'POST', body }));
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [side],
  );

  // After a reload (or coming back to this tab) the twin is still in the server's memory: show the latest one.
  useEffect(() => {
    let stop = false;
    (async () => {
      try {
        const r = await call('/api/twin');
        if (!stop && r?.twin && !built.current) shown(r.twin, true);
      } catch (e) {
        if (!stop) setErr(`Could not read the last twin from the server: ${e instanceof Error ? e.message : String(e)}`);
      }
    })();
    return () => {
      stop = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const upload = (f: File) => {
    const h = Number(hfov);
    if (!Number.isFinite(h) || h < 20 || h > 150) {
      setErr('Field of view: 20-150 degrees (a phone main camera is about 65-70).');
      return;
    }
    void build(f, { hfov: h, source: 'upload' });
  };

  const sample = async () => {
    setErr(null);
    setBusy('Rendering a synthetic desk photo…');
    try {
      const r = await fetch(`/api/twin/sample.png?seed=${sampleSeed}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const h = Number(r.headers.get('X-Hasami-Hfov') ?? '70');
      setSampleSeed((s) => s + 1);
      await build(await r.blob(), { hfov: h, source: 'synthetic' });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  };

  const rebuild = async () => {
    if (!twin) return;
    const typed: Record<string, number> = {};
    for (const [k, v] of Object.entries(heights)) {
      if (v.trim() === '') continue;
      const n = Number(v);
      if (!Number.isFinite(n) || n < 5 || n > 150) {
        setErr(`Height for object ${k}: 5-150 mm.`);
        return;
      }
      typed[k] = n;
    }
    setErr(null);
    setBusy('Refitting with the typed heights…');
    try {
      shown(await call(`/api/twin/${twin.twin_id}/adjust`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ heights_mm: typed, side }),
      }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const load = async () => {
    if (!twin) return;
    setErr(null);
    setBusy('Loading the twin into the simulator…');
    try {
      await call(`/api/twin/${twin.twin_id}/load`, { method: 'POST' });
      onLoaded();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const run = async () => {
    if (!twin || !task) return;
    setErr(null);
    setGoal(null);
    const t = twin.tasks.find((x) => x.id === task);
    const id = await onRun(twin.twin_id, task, `twin: ${t?.instruction ?? task}`);
    setMyRun(id);
  };

  // when this tab's run finishes, read the goal check from the run record
  useEffect(() => {
    if (!myRun || runId !== myRun || (runPhase !== 'done' && runPhase !== 'failed')) return;
    let stop = false;
    (async () => {
      for (let i = 0; i < 10 && !stop; i++) {
        try {
          const rec = await call(`/api/runs/${myRun}`);
          if (rec?.goal) {
            setGoal(rec.goal);
            return;
          }
        } catch {
          /* the record is written just after `done`; try again */
        }
        await new Promise((r) => setTimeout(r, 500));
      }
    })();
    return () => {
      stop = true;
    };
  }, [myRun, runId, runPhase]);

  const running = runPhase === 'starting' || runPhase === 'running';
  const objs = twin?.objects ?? [];

  return (
    <section className="twin" aria-labelledby="tw-h">
      <div className="sec-head">
        <h2 id="tw-h">Your desk, twinned</h2>
        <span className="rc-arm">{TWIN_LABEL}</span>
      </div>
      <p className="muted small tw-lede">
        Put an A4 sheet on the desk and photograph it with the objects. The sheet gives the table plane and the
        camera's pose; SAM 2.1 finds every object; each becomes an upright box or cylinder in MuJoCo, with the
        SO-101 standing {SIDES.find(([s]) => s === side)?.[1]}, 215 mm from the sheet's centre.
      </p>
      <div className="rc-act">
        <button type="button" className="btn btn-lilac btn-sm" onClick={() => fileRef.current?.click()} disabled={!!busy || running}>
          Upload a desk photo
        </button>
        <input ref={fileRef} type="file" accept="image/png,image/jpeg" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) upload(f); }} />
        <button type="button" className="btn btn-ghost btn-sm" onClick={sample} disabled={!!busy || running} title="A MuJoCo-rendered desk photo, labelled synthetic">
          Try a synthetic photo
        </button>
        <label className="rc-h">
          FOV
          <input inputMode="decimal" value={hfov} onChange={(e) => setHfov(e.target.value)} aria-label="Camera horizontal field of view in degrees" />
          °
        </label>
        <label className="rc-h">
          arm
          <select value={side} onChange={(e) => setSide(e.target.value)} aria-label="Where the simulated arm stands">
            {SIDES.map(([v, t]) => (
              <option key={v} value={v}>{t}</option>
            ))}
          </select>
        </label>
      </div>
      <p className="muted small rc-step" aria-live="polite">{busy ?? (twin ? `Twin ${twin.twin_id} · ${twin.photo.source === 'synthetic' ? 'synthetic photo (MuJoCo render)' : 'your photo'} · ${twin.stats.total_ms ? (twin.stats.total_ms / 1000).toFixed(1) + ' s' : ''}${restored ? ' · restored from the server' : ''}` : 'No photo yet.')}</p>
      {err && <div className="notice notice-error" role="alert">{err}</div>}

      {twin ? (
        <>
          <figure className="tw-sbs">
            <img src={`${twin.images.side_by_side}?t=${nonce}`} alt={`Left: the ${twin.photo.source === 'synthetic' ? 'synthetic ' : ''}photo with the sheet, SAM masks and fitted shapes drawn. Right: the MuJoCo twin rendered from the same viewpoint, with the simulated SO-101.`} width={twin.photo.width * 2 + 8} height={twin.photo.height + Math.max(28, Math.floor(twin.photo.height / 18))} />
            <figcaption className="muted small">
              {twin.photo.source === 'synthetic' ? 'Synthetic photo (MuJoCo render)' : 'Photo'} | twin from the photo's own viewpoint ·
              sheet {twin.sheet.corners_source}, {twin.sheet.landscape ? 'landscape' : 'portrait'}, FOV {Math.round(twin.sheet.hfov_deg)}° ·
              {' '}{plural(twin.stats.masks, 'mask')}: {plural(objs.length, 'object')}, {plural(twin.stats.dropped_shadow, 'shadow')}, {twin.stats.dropped_flat} flat, {twin.stats.dropped_loose} unexplained
            </figcaption>
          </figure>

          {objs.length ? (
            <div className="tw-table-wrap">
              <table className="tw-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>object</th>
                    <th>x, y (mm)</th>
                    <th>yaw</th>
                    <th>size (mm)</th>
                    <th>height</th>
                    <th>twin</th>
                  </tr>
                </thead>
                <tbody>
                  {objs.map((o) => (
                    <tr key={o.index}>
                      <td>{o.index}</td>
                      <td>{o.colour} {o.kind}</td>
                      <td>{Math.round(o.x_m * 1000)}, {Math.round(o.y_m * 1000)}</td>
                      <td>{o.kind === 'cylinder' ? 'round' : `${deg(o.yaw_world)}°`}</td>
                      <td>{o.size_mm.slice(0, o.kind === 'cylinder' ? 1 : 2).map((v) => v.toFixed(0)).join(' × ')}{o.kind === 'cylinder' ? ' ⌀' : ''}</td>
                      <td>
                        <input
                          className="tw-h"
                          inputMode="decimal"
                          placeholder={o.size_mm[2].toFixed(0)}
                          value={heights[o.index] ?? ''}
                          onChange={(e) => setHeights((h) => ({ ...h, [o.index]: e.target.value }))}
                          aria-label={`Height of object ${o.index} in mm (now ${o.size_mm[2].toFixed(0)}, ${o.height_source})`}
                        />
                        <span className="muted"> {o.height_source}</span>
                      </td>
                      <td className={o.in_twin && o.reachable && o.graspable ? '' : 'bad-t'}>
                        {[...(o.in_twin ? (o.reachable && o.graspable ? ['in reach'] : []) : []), ...o.twin_notes, ...o.notes].join('; ') || 'in reach'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-box">
              <b>No objects found</b>
              <span>Nothing on the photo fitted an upright box or cylinder. Objects the colour of the paper or the table are missed; try a plainer background.</span>
            </div>
          )}

          <div className="rc-act tw-run">
            <button type="button" className="btn btn-ghost btn-sm" onClick={rebuild} disabled={!!busy || running}>
              Refit with typed heights
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={load} disabled={!!busy || running}>
              Show in the arm's camera
            </button>
            {twin.tasks.length ? (
              <>
                <select value={task} onChange={(e) => setTask(e.target.value)} aria-label="Task to run on the twin">
                  {twin.tasks.map((t) => (
                    <option key={t.id} value={t.id}>{t.instruction}</option>
                  ))}
                </select>
                <button type="button" className="btn btn-mint btn-sm" onClick={run} disabled={!!busy || running || !task}>
                  {running && myRun ? 'Running on the twin…' : 'Run on the twin'}
                </button>
              </>
            ) : (
              <span className="muted small">No task: no object is both reachable and graspable.</span>
            )}
          </div>
          <p className="muted small">
            Steps are planned by the simulated-click oracle (it knows which object each step moves), then the
            simulated arm runs them with the same SAM 2.1 grounding, gripper and camera checks, and retries.
            Watch it in the camera above.
          </p>
          {goal && (
            <div className={`notice ${goal.ok ? 'tw-ok' : 'notice-error'}`} role="status">
              <span>
                Goal {goal.ok ? 'met' : 'not met'} on the twin's physics:{' '}
                {Object.entries(goal.detail).map(([k, v]) => `${k.replace(/^twin\d+_/, '')} ${v ? '✓' : '✗'}`).join(' · ')}
              </span>
            </div>
          )}
        </>
      ) : (
        !busy && (
          <div className="empty-box">
            <b>No twin yet</b>
            <span>Upload a photo of a desk with an A4 sheet fully in view and a few objects around it, or try a synthetic photo. The photo stays on this machine; SAM 2.1 runs locally.</span>
          </div>
        )
      )}
    </section>
  );
}
