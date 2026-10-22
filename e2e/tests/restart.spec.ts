// A server restart in the middle of a run. This test starts its own Hasami server (not the suite's :8420) on
// 127.0.0.1, starts a 3-step clicked run from the dashboard, kills that server by PID (SIGKILL: no record is
// written), starts a fresh one on the same port, and checks the dashboard ends the run instead of staying on
// "running" forever, with Run enabled again. Real processes, real MuJoCo + SAM 2.1; nothing is routed or stubbed.
// Chrome logs the refused connections while the server is down, so the console guard is not used here.
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';
import { clickStep, freshScene } from './scene';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const E2E = fileURLToPath(new URL('../', import.meta.url));
const PORT = Number(process.env.HASAMI_E2E_RESTART_PORT ?? 8432);
const BASE = `http://127.0.0.1:${PORT}`;

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = createServer();
    s.once('error', () => resolve(false));
    s.listen(port, '127.0.0.1', () => s.close(() => resolve(true)));
  });
}

let server: ChildProcess | null = null;

const AUTHOR_CACHE = '/Volumes/Extreme SSD/.cache/huggingface';

/** The spawned server's environment. HF_HOME as serve.sh sets it: the caller's when set, else the author's
 * external cache only when that folder exists on this machine, else unset (the default ~/.cache/huggingface,
 * which scripts/setup.sh fills). An empty HF_HOME is dropped, never passed on as a relative path. */
export function serverEnv(base: NodeJS.ProcessEnv, authorCache = AUTHOR_CACHE): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...base, HASAMI_HOST: '127.0.0.1', HASAMI_PORT: String(PORT),
    HASAMI_RUNS_DIR: `${E2E}.runs-restart`, HASAMI_TWINS_DIR: `${E2E}.twins-restart` };
  for (const k of ['MODEL_API_KEY', 'HASAMI_REALCAM', 'HASAMI_SO101_PORT', 'HASAMI_DISTURB']) delete env[k];
  if (!env.HF_HOME) {
    delete env.HF_HOME;
    if (existsSync(authorCache)) env.HF_HOME = authorCache;
  }
  return env;
}

async function startServer(): Promise<ChildProcess> {
  const env = serverEnv(process.env);
  const child = spawn(`${ROOT}.venv/bin/python`, ['-m', 'hasami_server'], { env, stdio: 'ignore' });
  for (let i = 0; i < 240; i++) {
    try {
      if ((await fetch(`${BASE}/api/status`)).ok) return child;
    } catch {
      /* not up yet */
    }
    if (child.exitCode !== null) throw new Error(`hasami_server exited with ${child.exitCode}`);
    await new Promise((r) => setTimeout(r, 500));
  }
  child.kill('SIGKILL');
  throw new Error(`hasami_server did not answer on ${BASE}`);
}

function killServer(signal: NodeJS.Signals) {
  if (server?.pid && server.exitCode === null) process.kill(server.pid, signal); // only the process this test started
  server = null;
}

test.beforeAll(async () => {
  test.skip(!(await portFree(PORT)), `port ${PORT} is in use (someone else's); set HASAMI_E2E_RESTART_PORT`);
  for (const d of ['.runs-restart', '.twins-restart']) {
    rmSync(`${E2E}${d}`, { recursive: true, force: true });
    mkdirSync(`${E2E}${d}`, { recursive: true });
  }
});

test.afterAll(() => killServer('SIGTERM'));

test('the spawned server finds SAM 2.1 weights on a fresh clone: HF_HOME is not forced to the author\'s disk', () => {
  // It used to set HF_HOME to /Volumes/Extreme SSD/... whenever unset, so on any other machine the server could
  // not create that cache, SAM 2.1 did not load and the click segment (and this test) failed.
  const missing = `${E2E}.no-such-cache-${process.pid}`;
  expect(serverEnv({}, missing).HF_HOME).toBeUndefined();
  expect('HF_HOME' in serverEnv({ HF_HOME: '' }, missing)).toBe(false);
  expect(serverEnv({}, E2E).HF_HOME).toBe(E2E); // the author's cache, only when it exists
  expect(serverEnv({ HF_HOME: '/data/hf' }, E2E).HF_HOME).toBe('/data/hf'); // the caller's choice wins
  expect(serverEnv({ MODEL_API_KEY: 'x' }, missing).MODEL_API_KEY).toBeUndefined();
});

test('a server restart mid-run ends the run in the dashboard and enables Run again', async ({ page }, info) => {
  test.skip(info.project.name === 'mobile', 'server lifecycle, not layout: the desktop project covers it');
  test.setTimeout(300_000);
  server = await startServer();
  await page.goto(`${BASE}/`);
  await freshScene(page);
  for (const [i, c] of (['red', 'green', 'blue'] as const).entries()) await clickStep(page, c, i + 1);
  const started = page.waitForResponse((r) => r.url().endsWith('/api/run') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /Run 3 clicked steps/ }).click();
  const runId: string = (await (await started).json()).run_id;
  await expect(page.locator('.tl')).toContainText('Plan from clicks');
  await expect(page.locator('.run-state')).toHaveText('running');

  killServer('SIGKILL'); // mid-run, before step 1 has finished: the run never reaches its record
  await new Promise((r) => setTimeout(r, 1000));
  server = await startServer();

  // the status poll sees a free run slot with no run in it: the dashboard checks for the record, finds none
  await expect(page.locator('.run-state')).toHaveText('failed', { timeout: 30_000 });
  await expect(page.locator('.tl')).toContainText('The server restarted; this run was lost.');
  await expect(page.getByText('Run streaming · live frame')).toHaveCount(0);
  const file = info.outputPath('desktop-restart-01-run-lost.png');
  await page.screenshot({ path: file, fullPage: true });
  await info.attach('restart-01-run-lost', { path: file, contentType: 'image/png' });
  expect((await fetch(`${BASE}/api/runs/${runId}`)).status).toBe(404);

  // Run works again on the new server
  await freshScene(page);
  await clickStep(page, 'red', 1);
  await expect(page.getByRole('button', { name: /Run 1 clicked step/ })).toBeEnabled();
});
