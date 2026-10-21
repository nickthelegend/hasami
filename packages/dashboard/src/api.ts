// Typed client for the Hasami server (packages/server, default port 8420).
// Contract: specs/hasami.md "HTTP contract". Every failure surfaces as an ApiError with a kind
// the UI can act on; nothing here invents data when the server cannot answer.

export type Camera = 'overhead' | 'demo';
export type Pixel = [number, number];

export interface Status {
  arm: 'sim' | 'so101';
  so101: { connected: boolean; port: string | null; setup?: string };
  planner: { ready: boolean; model: string; setup?: string };
  segmenters: { name: string; model?: string; prompt?: string; ready: boolean; setup?: string; note?: string }[];
  scene: { objects: { name: string; label: string }[]; seed?: number | null; name?: string };
  sim?: string;
}

export interface Target {
  center: [number, number, number]; // world metres
  top_z: number; // metres
  yaw: number; // radians
  pixels: number;
  centroid_px: Pixel;
}

export interface SegmentRequest {
  point?: Pixel;
  text?: string;
  segmenter?: 'local' | 'meta';
}

export interface SegmentResponse {
  mask_png: string; // base64 PNG, same size as the overhead frame
  /** null when the mask has no pixels above the table; target_error says why */
  target: Target | null;
  iou_vs_truth?: number | null;
  target_error?: string;
  truth_label?: string | null;
  segmenter?: string;
  latency_ms?: number;
  mask_pixels?: number;
}

export interface PlanStep {
  pick: string;
  /** the contract calls it point; hasami_planner.PlanStep calls it pick_point */
  point?: Pixel;
  pick_point?: Pixel;
  place: string;
  place_point: Pixel;
  reason?: string;
}

export const pickPoint = (s: PlanStep): Pixel | undefined => s.point ?? s.pick_point;

export interface PlanResponse {
  plan: PlanStep[];
  model: string;
  latency_ms: number;
  source?: string;
}

export interface ClickStep {
  pick_point: Pixel;
  place_point: Pixel;
}

export type RunRequest = { instruction: string } | { steps: ClickStep[] };

export interface RunResponse {
  run_id: string;
}

/** One phase of a pick-and-place (hasami_sim.arm.StepLog). */
export interface StepLog {
  attempt?: number;
  phase: string;
  target: number[];
  reached: number[];
  error_mm: number;
  ok: boolean;
  note?: string;
}

export interface RunResult {
  object: string | null;
  in_tray: boolean;
  /** attempts this step took (1 + retries) */
  attempts?: number;
  /** the camera + gripper check passed on the last attempt */
  verified?: boolean;
  /** ground-truth failure class when ok is false */
  failure?: 'segment' | 'grasp' | 'place' | null;
  /** where the object ended up vs the place target */
  error_mm: number | null;
  /** depth-fused centre vs simulator truth, when the server reports it */
  localisation_error_mm?: number;
  ok?: boolean;
  note?: string;
  step?: number;
}

/** A recorded run from deployments/runs/*.json. Fields beyond run_id vary by recorder version. */
export interface RecordedRun {
  run_id?: string;
  id?: string;
  instruction?: string | null;
  started_at?: string | number;
  created_at?: string | number;
  finished_at?: string | number;
  mode?: string;
  arm?: string;
  results?: RunResult[];
  placed?: number | null;
  total?: number | null;
  ok?: boolean | null;
  source?: string | null;
  segmenter?: string | null;
  iou_mean?: number | null;
  error?: string | null;
  file?: string;
  video?: string | null;
  video_url?: string | null;
  [key: string]: unknown;
}

export type ApiErrorKind = 'unreachable' | 'busy' | 'setup' | 'bad_request' | 'server';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  /** For kind === 'setup': the variable or device the server says is missing, e.g. MODEL_API_KEY. */
  readonly setup?: string;

  constructor(kind: ApiErrorKind, status: number, message: string, setup?: string) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.setup = setup;
  }
}

/** Pull the credential name out of a setup string such as "Setup required: MODEL_API_KEY (...)". */
export function setupName(setup: string | undefined | null): string {
  if (!setup) return 'unknown';
  const rest = setup.replace(/^\s*setup required:?\s*/i, '').trim();
  if (rest !== setup.trim() && rest) return rest.split(/\s+/)[0];
  const m = setup.match(/[A-Z][A-Z0-9_]{2,}/);
  return m ? m[0] : rest || setup;
}

function detailText(body: unknown): string | undefined {
  if (!body || typeof body !== 'object') return undefined;
  const b = body as Record<string, unknown>;
  const d = b.detail ?? b.error ?? b.message;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object') {
    const inner = d as Record<string, unknown>;
    if (typeof inner.message === 'string') return inner.message;
    if (typeof inner.setup_required === 'string') return `Setup required: ${inner.setup_required}`;
    return JSON.stringify(d);
  }
  return undefined;
}

function setupField(body: unknown): string | undefined {
  if (!body || typeof body !== 'object') return undefined;
  const b = body as Record<string, unknown>;
  if (typeof b.setup_required === 'string') return b.setup_required;
  const d = b.detail;
  if (d && typeof d === 'object' && typeof (d as Record<string, unknown>).setup_required === 'string') {
    return (d as Record<string, string>).setup_required;
  }
  return undefined;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: init?.body ? { 'Content-Type': 'application/json', ...init.headers } : init?.headers,
    });
  } catch {
    throw new ApiError('unreachable', 0, 'The Hasami server is not reachable.');
  }
  const text = await res.text();
  let body: unknown = undefined;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  if (res.ok) {
    if (body === undefined) throw new ApiError('server', res.status, `Expected JSON from ${path}, got: ${text.slice(0, 120)}`);
    return body as T;
  }
  // A dev proxy with nothing behind it (or a static host without the API) answers with a non-JSON body.
  if (body === undefined) {
    throw new ApiError('unreachable', res.status, 'The Hasami server is not reachable.');
  }
  const setup = setupField(body);
  if (res.status === 503 && setup) {
    throw new ApiError('setup', 503, `Setup required: ${setup}`, setup);
  }
  if (res.status === 409) {
    throw new ApiError('busy', 409, detailText(body) ?? 'The arm is busy with another run.');
  }
  const msg = detailText(body) ?? `HTTP ${res.status} from ${path}`;
  throw new ApiError(res.status >= 400 && res.status < 500 ? 'bad_request' : 'server', res.status, msg);
}

const post = <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) });

export const api = {
  status: () => request<Status>('/api/status'),
  frameUrl: (camera: Camera, nonce: number | string) => `/api/frame.png?camera=${camera}&t=${nonce}`,
  segment: (req: SegmentRequest) => post<SegmentResponse>('/api/segment', req),
  plan: async (instruction: string): Promise<PlanResponse> => {
    const r = await post<Record<string, unknown>>('/api/plan', { instruction });
    return normalisePlan(r);
  },
  run: (req: RunRequest) => post<RunResponse>('/api/run', req),
  reset: (seed?: number, family?: string) =>
    post<unknown>('/api/reset', { ...(seed === undefined ? {} : { seed }), ...(family ? { family } : {}) }),
  runs: async (): Promise<RecordedRun[]> => {
    const body = await request<RecordedRun[] | { runs: RecordedRun[] }>('/api/runs');
    return Array.isArray(body) ? body : Array.isArray(body?.runs) ? body.runs : [];
  },
};

/** Accept {plan: [...], model, latency_ms} (contract) or {plan: {steps, model, source}, latency_ms}. */
function normalisePlan(r: Record<string, unknown>): PlanResponse {
  const p = r.plan;
  const inner = p && typeof p === 'object' && !Array.isArray(p) ? (p as Record<string, unknown>) : {};
  const steps = Array.isArray(p) ? p : Array.isArray(inner.steps) ? inner.steps : Array.isArray(r.steps) ? r.steps : [];
  const model = (typeof r.model === 'string' && r.model) || (typeof inner.model === 'string' && inner.model) || 'muse';
  const latency = typeof r.latency_ms === 'number' ? r.latency_ms : 0;
  const source = typeof inner.source === 'string' ? inner.source : typeof r.source === 'string' ? r.source : undefined;
  return { plan: steps as PlanStep[], model, latency_ms: latency, source };
}

// ---- server-sent events ---------------------------------------------------------------

export type RunEvent =
  | { type: 'plan'; data: { plan: PlanStep[]; model?: string | null; latency_ms?: number; source?: string; instruction?: string | null } }
  | {
      type: 'segment';
      data: {
        object?: string;
        label?: string;
        role?: 'pick' | 'place';
        step?: number;
        truth_label?: string | null;
        iou_vs_truth?: number | null;
        segmenter?: string;
        mask_png?: string;
        target?: Target | null;
        target_error?: string;
      };
    }
  | { type: 'target'; data: { object?: string; role?: 'pick' | 'place'; step?: number; target: Target | null } }
  | { type: 'phase'; data: StepLog & { object?: string; step?: number } }
  | { type: 'result'; data: RunResult }
  | { type: 'verify'; data: VerifyData }
  | { type: 'retry'; data: RetryData }
  | { type: 'done'; data: Record<string, unknown> }
  | { type: 'error'; data: { message: string; setup_required?: string } };

/** After each attempt: the gripper reading and the camera's verdict (physics_ok is ground truth, for scoring). */
export interface VerifyData {
  step?: number;
  attempt?: number;
  ok: boolean;
  grasp_held: boolean | null;
  jaw_gap_mm?: number | null;
  completed?: boolean;
  pick_clear: boolean;
  place_raised: boolean;
  physics_ok?: boolean;
  agree?: boolean;
  injected?: { offset_mm?: number; slip?: boolean };
}

export interface RetryData {
  step?: number;
  attempt: number;
  reason: string | null;
  how?: string;
  pick_point?: Pixel | null;
}

export const EVENT_TYPES = ['plan', 'segment', 'target', 'phase', 'verify', 'retry', 'result', 'done', 'error'] as const;
export type EventType = (typeof EVENT_TYPES)[number];

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
}

/** Normalise one SSE payload into a RunEvent. Accepts named events or {type, data} envelopes. */
export function parseEvent(name: string, raw: string): (RunEvent & { run_id?: string }) | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return name === 'error' ? { type: 'error', data: { message: raw } } : null;
  }
  let rec = asRecord(parsed);
  let type = name;
  if (name === 'message') {
    const t = rec.type ?? rec.event;
    if (typeof t !== 'string') return null;
    type = t;
  }
  if (!(EVENT_TYPES as readonly string[]).includes(type)) return null;
  const run_id = typeof rec.run_id === 'string' ? rec.run_id : undefined;
  if (rec.data && typeof rec.data === 'object' && !Array.isArray(rec.data)) rec = asRecord(rec.data);
  switch (type) {
    case 'plan':
      return {
        type,
        run_id,
        data: {
          plan: Array.isArray(rec.plan)
            ? (rec.plan as PlanStep[])
            : Array.isArray(rec.steps)
              ? (rec.steps as PlanStep[])
              : Array.isArray(parsed)
                ? (parsed as PlanStep[])
                : [],
          model: typeof rec.model === 'string' ? rec.model : undefined,
          latency_ms: typeof rec.latency_ms === 'number' ? rec.latency_ms : undefined,
          source: typeof rec.source === 'string' ? rec.source : undefined,
          instruction: typeof rec.instruction === 'string' ? rec.instruction : undefined,
        },
      };
    case 'target': {
      const target = 'target' in rec ? ((rec.target ?? null) as Target | null) : (rec as unknown as Target);
      return {
        type,
        run_id,
        data: {
          object: typeof rec.object === 'string' ? rec.object : undefined,
          role: rec.role === 'pick' || rec.role === 'place' ? rec.role : undefined,
          step: typeof rec.step === 'number' ? rec.step : undefined,
          target,
        },
      };
    }
    case 'phase':
      return { type, run_id, data: (rec.log && typeof rec.log === 'object' ? { ...rec, ...asRecord(rec.log) } : rec) as unknown as StepLog };
    case 'result':
      return { type, run_id, data: rec as unknown as RunResult };
    case 'error': {
      const message = typeof rec.message === 'string' ? rec.message : typeof rec.error === 'string' ? rec.error : JSON.stringify(rec);
      return { type, run_id, data: { message, setup_required: typeof rec.setup_required === 'string' ? rec.setup_required : undefined } };
    }
    case 'segment':
      return { type, run_id, data: rec as Extract<RunEvent, { type: 'segment' }>['data'] };
    case 'verify':
      return { type, run_id, data: rec as unknown as VerifyData };
    case 'retry':
      return { type, run_id, data: rec as unknown as RetryData };
    default:
      return { type: 'done', run_id, data: rec };
  }
}

/**
 * Open /api/events and call onEvent for each run event. Resolves once the stream is open
 * (or after a short grace period) so the caller can start the run without missing events.
 */
export function subscribe(
  onEvent: (e: RunEvent & { run_id?: string }) => void,
  onDrop: () => void,
): { ready: Promise<void>; close: () => void } {
  const es = new EventSource('/api/events');
  let opened = false;
  const ready = new Promise<void>((resolve) => {
    es.addEventListener('open', () => {
      opened = true;
      resolve();
    });
    setTimeout(resolve, 1500);
  });
  const handle = (name: string) => (ev: Event) => {
    const me = ev as MessageEvent<string>;
    if (typeof me.data !== 'string') {
      if (name === 'error' && opened && es.readyState === EventSource.CLOSED) onDrop();
      return;
    }
    const parsed = parseEvent(name, me.data);
    if (parsed) onEvent(parsed);
  };
  for (const t of EVENT_TYPES) es.addEventListener(t, handle(t));
  es.addEventListener('message', handle('message'));
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) onDrop();
  };
  return { ready, close: () => es.close() };
}
