import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api, ApiError, pickPoint, setupName, subscribe, type Camera, type ClickStep, type Pixel, type SegmentResponse, type Status } from './api';
import { CameraTile, type Marker } from './components/CameraTile';
import { InstructionCard, Readout, Results, RunsTable, StepsCard, Timeline, type PlanView, type RunsView } from './components/Panels';
import { RealCam } from './components/RealCam';
import { Twin } from './components/Twin';
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
  const [tab, setTab] = useState<'runs' | 'twin' | 'realcam'>('runs');
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

  /** Subscribe to the event stream, then start a run with `start`; the run log and camera follow it. */
  const startRun = useCallback(async (label: string, start: () => Promise<{ run_id: string }>, onSetup?: (name: string) => void) => {
    setNotice(null);
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
      const { run_id } = await start();
      dispatch({ type: 'started', runId: run_id });
      return run_id;
    } catch (e) {
      s.close();
      sub.current = null;
      if (e instanceof ApiError && e.kind === 'busy') {
        dispatch({ type: 'clear' });
        setNotice({ tone: 'busy', text: `Busy: ${e.message} Wait for it to finish, then run again.` });
      } else if (e instanceof ApiError && e.kind === 'setup') {
        dispatch({ type: 'fail', message: `Setup required: ${setupName(e.setup)}` });
        onSetup?.(setupName(e.setup));
      } else if (e instanceof ApiError && e.kind === 'unreachable') {
        dispatch({ type: 'fail', message: 'Server not reachable.' });
        setReachable(false);
      } else {
        dispatch({ type: 'fail', message: errText(e) });
      }
      return null;
    }
  }, []);

  // A run already going when this page loads (a reload mid-run, a second tab): follow it in the run log.
  // GET /api/events replays the latest run from its first event, so the log starts at the plan.
  const serverRun = status?.run?.busy ? status.run.run_id : null;
  useEffect(() => {
    if (!serverRun || sub.current || run.runId === serverRun || run.phase === 'starting' || run.phase === 'running') return;
    dispatch({ type: 'start', label: `run ${serverRun} (already running)` });
    const s = subscribe(
      (event) => dispatch({ type: 'event', event }),
      () => dispatch({ type: 'drop' }),
    );
    sub.current = s;
    dispatch({ type: 'started', runId: serverRun });
  }, [serverRun, run.runId, run.phase]);

  const doRun = useCallback(async () => {
    const body = useSteps ? { steps } : { instruction: instruction.trim() };
    const label = useSteps ? `${steps.length} clicked step${steps.length === 1 ? '' : 's'}` : `“${instruction.trim()}”`;
    const id = await startRun(label, () => api.run(body), useSteps ? undefined : (name) => setPlan({ state: 'setup', name }));
    if (id && useSteps) setSteps([]);
  }, [useSteps, steps, instruction, startRun]);

  const doTwinRun = useCallback(
    async (twinId: string, task: string, label: string) => {
      setSteps([]);
      setPending(null);
      setSeg(null);
      setScene('twin');
      const id = await startRun(label, () => api.twinRun(twinId, task));
      void loadStatus();
      return id;
    },
    [startRun, loadStatus],
  );

  const onTwinLoaded = useCallback(() => {
    setSteps([]);
    setPending(null);
    setSeg(null);
    setScene('twin');
    dispatch({ type: 'clear' });
    refreshFrame();
    void loadStatus();
  }, [refreshFrame, loadStatus]);

  const doReset = useCallback(async () => {
    setNotice(null);
    setResetting(true);
    try {
      const fam = scene === 'classic' || scene === 'twin' ? undefined : scene;
      if (scene === 'twin') setScene('classic');
      const r = (await api.reset(undefined, fam)) as { instruction?: string };
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

  return (
    <div className="shell">
      <a className="skip" href="#cam">
        Skip to the camera
      </a>
      <header className="topbar">
        <a className="brand" href="/" aria-label="Hasami home">
          <LogoMark />
          <span>Hasami</span>
          <span className="kanji" lang="ja" aria-hidden="true">
            鋏
          </span>
        </a>
        <StatusPills status={status} reachable={reachable} />
        <span className={`simlabel${reachable === false ? ' simlabel-off' : ''}`} role="note">
          <i aria-hidden="true" />
          {status?.arm === 'so101' && status.so101.connected ? 'Real SO-101 mirroring the sim' : 'MuJoCo simulation · official SO-101 model'}
        </span>
      </header>
      <main className="desk">
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
            {/* the skip link's target: under 1100 px .col-main is display: contents (no box to scroll to or focus) */}
            <div className="title-row" id="cam" tabIndex={-1}>
              <h1>
                Viewfinder <span>/ {camera}</span>
              </h1>
              <div className="seg" role="group" aria-label="Camera">
                {(['overhead', 'demo'] as Camera[]).map((c) => (
                  <button key={c} type="button" aria-pressed={camera === c} onClick={() => setCamera(c)}>
                    {c}
                  </button>
                ))}
              </div>
              <div className="top-act">
                <label className="scene-pick">
                  <span className="sr">Scene</span>
                  <select value={scene} onChange={(e) => setScene(e.target.value)} disabled={!reachable || live || resetting}>
                    <option value="classic">3 cubes</option>
                    <option value="sort-by-colour">sort</option>
                    <option value="stack-two">stack 2</option>
                    <option value="clear-except-X">clear but 1</option>
                    <option value="tower-of-3">tower of 3</option>
                    <option value="sort-by-shape">by shape</option>
                    <option value="line-up">line up</option>
                    <option value="pick-from-bin">from bin</option>
                    {scene === 'twin' && <option value="twin">twin</option>}
                  </select>
                </label>
                <button type="button" className="btn btn-ghost" onClick={doReset} disabled={!reachable || live || resetting}>
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                    <path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4h4" />
                  </svg>
                  {resetting ? 'Resetting…' : 'Reset scene'}
                </button>
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
              live={live}
            />
            <Readout seg={readout} label={readoutLabel} />
            <Timeline run={run} />
          </div>

          <aside className="col-side paper" aria-label="Notebook">
            <p className="nb-head">
              <span>Notebook</span>
              <span>{status?.scene.name ?? scene}{status?.scene.seed != null ? ` · seed ${status.scene.seed}` : ''}</span>
            </p>
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

        {traceId && <RunTrace runId={traceId} onClose={() => setTraceId(null)} onLoaded={loadRuns} />}
        <section className="register paper" aria-label="Runs, twin and real camera">
          <div className="tabs" role="tablist" aria-label="Register">
            {(['runs', 'twin', 'realcam'] as const).map((t) => (
              <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
                {t === 'runs' ? 'Recorded runs' : t === 'twin' ? 'Twin' : 'Real camera'}
              </button>
            ))}
          </div>
          {tab === 'realcam' ? (
            <RealCam />
          ) : tab === 'twin' ? (
            <Twin onRun={doTwinRun} runPhase={run.phase} runId={run.runId} onLoaded={onTwinLoaded} />
          ) : (
            <RunsTable view={reachable === false ? { state: 'error', message: 'Server not reachable.' } : runs} onReplay={setTraceId} active={traceId} />
          )}
        </section>
      </main>
      <footer className="foot">
        Hasami — 鋏, scissors · Meta Global AI Developer Hackathon · MIT · Copyright (c) 2026 Nivesh Gajengi ·{' '}
        <a href="https://github.com/nickthelegend/hasami" target="_blank" rel="noreferrer">
          GitHub
        </a>
      </footer>
    </div>
  );
}
