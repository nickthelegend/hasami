import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api, ApiError, pickPoint, setupName, subscribe, type Camera, type ClickStep, type Pixel, type SegmentResponse, type Status } from './api';
import { CameraTile, type Marker } from './components/CameraTile';
import { InstructionCard, Readout, Results, RunsTable, StepsCard, Timeline, type PlanView, type RunsView } from './components/Panels';
import { RealCam } from './components/RealCam';
import { RunTrace } from './components/RunTrace';
import { LogoMark, StatusPills } from './components/TopBar';
import { initialRun, runReducer } from './runState';

type Notice = { tone: 'busy' | 'setup' | 'error'; text: string } | null;

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [camera, setCamera] = useState<Camera>('overhead');
  const [nonce, setNonce] = useState(() => Date.now());
  const [instruction, setInstruction] = useState('put the red cube in the tray');
  const [plan, setPlan] = useState<PlanView>({ state: 'idle' });
  const [steps, setSteps] = useState<ClickStep[]>([]);
  const [pending, setPending] = useState<Pixel | null>(null);
  const [seg, setSeg] = useState<SegmentResponse | null>(null);
  const [segBusy, setSegBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [runs, setRuns] = useState<RunsView>({ state: 'loading' });
  const [resetting, setResetting] = useState(false);
  const [scene, setScene] = useState('classic');
  const [traceId, setTraceId] = useState<string | null>(null);
  const [tab, setTab] = useState<'runs' | 'realcam'>('runs');
  const [run, dispatch] = useReducer(runReducer, initialRun);
  const sub = useRef<{ close: () => void } | null>(null);
  const [, setFrameSize] = useState<{ w: number; h: number } | null>(null);

  const refreshFrame = useCallback(() => setNonce(Date.now()), []);
  const onSize = useCallback((w: number, h: number) => setFrameSize((s) => (s && s.w === w && s.h === h ? s : { w, h })), []);

  // ---- status: poll, faster while the server is down --------------------------------------
  const loadStatus = useCallback(async () => {
    try {
      const s = await api.status();
      setStatus(s);
      setReachable((was) => {
        if (was === false) setNonce(Date.now());
        return true;
      });
    } catch (e) {
      if (e instanceof ApiError && e.kind === 'unreachable') {
        setReachable(false);
        setStatus(null);
      } else {
        setReachable(true);
        setNotice({ tone: 'error', text: `GET /api/status: ${errText(e)}` });
      }
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const t = setInterval(loadStatus, reachable === false ? 4000 : 15000);
    return () => clearInterval(t);
  }, [loadStatus, reachable]);

  const loadRuns = useCallback(async () => {
    try {
      setRuns({ state: 'ok', runs: await api.runs() });
    } catch (e) {
      setRuns({ state: 'error', message: e instanceof ApiError && e.kind === 'unreachable' ? 'Server not reachable.' : errText(e) });
    }
  }, []);

  useEffect(() => {
    if (reachable) void loadRuns();
  }, [reachable, loadRuns]);

  // ---- live frame while a run streams ---------------------------------------------------------
  const live = run.phase === 'starting' || run.phase === 'running';
  useEffect(() => {
    if (!live) return;
    const t = setInterval(refreshFrame, 650);
    return () => clearInterval(t);
  }, [live, refreshFrame]);

  useEffect(() => {
    if (run.phase === 'done' || run.phase === 'failed') {
      sub.current?.close();
      sub.current = null;
      if (run.phase === 'done' && run.runId) setTraceId(run.runId);
      refreshFrame();
      void loadRuns();
      void loadStatus();
    }
  }, [run.phase, run.runId, refreshFrame, loadRuns, loadStatus]);

  useEffect(() => () => sub.current?.close(), []);

  // ---- clicks: object, then target ---------------------------------------------------------
  const onPick = useCallback(
    async (p: Pixel) => {
      setNotice(null);
      if (!pending) {
        setPending(p);
        setSeg(null);
        setSegBusy(true);
        try {
          const res = await api.segment({ point: p, segmenter: 'local' });
          setSeg(res);
          if (!res.target) {
            // A mask with nothing above the table cannot be grasped: drop the click rather than build a doomed step.
            setPending(null);
            setNotice({ tone: 'error', text: `No graspable object at ${p[0]}, ${p[1]}: ${res.target_error ?? 'mask has no pixels above the table'}` });
          }
        } catch (e) {
          if (e instanceof ApiError && e.kind === 'setup') setNotice({ tone: 'setup', text: `Setup required: ${setupName(e.setup)}` });
          else if (e instanceof ApiError && e.kind === 'unreachable') setReachable(false);
          else setNotice({ tone: 'error', text: `Segment failed: ${errText(e)}` });
        } finally {
          setSegBusy(false);
        }
      } else {
        setSteps((s) => [...s, { pick_point: pending, place_point: p }]);
        setPending(null);
      }
    },
    [pending],
  );

  // ---- plan ---------------------------------------------------------------------------------
  const doPlan = useCallback(async () => {
    const text = instruction.trim();
    if (!text) return;
    setPlan({ state: 'loading' });
    try {
      setPlan({ state: 'ok', res: await api.plan(text) });
    } catch (e) {
      if (e instanceof ApiError && e.kind === 'setup') setPlan({ state: 'setup', name: setupName(e.setup) });
      else if (e instanceof ApiError && e.kind === 'unreachable') {
        setReachable(false);
        setPlan({ state: 'error', message: 'Server not reachable.' });
      } else setPlan({ state: 'error', message: errText(e) });
    }
  }, [instruction]);

  // ---- run ----------------------------------------------------------------------------------
  const useSteps = steps.length > 0;
  const runLabel = useSteps ? `Run ${steps.length} clicked step${steps.length === 1 ? '' : 's'}` : 'Run instruction';
  const plannerReady = status?.planner.ready ?? false;
  const plannerSetup = status && !plannerReady ? setupName(status.planner.setup ?? 'MODEL_API_KEY') : null;
  const canRun = reachable === true && !live && !resetting && !pending && (useSteps || (plannerReady && instruction.trim().length > 0));

  const doRun = useCallback(async () => {
    setNotice(null);
    const body = useSteps ? { steps } : { instruction: instruction.trim() };
    const label = useSteps ? `${steps.length} clicked step${steps.length === 1 ? '' : 's'}` : `“${instruction.trim()}”`;
    sub.current?.close();
    dispatch({ type: 'start', label });
    let dropped = false;
    const s = subscribe(
      (event) => dispatch({ type: 'event', event }),
      () => {
        if (!dropped) {
          dropped = true;
          dispatch({ type: 'drop' });
        }
      },
    );
    sub.current = s;
    await s.ready;
    try {
      const { run_id } = await api.run(body);
      dispatch({ type: 'started', runId: run_id });
      if (useSteps) setSteps([]);
    } catch (e) {
      s.close();
      sub.current = null;
      if (e instanceof ApiError && e.kind === 'busy') {
        dispatch({ type: 'clear' });
        setNotice({ tone: 'busy', text: `Busy: ${e.message} Wait for it to finish, then run again.` });
      } else if (e instanceof ApiError && e.kind === 'setup') {
        dispatch({ type: 'fail', message: `Setup required: ${setupName(e.setup)}` });
        if (!useSteps) setPlan({ state: 'setup', name: setupName(e.setup) });
      } else if (e instanceof ApiError && e.kind === 'unreachable') {
        dispatch({ type: 'fail', message: 'Server not reachable.' });
        setReachable(false);
      } else {
        dispatch({ type: 'fail', message: errText(e) });
      }
    }
  }, [useSteps, steps, instruction]);

  const doReset = useCallback(async () => {
    setNotice(null);
    setResetting(true);
    try {
      const r = (await api.reset(undefined, scene === 'classic' ? undefined : scene)) as { instruction?: string };
      if (r && typeof r.instruction === 'string') setInstruction(r.instruction);
      setSteps([]);
      setPending(null);
      setSeg(null);
      setPlan({ state: 'idle' });
      dispatch({ type: 'clear' });
      refreshFrame();
      void loadStatus();
    } catch (e) {
      if (e instanceof ApiError && e.kind === 'busy') setNotice({ tone: 'busy', text: `Busy: ${e.message}` });
      else if (e instanceof ApiError && e.kind === 'unreachable') setReachable(false);
      else setNotice({ tone: 'error', text: `Reset failed: ${errText(e)}` });
    } finally {
      setResetting(false);
    }
  }, [refreshFrame, loadStatus, scene]);

  // ---- markers: clicked steps, the pending pick, and the Muse plan ---------------------------
  const markers = useMemo<Marker[]>(() => {
    const m: Marker[] = [];
    steps.forEach((s, i) => {
      m.push({ key: `c${i}p`, n: i + 1, at: s.pick_point, kind: 'pick', source: 'click' });
      m.push({ key: `c${i}t`, n: i + 1, at: s.place_point, kind: 'place', source: 'click' });
    });
    if (pending) m.push({ key: 'pend', n: steps.length + 1, at: pending, kind: 'pick', source: 'click' });
    const planSteps = run.plan ?? (plan.state === 'ok' ? plan.res.plan : null);
    planSteps?.forEach((s, i) => {
      const pp = pickPoint(s);
      if (Array.isArray(pp)) m.push({ key: `p${i}p`, n: i + 1, at: pp, kind: 'pick', source: 'plan' });
      if (Array.isArray(s.place_point)) m.push({ key: `p${i}t`, n: i + 1, at: s.place_point, kind: 'place', source: 'plan' });
    });
    return m;
  }, [steps, pending, plan, run.plan]);

  const readout = run.lastTarget ? { target: run.lastTarget.target, iou_vs_truth: run.ious[run.ious.length - 1] } : seg;
  const lt = run.lastTarget?.object;
  const readoutLabel = lt
    ? lt === 'pick'
      ? 'Pick target'
      : lt === 'place'
        ? 'Place target'
        : lt
    : pending
      ? `Click ${steps.length + 1}: object`
      : seg
        ? 'Last click'
        : '';
  const mask = (live || run.phase === 'done') && run.lastMask ? run.lastMask : seg?.mask_png ?? null;
  const busy = resetting ? 'Resetting the scene…' : segBusy ? 'Segmenting with SAM 2.1…' : live ? 'Run streaming · live frame' : null;
  const realArmSetup = status && status.arm === 'sim' ? setupName(status.so101.setup ?? 'HASAMI_SO101_PORT') : null;

  return (
    <div className="shell">
      <div className={`env-pill${reachable === false ? ' env-off' : ''}`} role="note">
        <i aria-hidden="true" />
        {reachable === null ? (
          'Connecting to the server…'
        ) : reachable === false ? (
          'Server offline'
        ) : status?.arm === 'so101' && status.so101.connected ? (
          'Real SO-101 connected'
        ) : (
          <span>
            Simulation<span className="env-long"> · real arm: Setup required: {realArmSetup ?? 'HASAMI_SO101_PORT'}</span>
            <span className="env-short"> · real arm blocked</span>
          </span>
        )}
      </div>
      <main className="frame">
        <header className="top">
          <a className="brand" href="/" aria-label="Hasami home">
            <LogoMark />
            <span>Hasami</span>
          </a>
          <StatusPills status={status} reachable={reachable} />
          <div className="top-act">
            <label className="scene-pick">
              <span className="sr">Scene</span>
              <select value={scene} onChange={(e) => setScene(e.target.value)} disabled={!reachable || live || resetting}>
                <option value="classic">3 cubes + tray</option>
                <option value="sort-by-colour">sort by colour</option>
                <option value="stack-two">stack two</option>
                <option value="clear-except-X">clear except one</option>
                <option value="tower-of-3">tower of 3</option>
              </select>
            </label>
            <button type="button" className="btn btn-ghost" onClick={doReset} disabled={!reachable || live || resetting}>
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4h4" />
              </svg>
              {resetting ? 'Resetting…' : 'Reset scene'}
            </button>
          </div>
        </header>

        {reachable === false && (
          <div className="offline" role="alert">
            <div>
              <b>Start the server: python -m hasami_server</b>
              <p>The dashboard talks to the Hasami server on port 8420. Nothing on this page is simulated in the browser; it waits for the real sim.</p>
              <pre>pip install -e . && python -m hasami_server</pre>
            </div>
            <button type="button" className="btn btn-mint" onClick={loadStatus}>
              Retry
            </button>
          </div>
        )}

        {notice && (
          <div className={`notice notice-${notice.tone}`} role="alert">
            <span>{notice.text}</span>
            <button type="button" className="x" aria-label="Dismiss" onClick={() => setNotice(null)}>
              ×
            </button>
          </div>
        )}

        <div className="grid">
          <div className="col-main">
            <div className="title-row">
              <div className="title">
                <span className="glyphs" aria-hidden="true">
                  <span className="g1">
                    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2" />
                      <circle cx="12" cy="12" r="3" />
                    </svg>
                  </span>
                  <span className="g2">
                    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M9 3v5l-4 5v8M15 3v5l4 5v8" />
                    </svg>
                  </span>
                </span>
                <h1>
                  Live camera <span>/ {camera}</span>
                </h1>
              </div>
              <div className="seg" role="group" aria-label="Camera">
                {(['overhead', 'demo'] as Camera[]).map((c) => (
                  <button key={c} type="button" aria-pressed={camera === c} onClick={() => setCamera(c)}>
                    {c}
                  </button>
                ))}
              </div>
            </div>
            <CameraTile
              camera={camera}
              nonce={nonce}
              reachable={reachable === true}
              clickable={!live}
              markers={markers}
              mask={mask}
              busy={busy}
              onPick={onPick}
              onSize={onSize}
              awaiting={pending ? 'place' : 'pick'}
            />
            <Readout seg={readout} label={readoutLabel} />
            <Timeline run={run} />
          </div>

          <aside className="col-side">
            <InstructionCard value={instruction} onChange={setInstruction} onPlan={doPlan} plan={plan} status={status} disabled={!reachable} />
            <StepsCard
              steps={steps}
              pending={pending}
              onRemove={(i) => setSteps((s) => s.filter((_, j) => j !== i))}
              onClear={() => {
                setSteps([]);
                setPending(null);
                setSeg(null);
              }}
            />
            <button type="button" className="btn btn-run" onClick={doRun} disabled={!canRun}>
              {live ? (
                'Running…'
              ) : (
                <>
                  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
                    <path d="M7 5v14l12-7z" />
                  </svg>
                  {runLabel}
                </>
              )}
            </button>
            {pending ? (
              <p className="muted small hint">Click a target for step {steps.length + 1} before running.</p>
            ) : !useSteps && plannerSetup && reachable ? (
              <p className="muted small hint">
                Running an instruction needs the Muse planner (Setup required: <code>{plannerSetup}</code>). Clicked steps run without it.
              </p>
            ) : null}
            <Results run={run} />
          </aside>
        </div>

        {traceId && <RunTrace runId={traceId} onClose={() => setTraceId(null)} />}
        <div className="tabs" role="tablist" aria-label="More">
          {(['runs', 'realcam'] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {t === 'runs' ? 'Recorded runs' : 'Real camera'}
            </button>
          ))}
        </div>
        {tab === 'realcam' ? <RealCam /> : (
        <RunsTable view={reachable === false ? { state: 'error', message: 'Server not reachable.' } : runs} onReplay={setTraceId} active={traceId} />
        )}
      </main>
      <footer className="foot">
        Hasami — 鋏, scissors · MIT · Copyright (c) 2026 Nivesh Gajengi ·{' '}
        <a href="https://github.com/nickthelegend/hasami" target="_blank" rel="noreferrer">
          GitHub
        </a>
      </footer>
    </div>
  );
}
