import type { PlanStep, RunEvent, RunResult, Target } from './api';
import { mm, deg } from './format';

export type RowKind = 'plan' | 'segment' | 'target' | 'phase' | 'result' | 'done' | 'error';

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
  lastTarget: { object?: string; target: Target } | null;
  lastMask: string | null;
  error: string | null;
}

export const initialRun: RunState = {
  phase: 'idle',
  runId: null,
  label: '',
  rows: [],
  results: [],
  ious: [],
  plan: null,
  lastTarget: null,
  lastMask: null,
  error: null,
};

export type RunAction =
  | { type: 'start'; label: string }
  | { type: 'started'; runId: string }
  | { type: 'event'; event: RunEvent & { run_id?: string } }
  | { type: 'fail'; message: string }
  | { type: 'drop' }
  | { type: 'clear' };

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
    case 'result':
      return row({
        kind: 'result',
        title: `Result · ${e.data.object ?? 'no object'}`,
        detail: e.data.note,
        mono: typeof e.data.localisation_error_mm === 'number' ? `localisation ${e.data.localisation_error_mm.toFixed(2)} mm` : undefined,
        errorMm: typeof e.data.error_mm === 'number' ? e.data.error_mm : undefined,
        ok: Boolean(e.data.in_tray),
        pill: e.data.in_tray ? 'in tray' : 'not in tray',
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
    case 'started':
      return { ...state, phase: state.phase === 'starting' ? 'running' : state.phase, runId: action.runId };
    case 'fail':
      return { ...state, phase: 'failed', error: action.message };
    case 'drop':
      if (state.phase !== 'running' && state.phase !== 'starting') return state;
      return {
        ...state,
        phase: 'failed',
        error: 'Lost the event stream before the run finished. Check the server log.',
      };
    case 'clear':
      return initialRun;
    case 'event': {
      const e = action.event;
      if (state.phase === 'idle') return state;
      if (e.run_id && state.runId && e.run_id !== state.runId) return state;
      const next: RunState = { ...state, rows: [...state.rows, eventRow(e)] };
      if (state.phase === 'starting') next.phase = 'running';
      if (e.type === 'plan') next.plan = e.data.plan;
      if (e.type === 'segment') {
        if (typeof e.data.iou_vs_truth === 'number') next.ious = [...state.ious, e.data.iou_vs_truth];
        if (e.data.mask_png) next.lastMask = e.data.mask_png;
      }
      if (e.type === 'target' && e.data.target) next.lastTarget = { object: e.data.role ?? e.data.object, target: e.data.target };
      if (e.type === 'result') next.results = [...state.results, e.data];
      if (e.type === 'done') next.phase = 'done';
      if (e.type === 'error') {
        next.phase = 'failed';
        next.error = e.data.message;
      }
      return next;
    }
  }
}

export function summarise(results: RunResult[], ious: number[]) {
  const placed = results.filter((r) => r.in_tray).length;
  const errs = results
    .map((r) => (typeof r.localisation_error_mm === 'number' ? r.localisation_error_mm : r.error_mm))
    .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  const meanErr = errs.length ? errs.reduce((a, b) => a + b, 0) / errs.length : null;
  const meanIou = ious.length ? ious.reduce((a, b) => a + b, 0) / ious.length : null;
  return { placed, total: results.length, meanErr, meanIou };
}
