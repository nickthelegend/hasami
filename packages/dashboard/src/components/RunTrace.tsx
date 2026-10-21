// "Watch the agent think": a recorded run, step by step and attempt by attempt — the plan, what SAM saw
// (or the simulator's truth), the 3D target, every arm phase with its error, the gripper + camera check,
// and why it retried. Everything comes from the run record and the stored per-step images.
import { useEffect, useState } from 'react';
import { ApiError } from '../api';
import { deg, mm } from '../format';

interface Phase { phase: string; error_mm: number; ok: boolean; note?: string }
interface Seg {
  point?: [number, number] | null;
  text?: string | null;
  segmenter?: string;
  iou_vs_truth?: number | null;
  truth_label?: string | null;
  target?: { center: number[]; top_z: number; yaw: number } | null;
  target_error?: string;
  grounding?: { source: string | null; tries: { source: string; point: number[] | null; rejected: string[] }[] };
}
interface Verify {
  ok: boolean;
  grasp_held: boolean | null;
  jaw_gap_mm?: number | null;
  pick_clear: boolean;
  place_raised: boolean;
  physics_ok?: boolean;
  agree?: boolean;
}
interface Attempt {
  attempt: number;
  pick_point: number[];
  place_point: number[];
  pick?: Seg;
  place?: Seg;
  place_how?: string;
  surface_z?: number;
  grasp?: { width_mm: number; period: number | null };
  localisation_error_mm?: number;
  injected?: { offset_mm?: number; slip?: boolean };
  phases?: Phase[];
  verify?: Verify;
  failure?: string;
  note?: string;
  images?: Record<string, string>;
}
interface StepRec {
  step: number;
  pick_label: string;
  place_label: string;
  attempts?: Attempt[];
  result: { ok: boolean; object: string | null; attempts?: number; failure?: string | null; note?: string; error_mm?: number | null };
}
export interface RunRecord {
  run_id: string;
  simulation?: string;
  scene?: string;
  segmenter_name?: string;
  plan?: { source: string; model?: string | null; instruction?: string | null };
  disturb?: { offset_mm: number; slip: number } | null;
  steps: StepRec[];
  ok?: boolean;
  placed?: number;
  total?: number;
  error?: string;
}

type View = 'perception' | 'truth';

const img = (runId: string, step: number, attempt: number, kind: string) =>
  `/api/runs/${encodeURIComponent(runId)}/steps/${step}.png?attempt=${attempt}&kind=${kind}`;

function Phases({ phases }: { phases: Phase[] }) {
  const worst = Math.max(8, ...phases.map((p) => p.error_mm || 0));
  return (
    <ol className="tr-phases">
      {phases.map((p, i) => (
        <li key={i} className={p.ok ? '' : 'bad'} title={p.note || undefined}>
          <span>{p.phase}</span>
          <span className="tr-bar" aria-hidden="true">
            <i style={{ width: `${Math.min(100, ((p.error_mm || 0) / worst) * 100)}%` }} />
          </span>
          <code>{(p.error_mm ?? 0).toFixed(1)} mm</code>
        </li>
      ))}
    </ol>
  );
}

function Check({ v }: { v: Verify }) {
  const grip = v.grasp_held == null ? 'never closed' : v.grasp_held ? `held · ${v.jaw_gap_mm ?? '?'} mm` : `empty · ${v.jaw_gap_mm ?? '?'} mm`;
  return (
    <div className="tr-check">
      <span className={v.grasp_held ? 'y' : 'n'}>gripper {grip}</span>
      <span className={v.pick_clear ? 'y' : 'n'}>pick spot {v.pick_clear ? 'clear' : 'occupied'}</span>
      <span className={v.place_raised ? 'y' : 'n'}>place spot {v.place_raised ? 'object seen' : 'empty'}</span>
      {typeof v.physics_ok === 'boolean' && (
        <span className="t">
          truth: {v.physics_ok ? 'placed' : 'not placed'} · check {v.agree ? 'agrees' : 'disagrees'}
        </span>
      )}
    </div>
  );
}

function AttemptView({ runId, step, a, view, last }: { runId: string; step: number; a: Attempt; view: View; last: boolean }) {
  const has = a.images ?? {};
  const kind = view === 'truth' ? 'view_truth' : 'view';
  const t = a.pick?.target;
  const inj = a.injected && (a.injected.offset_mm || a.injected.slip);
  return (
    <li className={`tr-att${a.verify?.ok ? ' ok' : ''}`}>
      <div className="tr-att-head">
        <b>Attempt {a.attempt}</b>
        {inj ? (
          <span className="tr-inj">
            injected {a.injected?.offset_mm ? `${a.injected.offset_mm} mm offset` : ''}
            {a.injected?.offset_mm && a.injected?.slip ? ' + ' : ''}
            {a.injected?.slip ? 'slip' : ''}
          </span>
        ) : null}
        {a.verify ? (
          <span className={`ok-pill ${a.verify.ok ? 'ok' : 'no'}`}>{a.verify.ok ? 'verified' : 'check failed'}</span>
        ) : (
          <span className="ok-pill no">{a.failure ?? 'stopped'}</span>
        )}
      </div>
      <div className="tr-att-body">
        <figure className="tr-shot">
          {has.frame ? (
            <img src={img(runId, step, a.attempt, kind)} alt={`Overhead frame, step ${step + 1} attempt ${a.attempt}, with ${view === 'truth' ? 'ground-truth' : 'SAM'} masks`} loading="lazy" width={640} height={480} />
          ) : (
            <div className="tr-noimg">No stored frame for this attempt</div>
          )}
          <figcaption>
            <i className="sw pick" /> pick <i className="sw place" /> place · {view === 'truth' ? 'simulator truth' : a.pick?.segmenter ?? 'SAM'}
          </figcaption>
        </figure>
        <div className="tr-facts">
          <dl>
            <dt>Mask</dt>
            <dd>
              {a.pick?.target_error ? <span className="bad-t">{a.pick.target_error}</span> : <>IoU {typeof a.pick?.iou_vs_truth === 'number' ? a.pick.iou_vs_truth.toFixed(2) : '—'} vs truth ({a.pick?.truth_label ?? '?'})</>}
              {a.pick?.grounding?.source && <span className="muted"> · grounded by {a.pick.grounding.source}</span>}
            </dd>
            {t && (
              <>
                <dt>Target</dt>
                <dd className="mono">
                  x {mm(t.center[0])} y {mm(t.center[1])} top {mm(t.top_z)} mm · yaw {deg(t.yaw)}°
                  {typeof a.localisation_error_mm === 'number' && <span className="muted"> · {a.localisation_error_mm.toFixed(1)} mm from truth</span>}
                </dd>
              </>
            )}
            {a.place_how && (
              <>
                <dt>Place</dt>
                <dd>
                  {a.place_how}
                  {typeof a.surface_z === 'number' && <span className="mono muted"> · surface {mm(a.surface_z)} mm</span>}
                  {a.grasp && <span className="mono muted"> · grip {a.grasp.width_mm} mm</span>}
                </dd>
              </>
            )}
          </dl>
          {a.phases && a.phases.length > 0 && <Phases phases={a.phases} />}
          {a.verify && <Check v={a.verify} />}
          {!last && a.note && <p className="tr-why">Retry because: {a.note}</p>}
          {has.after && (
            <a className="tr-after" href={img(runId, step, a.attempt, 'after')} target="_blank" rel="noreferrer">
              Frame after this attempt
            </a>
          )}
        </div>
      </div>
    </li>
  );
}

export function RunTrace({ runId, onClose, onLoaded }: { runId: string; onClose: () => void; onLoaded?: () => void }) {
  const [rec, setRec] = useState<RunRecord | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<View>('perception');

  useEffect(() => {
    let off = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setRec(null);
    setErr(null);
    // The server streams "done" before it writes the record file (it sends "recorded" after), so a trace
    // opened on "done" can get a 404 for a moment: retry a 404 for up to ~5 s before showing the error.
    const load = (tries: number) => {
      fetch(`/api/runs/${encodeURIComponent(runId)}`)
        .then(async (r) => {
          const body = await r.json().catch(() => null);
          if (r.status === 404 && tries > 0) return null;
          if (!r.ok) throw new ApiError('server', r.status, (body && (body.error as string)) || `HTTP ${r.status}`);
          return body as RunRecord;
        })
        .then((b) => {
          if (off) return;
          if (b === null) timer = setTimeout(() => load(tries - 1), 500);
          else {
            setRec(b);
            onLoaded?.();
          }
        })
        .catch((e) => !off && setErr(e instanceof Error ? e.message : String(e)));
    };
    load(10);
    return () => {
      off = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId, onLoaded]);

  return (
    <section className="trace" aria-labelledby="trace-h">
      <div className="sec-head">
        <h2 id="trace-h">
          Agent trace <span className="mono muted small">{runId}</span>
        </h2>
        <div className="trace-act">
          <div className="seg" role="group" aria-label="Masks shown">
            {(['perception', 'truth'] as View[]).map((v) => (
              <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)}>
                {v}
              </button>
            ))}
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {err ? (
        <div className="empty-box">
          <b>Could not load this run</b>
          <span>{err}</span>
        </div>
      ) : !rec ? (
        <div className="empty-box">
          <b>Loading the run record…</b>
        </div>
      ) : (
        <>
          <p className="muted small trace-meta">
            {rec.simulation ?? 'MuJoCo simulation'} · plan from {rec.plan?.source === 'muse' ? rec.plan.model ?? 'Muse' : 'clicks'}
            {rec.plan?.instruction ? ` · “${rec.plan.instruction}”` : ''}
            {rec.disturb ? ` · injected disturbances: ${[rec.disturb.offset_mm ? `${rec.disturb.offset_mm} mm offset on first attempts` : null, rec.disturb.slip ? `slip p=${rec.disturb.slip}` : null].filter(Boolean).join(', ')}` : ''}
            {typeof rec.placed === 'number' ? ` · ${rec.placed}/${rec.total} placed` : ''}
          </p>
          {rec.error && <div className="notice notice-error">{rec.error}</div>}
          <ol className="tr-steps">
            {rec.steps.map((s) => (
              <li key={s.step} className="tr-step">
                <div className="tr-step-head">
                  <span className="tr-n">{s.step + 1}</span>
                  <b>
                    {s.pick_label} → {s.place_label}
                  </b>
                  <span className={`ok-pill ${s.result.ok ? 'ok' : 'no'}`}>
                    {s.result.ok ? `placed${(s.result.attempts ?? 1) > 1 ? ` after ${s.result.attempts} attempts` : ''}` : `failed${s.result.failure ? ` · ${s.result.failure}` : ''}`}
                  </span>
                </div>
                {s.attempts && s.attempts.length ? (
                  <ol className="tr-atts">
                    {s.attempts.map((a, j) => (
                      <AttemptView key={a.attempt} runId={rec.run_id} step={s.step} a={a} view={view} last={j === s.attempts!.length - 1} />
                    ))}
                  </ol>
                ) : (
                  <p className="muted small">Recorded before per-step images were kept: result only{s.result.note ? ` (${s.result.note})` : ''}.</p>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
