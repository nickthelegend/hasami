import { pickPoint, type ClickStep, type PlanResponse, type RecordedRun, type SegmentResponse, type Status } from '../api';
import { mm, deg, px, when } from '../format';
import type { RunState, TimelineRow } from '../runState';
import { summarise } from '../runState';

const Arrow = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

// ---- 3D target readout ------------------------------------------------------------------

type ReadoutData = Pick<SegmentResponse, 'target' | 'iou_vs_truth' | 'target_error' | 'segmenter' | 'latency_ms'>;

export function Readout({ seg, label }: { seg: ReadoutData | null; label: string }) {
  const t = seg?.target ?? null;
  const cells: [string, string, string?][] = [
    ['Centre x', mm(t?.center?.[0]), 'mm'],
    ['Centre y', mm(t?.center?.[1]), 'mm'],
    ['Centre z', mm(t?.center?.[2]), 'mm'],
    ['Yaw', deg(t?.yaw), '°'],
    ['Pixels', t ? String(t.pixels) : '—'],
    ['IoU vs truth', typeof seg?.iou_vs_truth === 'number' ? seg.iou_vs_truth.toFixed(3) : '—'],
  ];
  return (
    <div className="readout" aria-label="3D target from mask and depth">
      <div className="readout-head">
        <span className="dot-mint" aria-hidden="true" />
        <b>{t ? label || 'Target' : seg?.target_error ? 'Mask found, no 3D target' : 'No target yet'}</b>
        <span className={`mono ${seg?.target_error && !t ? 'readout-err' : 'muted'}`}>
          {t
            ? `centroid ${px(t.centroid_px)} px${seg?.segmenter ? ` · ${seg.segmenter}` : ''}${seg?.latency_ms != null ? ` · ${Math.round(seg.latency_ms)} ms` : ''}`
            : seg?.target_error ?? 'Click an object on the overhead camera'}
        </span>
      </div>
      <dl className="readout-grid">
        {cells.map(([k, v, unit]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd className="mono">
              {v}
              {unit && v !== '—' ? <small>{unit}</small> : null}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ---- instruction + Muse plan ---------------------------------------------------------

export type PlanView =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'ok'; res: PlanResponse }
  | { state: 'setup'; name: string }
  | { state: 'error'; message: string };

export function InstructionCard(props: {
  value: string;
  onChange: (v: string) => void;
  onPlan: () => void;
  plan: PlanView;
  status: Status | null;
  disabled: boolean;
}) {
  const objects = props.status?.scene.objects ?? [];
  return (
    <section className="card" aria-labelledby="ins-h">
      <div className="card-head">
        <h2 id="ins-h">Instruction</h2>
        <span className="muted small">Muse plans from the frame</span>
      </div>
      <form
        className="ins"
        onSubmit={(e) => {
          e.preventDefault();
          props.onPlan();
        }}
      >
        <label className="sr" htmlFor="ins">
          What should the arm do?
        </label>
        <textarea
          id="ins"
          rows={2}
          value={props.value}
          placeholder="put the red cube in the tray"
          onChange={(e) => props.onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              props.onPlan();
            }
          }}
        />
        <button className="btn btn-lilac" type="submit" disabled={props.disabled || !props.value.trim() || props.plan.state === 'loading'}>
          {props.plan.state === 'loading' ? 'Planning…' : 'Plan'}
        </button>
      </form>
      {objects.length > 0 && (
        <div className="chips" aria-label="Objects in the scene">
          {objects
            .filter((o) => !/tray/i.test(o.name))
            .map((o) => (
              <button key={o.name} type="button" className="chip" onClick={() => props.onChange(`put the ${o.label} in the tray`)}>
                {o.label}
              </button>
            ))}
        </div>
      )}
      <PlanBody plan={props.plan} />
    </section>
  );
}

function PlanBody({ plan }: { plan: PlanView }) {
  if (plan.state === 'idle') return null;
  if (plan.state === 'loading') return <div className="plan plan-loading" aria-live="polite">Asking Muse for a plan…</div>;
  if (plan.state === 'setup')
    return (
      <div className="setup" role="alert">
        <b>Setup required: {plan.name}</b>
        <p>
          The Muse planner needs a Meta Model API key. Get one at{' '}
          <a href="https://dev.meta.ai/api-keys/" target="_blank" rel="noreferrer">
            dev.meta.ai
          </a>
          , put it in <code>.env</code> as <code>{plan.name}=</code> and restart the server.
        </p>
        <p className="setup-alt">No plan is shown without it. You can still click an object, then a target, on the overhead camera: that path runs without a key.</p>
      </div>
    );
  if (plan.state === 'error')
    return (
      <div className="err" role="alert">
        <b>Planning failed</b>
        <p>{plan.message}</p>
      </div>
    );
  const r = plan.res;
  return (
    <div className="plan" aria-live="polite">
      <div className="plan-meta mono">
        {r.model} · {Math.round(r.latency_ms)} ms · {r.plan.length} step{r.plan.length === 1 ? '' : 's'}
      </div>
      {r.plan.length === 0 ? (
        <p className="muted">Muse returned an empty plan for this instruction.</p>
      ) : (
        <ol>
          {r.plan.map((s, i) => (
            <li key={i}>
              <span className="n">P{i + 1}</span>
              <span>
                <b>{s.pick}</b> → <b>{s.place}</b>
              </span>
              <code>
                {px(pickPoint(s))} → {px(s.place_point)}
              </code>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---- clicked steps -------------------------------------------------------------------------

export function StepsCard(props: {
  steps: ClickStep[];
  pending: [number, number] | null;
  onRemove: (i: number) => void;
  onClear: () => void;
}) {
  const empty = props.steps.length === 0 && !props.pending;
  return (
    <section className="card" aria-labelledby="steps-h">
      <div className="card-head">
        <h2 id="steps-h">Clicked steps</h2>
        {!empty && (
          <button type="button" className="link" onClick={props.onClear}>
            Clear
          </button>
        )}
      </div>
      {empty ? (
        <p className="muted small empty">Click an object on the overhead camera, then the spot it should go. SAM 2.1 segments from the click; no API key needed.</p>
      ) : (
        <ol className="steps">
          {props.steps.map((s, i) => (
            <li key={i}>
              <span className="n">{i + 1}</span>
              <code>
                pick {px(s.pick_point)} → place {px(s.place_point)}
              </code>
              <button type="button" className="x" aria-label={`Remove step ${i + 1}`} onClick={() => props.onRemove(i)}>
                ×
              </button>
            </li>
          ))}
          {props.pending && (
            <li className="pending">
              <span className="n">{props.steps.length + 1}</span>
              <code>pick {px(props.pending)} → click a target…</code>
            </li>
          )}
        </ol>
      )}
    </section>
  );
}

// ---- results --------------------------------------------------------------------------------

export function Results({ run }: { run: RunState }) {
  const s = summarise(run.results, run.ious);
  const has = s.total > 0;
  return (
    <section className="card card-results" aria-labelledby="res-h">
      <div className="card-head">
        <h2 id="res-h">Results</h2>
        <span className="muted small">{run.phase === 'idle' ? 'No run yet' : run.label}</span>
      </div>
      <div className="bigs">
        <div>
          <span className="big">
            {has ? s.placed : '—'}
            {has && <span className="of">/{s.total}</span>}
          </span>
          <span className="cap">placed in the tray</span>
        </div>
        <div>
          <span className="big">
            {s.meanErr != null ? s.meanErr.toFixed(1) : '—'}
            {s.meanErr != null && <span className="of">mm</span>}
          </span>
          <span className="cap">mean localisation error</span>
        </div>
        <div>
          <span className="big">{s.meanIou != null ? s.meanIou.toFixed(2) : '—'}</span>
          <span className="cap">mean IoU vs truth</span>
        </div>
      </div>
      <p className="muted small foot-note">From this run's events. Placed is read from the physics state; the arm itself only sees the gripper reading and the camera.</p>
    </section>
  );
}

// ---- timeline -----------------------------------------------------------------------------

const KIND_LABEL: Record<TimelineRow['kind'], string> = {
  plan: 'plan',
  segment: 'segment',
  target: 'target',
  phase: 'phase',
  verify: 'check',
  retry: 'retry',
  result: 'result',
  done: 'done',
  error: 'error',
};

export function Timeline({ run }: { run: RunState }) {
  return (
    <section className="tl-wrap" aria-labelledby="tl-h">
      <div className="sec-head">
        <h2 id="tl-h">Timeline</h2>
        <span className={`run-state rs-${run.phase}`}>
          {run.phase === 'idle' ? 'idle' : run.phase === 'starting' ? 'starting…' : run.phase === 'running' ? 'running' : run.phase === 'done' ? 'done' : 'failed'}
        </span>
      </div>
      {run.phase === 'idle' ? (
        <div className="empty-box">
          <b>No run yet</b>
          <span>Plan an instruction or click steps, then press Run. Every event from the server streams in here.</span>
        </div>
      ) : (
        <ol className="tl" aria-live="polite">
          {run.rows.length === 0 && <li className="tl-row tl-wait">Waiting for the first event…</li>}
          {run.rows.map((r) => (
            <li key={r.id} className={`tl-row tl-${r.kind}`}>
              <span className="tl-kind mono">{KIND_LABEL[r.kind]}</span>
              <span className="tl-main">
                <b>{r.title}</b>
                {r.detail && <span className="muted">{r.detail}</span>}
                {r.mono && <code>{r.mono}</code>}
              </span>
              <span className="tl-end">
                {r.errorMm != null && <code className="tl-err">{r.errorMm.toFixed(1)} mm</code>}
                {r.ok !== undefined && <span className={`ok-pill ${r.ok ? 'ok' : 'no'}`}>{r.pill ?? (r.ok ? 'ok' : 'off')}</span>}
              </span>
            </li>
          ))}
          {run.error && run.phase === 'failed' && !run.rows.some((r) => r.kind === 'error') && (
            <li className="tl-row tl-error">
              <span className="tl-kind mono">error</span>
              <span className="tl-main">
                <b>Run failed</b>
                <span className="muted">{run.error}</span>
              </span>
              <span className="tl-end">
                <span className="ok-pill no">failed</span>
              </span>
            </li>
          )}
        </ol>
      )}
    </section>
  );
}

// ---- recorded runs ---------------------------------------------------------------------------

function runCounts(r: RecordedRun): string {
  if (typeof r.placed === 'number' && typeof r.total === 'number') return `${r.placed}/${r.total}`;
  if (r.error) return 'error';
  if (Array.isArray(r.results) && r.results.length) return `${r.results.filter((x) => x.in_tray).length}/${r.results.length}`;
  return '—';
}

function videoHref(r: RecordedRun): string | null {
  const v = r.video_url ?? r.video;
  if (!v || typeof v !== 'string') return null;
  if (/^https?:\/\//.test(v) || v.startsWith('/')) return v;
  return null;
}

export type RunsView = { state: 'loading' } | { state: 'ok'; runs: RecordedRun[] } | { state: 'error'; message: string };

export function RunsTable({ view, onReplay, active }: { view: RunsView; onReplay?: (id: string) => void; active?: string | null }) {
  return (
    <section className="runs" aria-labelledby="runs-h">
      <div className="sec-head">
        <h2 id="runs-h">Runs</h2>
        <span className="muted small">Recorded in deployments/runs</span>
      </div>
      {view.state === 'loading' ? (
        <div className="empty-box">
          <b>Loading runs…</b>
        </div>
      ) : view.state === 'error' ? (
        <div className="empty-box">
          <b>Could not load runs</b>
          <span>{view.message}</span>
        </div>
      ) : view.runs.length === 0 ? (
        <div className="empty-box">
          <b>No recorded runs yet</b>
          <span>Each finished run writes a record here, with its video when one was recorded.</span>
        </div>
      ) : (
        <div className="table" role="table" aria-label="Recorded runs">
          <div className="tr th" role="row">
            <span role="columnheader">Run</span>
            <span role="columnheader">Instruction</span>
            <span role="columnheader">Placed</span>
            <span role="columnheader">IoU</span>
            <span role="columnheader">When</span>
            <span role="columnheader">Video</span>
            <span role="columnheader">Trace</span>
          </div>
          {view.runs.map((r, i) => {
            const id = r.run_id ?? r.id ?? `run ${i + 1}`;
            const href = videoHref(r);
            const v = r.video_url ?? r.video;
            return (
              <div className="tr" role="row" key={`${id}-${i}`}>
                <span role="cell" className="mono td-id">
                  {id}
                </span>
                <span role="cell" className="td-ins">
                  {r.instruction || <span className="muted">{r.source === 'clicks' || !r.source ? 'clicked steps' : r.source}</span>}
                  {r.error && <span className="td-err">{r.error}</span>}
                </span>
                <span role="cell" className={`td-n${r.ok === false || r.error ? ' td-bad' : ''}`} title={r.error ?? undefined}>
                  {runCounts(r)}
                </span>
                <span role="cell" className="mono td-iou">
                  {typeof r.iou_mean === 'number' ? r.iou_mean.toFixed(2) : '—'}
                </span>
                <span role="cell" className="muted td-when">
                  {when(r.started_at ?? r.created_at ?? r.finished_at)}
                </span>
                <span role="cell" className="td-v">
                  {href ? (
                    <a className="vlink" href={href} target="_blank" rel="noreferrer">
                      Watch <Arrow />
                    </a>
                  ) : v ? (
                    <code className="muted">{String(v).split('/').pop()}</code>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </span>
                <span role="cell" className="td-r">
                  {onReplay && r.run_id ? (
                    <button type="button" className="btn btn-ghost btn-sm" aria-pressed={active === r.run_id} onClick={() => onReplay(r.run_id!)}>
                      Replay
                    </button>
                  ) : null}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

export { Arrow };
