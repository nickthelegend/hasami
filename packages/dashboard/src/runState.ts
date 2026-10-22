import type { PlanStep, RunEvent, RunResult, Target } from './api';
import { mm, deg } from './format';

export type RowKind = 'plan' | 'segment' | 'target' | 'phase' | 'verify' | 'retry' | 'result' | 'done' | 'error';

export interface TimelineRow {
  id: number;
  kind: RowKind;
  title: string;
  detail?: string;
  mono?: string;
  /** true = ok pill, false = failed pill, undefined = no pill */
  ok?: boolean;
  pill?: string;
  errorMm?: number;
}

export type RunPhase = 'idle' | 'starting' | 'running' | 'done' | 'failed';

export interface RunState {
  phase: RunPhase;
  runId: string | null;
  label: string;
  rows: TimelineRow[];
  results: RunResult[];
  ious: number[];
  plan: PlanStep[] | null;
  /** verify events seen, and how many retries */
  checks: number;
  retries: number;
  lastTarget: { object?: string; target: Target } | null;
  lastMask: string | null;
  error: string | null;
  /** events received before POST /api/run answered with our run id: kept aside, then filtered by it */
  pending: (RunEvent & { run_id?: string })[];
}

export const initialRun: RunState = {
  phase: 'idle',
  runId: null,
  label: '',
  rows: [],
  results: [],
  ious: [],
  plan: null,
  checks: 0,
  retries: 0,
  lastTarget: null,
  lastMask: null,
  error: null,
  pending: [],
};

export type RunAction =
  | { type: 'start'; label: string }
  | { type: 'started'; runId: string }
  | { type: 'event'; event: RunEvent & { run_id?: string } }
  | { type: 'fail'; message: string }
  | { type: 'drop' }
  /** the server no longer runs this run (it restarted, or the stream died): its record, if it was written */
  | { type: 'lost'; record: LostRecord | null; message: string }
  | { type: 'clear' };

/** The parts of a run record (GET /api/runs/{id}) the run log needs. */
export interface LostRecord {
  run_id?: string;
  ok?: boolean | null;
  placed?: number | null;
  total?: number | null;
  error?: string | null;
  steps?: { result?: RunResult | null }[];
}

/** While this page follows a run, does /api/status's run slot say it is over without the stream saying so?
 *  'ended': the slot is free and still names our run (read its record). 'restarted': the slot names another
 *  run or none (a new server process). null: nothing to do. */
export function lostRunCheck(state: RunState, slot: { busy: boolean; run_id: string | null } | undefined): 'ended' | 'restarted' | null {
  if (!slot || !state.runId || (state.phase !== 'starting' && state.phase !== 'running')) return null;
  if (slot.run_id !== state.runId) return 'restarted';
  return slot.busy ? null : 'ended';
}

let nextId = 1;

function row(r: Omit<TimelineRow, 'id'>): TimelineRow {
  return { id: nextId++, ...r };
}

function eventRow(e: RunEvent): TimelineRow {
  switch (e.type) {
    case 'plan': {
      const n = e.data.plan.length;
      const src = e.data.source === 'clicks' ? 'clicked' : e.data.model;
      const bits = [src, `${n} step${n === 1 ? '' : 's'}`, e.data.latency_ms != null ? `${Math.round(e.data.latency_ms)} ms` : null];
      return row({
        kind: 'plan',
        title: e.data.source === 'clicks' ? 'Plan from clicks' : 'Plan',
        detail: e.data.plan.map((s) => `${s.pick} → ${s.place}`).join(' · ') || 'Empty plan',
        mono: bits.filter(Boolean).join(' · '),
      });
    }
    case 'segment': {
      const iou = e.data.iou_vs_truth;
      const what = e.data.label ?? e.data.object ?? e.data.truth_label;
      const step = typeof e.data.step === 'number' ? `${e.data.step + 1} ` : '';
      return row({
        kind: 'segment',
        title: `Segment ${step}${e.data.role ?? ''}${what ? ` · ${what}` : ''}`.replace(/\s+/g, ' ').trim(),
        detail: e.data.target_error ?? e.data.segmenter,
        mono: typeof iou === 'number' ? `IoU ${iou.toFixed(2)} vs truth` : undefined,
        ok: e.data.target_error ? false : undefined,
        pill: e.data.target_error ? 'no target' : undefined,
      });
    }
    case 'target': {
      const t = e.data.target;
      return row({
        kind: 'target',
        title: `Target${e.data.role ? ` · ${e.data.role}` : ''}${e.data.object ? ` · ${e.data.object}` : ''}`,
        mono: t?.center ? `x ${mm(t.center[0])}  y ${mm(t.center[1])}  z ${mm(t.center[2])} mm · yaw ${deg(t.yaw)}°` : undefined,
      });
    }
    case 'phase':
      return row({
        kind: 'phase',
        title: e.data.phase,
        detail: e.data.note || undefined,
        errorMm: typeof e.data.error_mm === 'number' ? e.data.error_mm : undefined,
        ok: Boolean(e.data.ok),
      });
    case 'verify': {
      const v = e.data;
      const inj = v.injected && (v.injected.offset_mm || v.injected.slip)
        ? ` · injected ${[v.injected.offset_mm ? `${v.injected.offset_mm} mm offset` : null, v.injected.slip ? 'slip' : null].filter(Boolean).join(' + ')}`
        : '';
      const grip = v.grasp_held == null ? 'jaws never closed' : v.grasp_held ? `held (${v.jaw_gap_mm ?? '?'} mm)` : `empty grasp (${v.jaw_gap_mm ?? '?'} mm)`;
      return row({
        kind: 'verify',
        title: `Check · attempt ${v.attempt ?? 1}${inj}`,
        detail: `gripper ${grip} · pick spot ${v.pick_clear ? 'clear' : 'still occupied'} · place spot ${v.place_raised ? 'object seen' : 'nothing seen'}`,
        mono: typeof v.physics_ok === 'boolean' ? `physics ${v.physics_ok ? 'ok' : 'failed'} · check ${v.agree ? 'agrees' : 'disagrees'}` : undefined,
        ok: v.ok,
        pill: v.ok ? 'verified' : 'failed check',
      });
    }
    case 'retry':
      return row({
        kind: 'retry',
        title: `Retry · attempt ${e.data.attempt}`,
        detail: e.data.reason ?? undefined,
        mono: e.data.how,
        ok: false,
        pill: 're-segment',
      });
    case 'result':
      return row({
        kind: 'result',
        title: `Result · ${e.data.object ?? 'no object'}`,
        detail: e.data.note,
        mono: typeof e.data.localisation_error_mm === 'number' ? `localisation ${e.data.localisation_error_mm.toFixed(2)} mm` : undefined,
        errorMm: typeof e.data.error_mm === 'number' ? e.data.error_mm : undefined,
        ok: Boolean(e.data.ok ?? e.data.in_tray),
        pill: e.data.ok ?? e.data.in_tray ? `placed${(e.data.attempts ?? 1) > 1 ? ` after ${e.data.attempts} attempts` : ''}` : `failed${e.data.failure ? ` · ${e.data.failure}` : ''}`,
      });
    case 'done': {
      const d = e.data as { placed?: number; total?: number; total_ms?: number; ok?: boolean };
      const bits = [
        typeof d.placed === 'number' && typeof d.total === 'number' ? `${d.placed}/${d.total} placed` : null,
        typeof d.total_ms === 'number' ? `${(d.total_ms / 1000).toFixed(1)} s` : null,
      ].filter(Boolean);
      return row({ kind: 'done', title: 'Done', mono: bits.join(' · ') || undefined, ok: typeof d.ok === 'boolean' ? d.ok : undefined, pill: typeof d.ok === 'boolean' ? (d.ok ? 'all placed' : 'not all placed') : undefined });
    }
    case 'error':
      return row({ kind: 'error', title: 'Error', detail: e.data.message, ok: false, pill: 'failed' });
  }
}

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case 'start':
      return { ...initialRun, phase: 'starting', label: action.label };
    case 'started': {
      // GET /api/events replays from the start of the latest run, which can be the previous one: only the
      // buffered events that carry this run's id are applied.
      let next: RunState = { ...state, phase: state.phase === 'starting' ? 'running' : state.phase, runId: action.runId, pending: [] };
      for (const e of state.pending) if (e.run_id === action.runId) next = applyEvent(next, e);
      return next;
    }
    case 'fail':
      return { ...state, phase: 'failed', error: action.message };
    case 'drop':
      if (state.phase !== 'running' && state.phase !== 'starting') return state;
      return {
        ...state,
        phase: 'failed',
        error: 'Lost the event stream before the run finished. Check the server log.',
      };
    case 'lost': {
      if (state.phase !== 'running' && state.phase !== 'starting') return state;
      const rec = action.record;
      if (!rec) {
        return {
          ...state,
          phase: 'failed',
          error: action.message,
          rows: [...state.rows, row({ kind: 'error', title: 'Run lost', detail: action.message, ok: false, pill: 'lost' })],
        };
      }
      const results = (rec.steps ?? []).map((st) => st?.result).filter((r): r is RunResult => Boolean(r));
      const counts = typeof rec.placed === 'number' && typeof rec.total === 'number' ? `${rec.placed}/${rec.total} placed` : undefined;
      const failed = Boolean(rec.error);
      return {
        ...state,
        phase: failed ? 'failed' : 'done',
        error: failed ? String(rec.error) : null,
        results,
        rows: [
          ...state.rows,
          row({
            kind: failed ? 'error' : 'done',
            title: 'Finished · read from the run record',
            detail: failed ? String(rec.error) : 'The event stream ended early; the result comes from the saved record.',
            mono: counts,
            ok: typeof rec.ok === 'boolean' ? rec.ok : !failed,
            pill: rec.ok ? 'all placed' : failed ? 'failed' : 'not all placed',
          }),
        ],
      };
    }
    case 'clear':
      return initialRun;
    case 'event': {
      const e = action.event;
      if (state.phase === 'idle') return state;
      if (!state.runId) return { ...state, pending: [...state.pending, e] };
      if (e.run_id && e.run_id !== state.runId) return state;
      return applyEvent(state, e);
    }
  }
}

function applyEvent(state: RunState, e: RunEvent & { run_id?: string }): RunState {
  const next: RunState = { ...state, rows: [...state.rows, eventRow(e)] };
  if (state.phase === 'starting') next.phase = 'running';
  if (e.type === 'plan') next.plan = e.data.plan;
  if (e.type === 'segment') {
    if (typeof e.data.iou_vs_truth === 'number') next.ious = [...state.ious, e.data.iou_vs_truth];
    if (e.data.mask_png) next.lastMask = e.data.mask_png;
  }
  if (e.type === 'target' && e.data.target) next.lastTarget = { object: e.data.role ?? e.data.object, target: e.data.target };
  if (e.type === 'result') next.results = [...state.results, e.data];
  if (e.type === 'verify') next.checks = state.checks + 1;
  if (e.type === 'retry') next.retries = state.retries + 1;
  if (e.type === 'done') next.phase = 'done';
  if (e.type === 'error') {
    next.phase = 'failed';
    next.error = e.data.message;
  }
  return next;
}

export function summarise(results: RunResult[], ious: number[]) {
  const placed = results.filter((r) => r.ok ?? r.in_tray).length;
  const errs = results
    .map((r) => (typeof r.localisation_error_mm === 'number' ? r.localisation_error_mm : r.error_mm))
    .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  const meanErr = errs.length ? errs.reduce((a, b) => a + b, 0) / errs.length : null;
  const meanIou = ious.length ? ious.reduce((a, b) => a + b, 0) / ious.length : null;
  return { placed, total: results.length, meanErr, meanIou };
}
