// node --test (bundled by `pnpm test`): the run reducer against the server's real event order.
// GET /api/events replays from the start of the latest run, so a page that subscribes before POST /api/run
// first receives the previous run's events, then "started" names the new run.
import test from 'node:test';
import assert from 'node:assert/strict';
import { initialRun, lostRunCheck, runReducer, type RunAction, type RunState } from './runState';

const ev = (run_id: string, event: Extract<RunAction, { type: 'event' }>['event']): RunAction => ({ type: 'event', event: { ...event, run_id } });
const target = { center: [0.2, 0.0, 0.0125] as [number, number, number], top_z: 0.025, yaw: 0, pixels: 440, centroid_px: [350, 260] as [number, number] };

test("events from the previous run, replayed before 'started', never reach the new run", () => {
  let s: RunState = runReducer(initialRun, { type: 'start', label: '1 clicked step' });
  s = runReducer(s, ev('OLD', { type: 'segment', data: { role: 'pick', iou_vs_truth: 0.92 } }));
  s = runReducer(s, ev('OLD', { type: 'result', data: { object: 'red cube', in_tray: true, error_mm: 5.4, ok: true } }));
  s = runReducer(s, ev('OLD', { type: 'done', data: { ok: true, placed: 1, total: 1 } }));
  s = runReducer(s, { type: 'started', runId: 'NEW' });
  assert.equal(s.phase, 'running');
  assert.deepEqual(s.rows, []);
  assert.deepEqual(s.results, []);
  assert.deepEqual(s.ious, []);
  s = runReducer(s, ev('NEW', { type: 'target', data: { role: 'pick', target } }));
  s = runReducer(s, ev('NEW', { type: 'done', data: { ok: true, placed: 1, total: 1 } }));
  assert.equal(s.phase, 'done');
  assert.equal(s.rows.length, 2);
});

test("the new run's own events that arrive before 'started' are kept, in order", () => {
  let s: RunState = runReducer(initialRun, { type: 'start', label: '1 clicked step' });
  s = runReducer(s, ev('OLD', { type: 'done', data: { ok: true } }));
  s = runReducer(s, ev('NEW', { type: 'segment', data: { role: 'pick', iou_vs_truth: 0.71 } }));
  s = runReducer(s, ev('NEW', { type: 'target', data: { role: 'pick', target } }));
  s = runReducer(s, { type: 'started', runId: 'NEW' });
  assert.equal(s.phase, 'running');
  assert.deepEqual(s.ious, [0.71]);
  assert.deepEqual(s.rows.map((r) => r.kind), ['segment', 'target']);
  assert.equal(s.lastTarget?.target.pixels, 440);
});

// A server restart (or a stream that died quietly) leaves the page on "running" unless something ends the run:
// the status poll says the server's run slot is free and names another run (or none).
test('a run the server no longer has ends instead of staying on running', () => {
  let s: RunState = runReducer(initialRun, { type: 'start', label: '3 clicked steps' });
  s = runReducer(s, { type: 'started', runId: 'R1' });
  assert.equal(lostRunCheck(s, { busy: true, run_id: 'R1' }), null, 'still running on the server');
  assert.equal(lostRunCheck(s, { busy: false, run_id: 'R1' }), 'ended');
  assert.equal(lostRunCheck(s, { busy: false, run_id: null }), 'restarted');
  assert.equal(lostRunCheck(s, { busy: true, run_id: 'R2' }), 'restarted');
  assert.equal(lostRunCheck(s, undefined), null, 'no status yet');
  assert.equal(lostRunCheck(runReducer(s, ev('R1', { type: 'done', data: { ok: true } })), { busy: false, run_id: null }), null);
  assert.equal(lostRunCheck(runReducer(initialRun, { type: 'start', label: 'x' }), { busy: false, run_id: null }), null, 'no run id yet');

  const lost = runReducer(s, { type: 'lost', record: null, message: 'The server restarted; this run was lost.' });
  assert.equal(lost.phase, 'failed');
  assert.equal(lost.error, 'The server restarted; this run was lost.');
  assert.equal(lost.rows.at(-1)?.kind, 'error');

  const rec = { run_id: 'R1', ok: true, placed: 1, total: 1, steps: [{ result: { object: 'red cube', in_tray: true, error_mm: 4.2, ok: true } }] };
  const found = runReducer(s, { type: 'lost', record: rec, message: 'unused' });
  assert.equal(found.phase, 'done');
  assert.equal(found.results.length, 1);
  assert.equal(found.results[0].object, 'red cube');
  assert.equal(found.rows.at(-1)?.kind, 'done');
  const failedRec = runReducer(s, { type: 'lost', record: { run_id: 'R1', ok: false, error: 'SetupRequired: x', steps: [] }, message: 'unused' });
  assert.equal(failedRec.phase, 'failed');
  assert.equal(failedRec.error, 'SetupRequired: x');
  // a late 'lost' after the stream delivered done changes nothing
  const done = runReducer(s, ev('R1', { type: 'done', data: { ok: true } }));
  assert.equal(runReducer(done, { type: 'lost', record: null, message: 'x' }), done);
});
