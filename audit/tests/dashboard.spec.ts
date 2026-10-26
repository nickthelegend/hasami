// Real dashboard audit. Request rewrites induce real API validation failures; no
// response is fulfilled or mocked. Run records and twins live under mkdtemp().
import { test as base, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { appendFileSync, closeSync, cpSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { clickOn, clickStep, freshScene, locate, maskPixels } from './dashboard-scene';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const OUT = `${ROOT}audit/dashboard`;
const BASE = 'http://127.0.0.1:8420';
const RETRY = '20261021T053203432Z';
const VIDEO = '20261021T053112874Z';
const SEEDS = ['20261020T221547959Z', '20261020T221658918Z', VIDEO, RETRY];
let scratch: string, runs: string, server: ChildProcess | null = null;
const delay = (ms: number) => new Promise(r => setTimeout(r, ms));
async function stop() {
  if (!server || server.exitCode !== null) return;
  const child = server;
  child.kill('SIGKILL');
  await new Promise<void>(resolve => child.once('exit', () => resolve()));
  server = null;
}
async function start() {
  const env = { ...process.env, HASAMI_HOST: '127.0.0.1', HASAMI_PORT: '8420', HASAMI_RUNS_DIR: runs, HASAMI_TWINS_DIR: `${scratch}/twins` };
  for (const key of ['MODEL_API_KEY', 'HASAMI_REALCAM', 'HASAMI_SO101_PORT', 'HASAMI_DISTURB']) delete env[key];
  if (existsSync('/Volumes/Extreme SSD/.cache/huggingface')) env.HF_HOME = '/Volumes/Extreme SSD/.cache/huggingface';
  const fd = openSync(`${scratch}/server.log`, 'a');
  server = spawn(`${ROOT}.venv/bin/python`, ['-m', 'hasami_server'], { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  closeSync(fd);
  appendFileSync('/tmp/hasami-audit-dash-pids', `${server.pid}\n`);
  for (let i = 0; i < 180; i++) {
    try { if ((await fetch(`${BASE}/api/status`)).ok) return; } catch {}
    if (server.exitCode !== null) throw Error(`Server exited: ${readFileSync(`${scratch}/server.log`, 'utf8')}`);
    await delay(500);
  }
  throw Error('Server startup timeout');
}
base.beforeAll(async () => {
  // Fail rather than commandeer somebody else's server.
  try { await fetch(`${BASE}/api/status`); throw Error('Port 8420 is already occupied'); }
  catch (e) { if (!(e instanceof TypeError)) throw e; }
  mkdirSync(OUT, { recursive: true });
  scratch = mkdtempSync(`${tmpdir()}/hasami-audit-dashboard-`);
  runs = `${scratch}/runs`;
  mkdirSync(runs); mkdirSync(`${scratch}/twins`);
  for (const f of readdirSync(`${ROOT}deployments/runs`).filter(f => f.endsWith('.json'))) {
    cpSync(`${ROOT}deployments/runs/${f}`, `${runs}/${f}`);
    const d = f.slice(0, -5);
    if (existsSync(`${ROOT}deployments/runs/${d}`)) cpSync(`${ROOT}deployments/runs/${d}`, `${runs}/${d}`, { recursive: true });
  }
  writeFileSync(`${OUT}/environment.json`, JSON.stringify({ base: BASE, runs, twins: `${scratch}/twins`, modelKey: 'unset', realArmPort: 'unset', video: videoPath(), seeded: SEEDS }, null, 2));
  await start();
});
base.afterAll(async () => { await stop(); });
function videoPath() {
  const name = `${VIDEO}-demo.mp4`;
  return [`${ROOT}deployments/runs/${name}`, `/Volumes/Extreme SSD/Projects/grokbot/projects/hasami/deployments/runs/${name}`].find(existsSync) ?? null;
}

type Audit = { allow: (status: number, path: RegExp) => void; outage: () => void; checkpoint: (id: string, selector?: string, stage?: string) => Promise<void>; note: (value: unknown) => void };
const test = base.extend<{ audit: Audit }>({
  audit: async ({ page, browser }, use, info) => {
    const ids = info.title.match(/[DK]\d{3}/g) ?? [];
    const mobile = info.title.includes('375px');
    const width = mobile ? 375 : 1280;
    const scenario = info.title.replace(/[DK]\d{3}\s*/g, '').replace(/\s*\d+px$/, '').trim().replace(/[^a-z0-9]+/gi, '-').toLowerCase();
    await page.setViewportSize({ width, height: mobile ? 812 : 800 });
    const allowed: { status: number; path: RegExp }[] = [];
    const errors: string[] = [], network: unknown[] = [], consoleLog: unknown[] = [], notes: unknown[] = [];
    let inOutage = false;
    const expected = (status: number, url: string) => allowed.some(a => a.status === status && a.path.test(new URL(url).pathname + new URL(url).search));
    const watch = (p: Page) => {
      p.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
      p.on('console', m => {
        const url = m.location().url;
        const status = /status of (\d{3})/.exec(m.text());
        const declared = !!(status && url && expected(+status[1], url)) || inOutage && /net::ERR_(CONNECTION_REFUSED|CONNECTION_RESET|EMPTY_RESPONSE|INCOMPLETE_CHUNKED_ENCODING)/.test(m.text());
        consoleLog.push({ type: m.type(), text: m.text(), url, declared });
        if (m.type() === 'error' && !declared) errors.push(`console: ${m.text()} ${url}`);
      });
      p.on('response', r => {
        const item: Record<string, unknown> = { method: r.request().method(), url: r.url(), status: r.status(), declared: r.status() >= 400 && expected(r.status(), r.url()) };
        network.push(item);
        if (r.status() >= 400) void r.json().then(body => { item.body = body; }).catch(() => {});
        if (r.status() >= 400 && !expected(r.status(), r.url())) errors.push(`HTTP ${r.status()} ${r.url()}`);
      });
      p.on('requestfailed', r => {
        const why = r.failure()?.errorText ?? '';
        const declared = /ERR_ABORTED/.test(why) && /\/api\/(events|frame\.png|runs\/[^/]+\/video)/.test(r.url()) || inOutage && /ERR_(CONNECTION_REFUSED|CONNECTION_RESET|EMPTY_RESPONSE|INCOMPLETE_CHUNKED_ENCODING)/.test(why);
        network.push({ method: r.method(), url: r.url(), failure: why, declared });
        if (!declared) errors.push(`requestfailed ${r.url()}: ${why}`);
      });
    };
    watch(page); page.context().on('page', watch);
    // Record only descendants of this Playwright worker for PID-based cleanup.
    const ps = execFileSync('ps', ['-axo', 'pid=,ppid=,comm='], { encoding: 'utf8' }).trim().split('\n').map(s => s.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/)).filter(Boolean);
    const descendants = new Set([process.pid]);
    for (let i = 0; i < 8; i++) for (const p of ps) if (descendants.has(+p![2])) descendants.add(+p![1]);
    for (const p of ps) if (descendants.has(+p![1]) && /Chrome|chrome/.test(p![3])) appendFileSync('/tmp/hasami-audit-dash-pids', `${p![1]}\n`);
    const shots: Record<string, string> = {};
    const allShots: Record<string, string[]> = {};
    const checkpoint = async (id: string, selector?: string, stage = '') => {
      if (selector) await page.locator(selector).first().scrollIntoViewIfNeeded();
      const name = `${id}-${width}-${scenario}${stage ? `-${stage}` : ''}.jpg`;
      await page.screenshot({ path: `${OUT}/${name}`, type: 'jpeg', quality: 55 });
      expect(statSync(`${OUT}/${name}`).size).toBeLessThanOrEqual(200_000);
      shots[id] = name;
      (allShots[id] ??= []).push(name);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    };
    await use({ allow: (status, path) => allowed.push({ status, path }), outage: () => { inOutage = true; }, checkpoint, note: value => notes.push(value) });
    for (const id of ids) {
      if (!shots[id]) await checkpoint(id).catch(() => {});
      writeFileSync(`${OUT}/${id}-${width}-${scenario}.json`, JSON.stringify({ id, width, status: info.status === 'skipped' ? 'UNTESTED' : info.status === 'passed' && errors.length === 0 ? 'PASS' : 'FAIL', test: info.title, assertions: info.errors.map(e => e.message), errors, console: consoleLog, network, notes, screenshots: allShots[id] }, null, 2));
    }
    expect(errors, 'No unexpected console/page/network errors').toEqual([]);
  },
});

async function open(page: Page) { await page.goto(`${BASE}/`); await expect(page.locator('.cam-stage img')).toBeVisible(); await expect.poll(() => page.locator('.cam-stage img').evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(640); }
async function tabs(page: Page) {
  const list = page.getByRole('tablist', { name: 'Register' });
  const names = ['Recorded runs', 'Twin', 'Real camera'];
  const choose = async (key: string, n: number) => {
    await page.keyboard.press(key);
    for (const [i, name] of names.entries()) { const tab = list.getByRole('tab', { name }); await expect(tab).toHaveAttribute('tabindex', i === n ? '0' : '-1'); await expect(tab).toHaveAttribute('aria-selected', String(i === n)); }
    const tab = list.getByRole('tab', { name: names[n] });
    await expect(tab).toBeFocused();
    const panel = page.getByRole('tabpanel'); await expect(panel).toHaveCount(1);
    await expect(panel).toHaveAttribute('aria-labelledby', (await tab.getAttribute('id'))!);
    await expect(tab).toHaveAttribute('aria-controls', (await panel.getAttribute('id'))!);
  };
  await list.getByRole('tab', { name: names[0] }).focus();
  for (const [key, n] of [['ArrowRight', 1], ['ArrowRight', 2], ['ArrowRight', 0], ['ArrowLeft', 2], ['Home', 0], ['End', 2], ['Home', 0]] as const) await choose(key, n);
}
async function post(page: Page, path: string, body: unknown) {
  return page.evaluate(async ({ path, body }) => { const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Hasami': '1' }, body: JSON.stringify(body) }); return { status: r.status, body: await r.json() }; }, { path, body });
}
async function resetSeed(page: Page, seed = 1) { expect((await post(page, '/api/reset', { seed })).status).toBe(200); await page.reload(); await expect(page.locator('.cam-stage img')).toBeVisible(); }
async function startClicked(page: Page) {
  const reply = page.waitForResponse(r => r.url().endsWith('/api/run') && r.request().method() === 'POST');
  await page.locator('.btn-run').click(); const r = await reply; expect(r.status()).toBe(200); return (await r.json()).run_id as string;
}
async function rewrite(page: Page, pattern: string, change: (url: URL) => void, body?: unknown) {
  await page.route(pattern, async route => { const url = new URL(route.request().url()); change(url); await route.continue({ url: url.href, ...(body ? { postData: JSON.stringify(body) } : {}) }); });
}

for (const mobile of [false, true]) {
  const suffix = mobile ? '375px' : '1280px';
  test(`${mobile ? 'D040' : 'D001 D002 D003 D004 D027 D032'} initial, keyboard and camera ${suffix}`, async ({ page, audit }) => {
    await open(page);
    await page.keyboard.press('Tab'); await expect(page.getByRole('link', { name: 'Skip to the camera' })).toBeFocused();
    await page.keyboard.press('Enter'); await expect(page).toHaveURL(/#cam$/); await expect(page.locator('#cam')).toBeFocused();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Viewfinder / overhead'); await expect(page.locator('#cam')).toBeInViewport();
    const headerBottom = await page.locator('.topbar').evaluate(e => getComputedStyle(e).position === 'sticky' ? e.getBoundingClientRect().bottom : 0);
    expect((await page.locator('h1').boundingBox())!.y).toBeGreaterThanOrEqual(headerBottom);
    await expect(page.locator('.pills')).toContainText('SAM 2.1local'); await expect(page.locator('.pills')).toContainText('ArmSO-101 simulation');
    await expect(page.locator('.osd-top')).toContainText('overhead · 640×480');
    await expect(page.locator('.cam-hud')).toContainText('MuJoCo simulation · official SO-101 model');
    await expect(page.locator('.run-state')).toHaveText('idle'); await expect(page.locator('.tl-wrap')).toContainText('No run yet');
    await expect(page.locator('.card-results')).toContainText('No run yet'); await expect(page.locator('.card-results .big')).toHaveText(['—', '—', '—']);
    await expect(page.locator('.readout')).toContainText('No target yet'); await expect(page.locator('.btn-run')).toBeDisabled();
    await expect(page.getByLabel('Objects in the scene')).toBeVisible();
    if (mobile) await audit.checkpoint('D040', '.pills', 'camera');
    if (!mobile) { await audit.checkpoint('D001'); await audit.checkpoint('D002'); await audit.checkpoint('D003'); }
    await page.getByRole('group', { name: 'Camera' }).getByRole('button', { name: 'demo', exact: true }).click();
    await expect(page.locator('h1')).toContainText('/ demo'); await expect(page.locator('.cam-hud')).toContainText('Clicks work on the overhead camera');
    await expect(page.locator('.cam-stage img')).toHaveAttribute('src', /camera=demo/);
    await expect.poll(() => page.locator('.cam-stage img').evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBeGreaterThan(0);
    if (!mobile) await audit.checkpoint('D004');
    await page.getByRole('group', { name: 'Camera' }).getByRole('button', { name: 'overhead', exact: true }).click();
    await expect(page.locator('.cam-stage img')).toHaveAttribute('src', /camera=overhead/);
    await tabs(page);
    await audit.checkpoint(mobile ? 'D040' : 'D027', '[role=tablist]', 'tabs');
    await expect(page.locator('footer')).toContainText('Hasami — 鋏, scissors · Meta Global AI Developer Hackathon · MIT · Copyright (c) 2026 Nivesh Gajengi');
    await expect(page.locator('footer a')).toHaveAttribute('href', 'https://github.com/nickthelegend/hasami'); await expect(page.locator('footer a')).toHaveAttribute('rel', 'noreferrer');
    await audit.checkpoint(mobile ? 'D040' : 'D032', 'footer');
  });

  test(`${mobile ? 'D041 K002 K003 K006' : 'D005 K002 K003 K006'} instruction and missing credentials ${suffix}`, async ({ page, audit }) => {
    audit.allow(503, /^\/api\/(plan|run|segment)$/);
    await open(page); await resetSeed(page);
    await expect(page.locator('.spill').filter({ hasText: 'Planner' })).toContainText('Setup required: MODEL_API_KEY');
    await expect(page.locator('.spill').filter({ hasText: 'Real arm' })).toContainText('Setup required: HASAMI_SO101_PORT');
    const status = await (await page.request.get(`${BASE}/api/status`)).json();
    expect(status.arm).toBe('sim'); expect(status.so101.connected).toBe(false);
    expect(status.so101.setup).toBe('Setup required: HASAMI_SO101_PORT (SO-101 follower arm on USB) and `pip install lerobot[feetech]`');
    const constructor = execFileSync(`${ROOT}.venv/bin/python`, ['-c', 'from hasami_perception.segment import MetaSam3, SetupRequired\ntry:\n MetaSam3()\nexcept SetupRequired as e:\n print(str(e))\nelse:\n raise AssertionError("MetaSam3 did not require setup")'], { cwd: ROOT, env: { ...process.env, MODEL_API_KEY: '' }, encoding: 'utf8' }).trim();
    expect(constructor).toBe('Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)');
    expect(status.planner.ready).toBe(false);
    expect(status.planner.setup).toBe(constructor);
    expect(status.segmenters.find((s: { name: string }) => s.name === 'meta')).toMatchObject({ ready: false, setup: constructor });
    await expect(page.locator('.spill').filter({ hasText: 'SAM 3.1' })).toContainText('Setup required: MODEL_API_KEY');
    audit.note({ status, metaConstructor: constructor });
    const input = page.getByLabel('What should the arm do?');
    await page.getByLabel('Objects in the scene').getByRole('button', { name: 'green cube', exact: true }).click(); await expect(input).toHaveValue('put the green cube in the tray');
    await input.fill('stack the blue cube on the red cube');
    let response = page.waitForResponse(r => r.url().endsWith('/api/plan')); await input.press('Enter');
    let r = await response; expect(r.status()).toBe(503); expect((await r.json()).setup_required).toBe('MODEL_API_KEY');
    const setup = page.locator('.setup'); await expect(setup).toContainText('Setup required: MODEL_API_KEY'); await expect(setup).toContainText('No plan is shown without it');
    expect(((await setup.textContent())!.match(/MODEL_API_KEY/g) ?? []).length).toBe(1);
    await expect(setup.getByRole('link', { name: 'dev.meta.ai' })).toHaveAttribute('href', 'https://dev.meta.ai/api-keys/');
    await expect(page.locator('.mk-plan')).toHaveCount(0); await expect(page.locator('.plan ol')).toHaveCount(0); await expect(page.locator('.btn-run')).toBeDisabled();
    expect(await post(page, '/api/run', { instruction: 'put the red cube in the tray' })).toMatchObject({ status: 503, body: { setup_required: 'MODEL_API_KEY' } });
    await expect(page.locator('.mk-plan')).toHaveCount(0);
    await audit.checkpoint('K002', '.setup'); await audit.checkpoint('K006', '.pills');
    await input.press('Shift+Enter'); await expect(input).toHaveValue(/\n/);
    await input.fill('   '); await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeDisabled();
    let requests = 0; page.on('request', r => { if (r.url().endsWith('/api/plan')) requests++; }); await input.press('Enter'); await delay(300); expect(requests).toBe(0);
    if (!mobile) await audit.checkpoint('D005', '#ins-h');
    // Induce the real SAM 3.1 setup reply through the camera click UI.
    await rewrite(page, '**/api/segment', () => {}, { text: 'red cube', segmenter: 'meta' });
    response = page.waitForResponse(r => r.url().endsWith('/api/segment')); await clickOn(page, 'red'); r = await response;
    expect(r.status()).toBe(503); expect((await r.json()).setup_required).toBe('MODEL_API_KEY');
    await expect(page.locator('.notice-setup')).toHaveText(/Setup required: MODEL_API_KEY/);
    expect(((await page.locator('.notice-setup').textContent())!.match(/MODEL_API_KEY/g) ?? []).length).toBe(1);
    await expect(page.locator('.steps')).toHaveCount(0); await expect(page.locator('.mk')).toHaveCount(0);
    await audit.checkpoint('K003', '.notice-setup');
    await audit.checkpoint(mobile ? 'D041' : 'K003', '.notice-setup');
  });

  test(`${mobile ? 'D041' : 'D006 D007 D008 D009 D010 D011 D012'} click editor ${suffix}`, async ({ page, audit }) => {
    await open(page); await resetSeed(page);
    const response = page.waitForResponse(r => r.url().endsWith('/api/segment'));
    await clickOn(page, 'red'); const r = await response; expect(r.status()).toBe(200);
    const sent = r.request().postDataJSON(); expect(sent.segmenter).toBe('local'); expect(sent.point.every(Number.isInteger)).toBe(true); expect(sent.point[0]).toBeLessThan(640); expect(sent.point[1]).toBeLessThan(480);
    await expect.poll(() => maskPixels(page.locator('canvas.cam-mask')), { timeout: 60_000 }).toBeGreaterThan(100);
    await expect(page.locator('.readout')).toContainText('Click 1: object'); await expect(page.locator('.readout')).toContainText('sam2.1-hiera-tiny');
    for (const dd of await page.locator('.readout dd').allTextContents()) expect(dd).toMatch(/-?\d/);
    await expect(page.locator('.pending')).toContainText('click a target…'); await expect(page.locator('.cam-hud')).toContainText('Now click where it goes'); await expect(page.locator('.hint')).toHaveText('Click a target for step 1 before running.'); await expect(page.locator('.btn-run')).toBeDisabled();
    if (!mobile) { await audit.checkpoint('D006', '.readout'); await audit.checkpoint('D007', '.steps'); }
    await clickOn(page, 'tray'); await expect(page.locator('.steps li')).toHaveCount(1); await expect(page.locator('.steps li code')).toHaveText(/pick \d+, \d+ → place \d+, \d+/); await expect(page.locator('.mk-click')).toHaveCount(2); await expect(page.locator('.btn-run')).toHaveText(/Run 1 clicked step/); await expect(page.locator('.btn-run')).toBeEnabled();
    if (!mobile) await audit.checkpoint('D008', '.steps');
    await clickStep(page, 'green', 2); await page.getByRole('button', { name: 'Remove step 1' }).click(); await expect(page.locator('.steps .n')).toHaveText('1');
    await page.getByRole('button', { name: 'Clear', exact: true }).click(); await expect(page.locator('.steps')).toHaveCount(0); await expect(page.locator('.mk')).toHaveCount(0); expect(await maskPixels(page.locator('canvas.cam-mask'))).toBe(0); await expect(page.locator('.btn-run')).toBeDisabled();
    if (!mobile) await audit.checkpoint('D009', '#steps-h');
    await page.locator('.cam-stage').scrollIntoViewIfNeeded(); let b = (await page.locator('.cam-stage img').boundingBox())!;
    await page.mouse.click(b.x + b.width * .06, b.y + b.height * .06); await expect(page.locator('.notice-error')).toContainText('No graspable object', { timeout: 60_000 }); await expect(page.locator('.steps')).toHaveCount(0);
    if (!mobile) await audit.checkpoint('D010', '.notice-error');
    await page.getByRole('button', { name: 'Dismiss' }).click(); await expect(page.locator('.notice-error')).toHaveCount(0);
    let n = 0; const count = () => n++; page.on('request', req => { if (req.url().endsWith('/api/segment')) count(); });
    await page.locator('.cam-stage').scrollIntoViewIfNeeded(); b = (await page.locator('.cam-stage img').boundingBox())!;
    await page.mouse.click(b.x - 2, b.y + 20); await delay(200); expect(n).toBe(0);
    const edge = page.waitForResponse(r => r.url().endsWith('/api/segment')); await page.mouse.click(b.x + b.width - .02, b.y + b.height - .02); const er = await edge; expect(er.status()).toBe(200); const p = er.request().postDataJSON().point; expect(p.every(Number.isInteger)).toBe(true); expect(p[0]).toBeLessThan(640); expect(p[1]).toBeLessThan(480); if (!mobile) expect(p).toEqual([639, 479]);
    await expect(page.getByText(/Segment failed|outside the 640/)).toHaveCount(0);
    await expect(page.getByText('Segmenting with SAM 2.1…')).toHaveCount(0);
    if (!mobile) await audit.checkpoint('D011', '.cam-stage');
    if (await page.getByRole('button', { name: 'Dismiss' }).count()) await page.getByRole('button', { name: 'Dismiss' }).click();
    if (await page.getByRole('button', { name: 'Clear', exact: true }).count()) await page.getByRole('button', { name: 'Clear', exact: true }).click();
    // Move to the actual red pixel and perform both keyboard clicks.
    const red = await locate(page, 'red'), tray = await locate(page, 'tray'); const stage = page.locator('.cam-stage'); await stage.focus();
    const move = async (from: number[], to: number[]) => { for (let axis = 0; axis < 2; axis++) { let delta = to[axis] - from[axis]; const key = axis === 0 ? delta > 0 ? 'ArrowRight' : 'ArrowLeft' : delta > 0 ? 'ArrowDown' : 'ArrowUp'; while (Math.abs(delta) >= 32) { await page.keyboard.press(`Shift+${key}`); delta += delta > 0 ? -32 : 32; } while (Math.abs(delta) >= 8) { await page.keyboard.press(key); delta += delta > 0 ? -8 : 8; } } };
    await expect(stage).toHaveAttribute('role', 'button');
    await move(p, [red.u, red.v]); const keySeg = page.waitForResponse(r => r.url().endsWith('/api/segment')); await page.keyboard.press('Enter'); expect((await keySeg).status()).toBe(200); await expect(page.locator('.pending')).toBeVisible();
    await stage.focus(); for (let i = 0; i < 21; i++) await page.keyboard.press('Shift+ArrowRight');
    expect(await page.locator('.xhair').evaluate(e => parseFloat((e as HTMLElement).style.left))).toBeCloseTo(639 / 640 * 100);
    for (let i = 0; i < 21; i++) await page.keyboard.press('Shift+ArrowLeft');
    expect(await page.locator('.xhair').evaluate(e => parseFloat((e as HTMLElement).style.left))).toBe(0);
    const cursorY = await page.locator('.xhair').evaluate(e => parseFloat((e as HTMLElement).style.top) * 480 / 100);
    await move([0, cursorY], [tray.u, tray.v]); await page.keyboard.press('Space'); await expect(page.locator('.steps li:not(.pending)')).toHaveCount(1);
    await audit.checkpoint(mobile ? 'D041' : 'D012', '.steps');
  });

  test(`${mobile ? 'D042' : 'D013 D014 D015 D016 D017 D019'} real red cube run and trace ${suffix}`, async ({ page, context, audit }) => {
    test.setTimeout(240_000); await open(page); await resetSeed(page); await clickStep(page, 'red', 1);
    const id = await startClicked(page); audit.note({ runId: id, runs });
    await expect(page.locator('.run-state')).toHaveText('running'); await expect(page.locator('.cam-hud')).toContainText('Run streaming · live frame'); await expect(page.locator('.btn-run')).toHaveText(/Running…/); await expect(page.getByRole('combobox', { name: 'Scene' })).toBeDisabled(); await expect(page.getByRole('button', { name: 'Reset scene' })).toBeDisabled(); await expect(page.locator('.cam-stage')).not.toHaveAttribute('role', 'button');
    if (mobile) await audit.checkpoint('D042', '.cam-stage', 'live');
    if (!mobile) await audit.checkpoint('D015', '.cam-stage');
    await expect(page.locator('.run-state')).toHaveText('done', { timeout: 150_000 });
    await expect(page.locator('.tl-result')).toContainText('red cube'); await expect(page.locator('.tl-result')).toContainText('placed'); await expect(page.locator('.tl-done')).toContainText('1/1 placed'); await expect(page.locator('.card-results .big').first()).toHaveText('1/1');
    for (const kind of ['plan', 'segment', 'target', 'phase', 'check', 'result', 'done']) expect(await page.locator('.tl-kind').allTextContents()).toContain(kind);
    for (const phase of ['above object', 'descend', 'close jaws', 'lift', 'above target', 'lower', 'open jaws', 'retreat']) await expect(page.locator('.tl').getByText(phase, { exact: true })).toBeVisible();
    const kinds = await page.locator('.tl-kind').allTextContents(); expect(kinds.indexOf('plan')).toBe(0); expect(kinds.indexOf('segment')).toBeLessThan(kinds.indexOf('target')); expect(kinds.indexOf('phase')).toBeLessThan(kinds.indexOf('check')); expect(kinds.indexOf('check')).toBeLessThan(kinds.indexOf('result')); expect(kinds.indexOf('result')).toBeLessThan(kinds.indexOf('done'));
    await expect(page.getByText('Run streaming · live frame')).toHaveCount(0); await expect(page.getByRole('combobox', { name: 'Scene' })).toBeEnabled(); await expect(page.getByRole('button', { name: 'Reset scene' })).toBeEnabled();
    await expect.poll(() => existsSync(`${runs}/${id}.json`)).toBe(true); const record = JSON.parse(readFileSync(`${runs}/${id}.json`, 'utf8')); expect(record.placed).toBe(1); expect(record.total).toBe(1); expect(record.steps[0].attempts.length).toBeGreaterThan(0); expect(readdirSync(`${runs}/${id}`).filter(f => f.endsWith('.png')).length).toBeGreaterThan(0);
    if (!mobile) { await audit.checkpoint('D013', '.card-results'); await audit.checkpoint('D014', '.tl-done'); }
    else { await audit.checkpoint('D042', '.card-results', 'results'); await audit.checkpoint('D042', '.tl-done', 'log'); }
    audit.note({ record, attemptFiles: readdirSync(`${runs}/${id}`).filter(f => f.endsWith('.png')) });
    const trace = page.locator('section.trace'); await expect(trace.locator('h2')).toContainText(id); const image = trace.locator('.tr-shot img').first(); await image.scrollIntoViewIfNeeded(); await expect.poll(() => image.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(640); await expect(trace.locator('figcaption').first()).toContainText('sam2.1');
    await trace.getByRole('button', { name: 'truth', exact: true }).click(); await expect(image).toHaveAttribute('src', /kind=view_truth/); await expect(trace.locator('figcaption').first()).toContainText('simulator truth');
    await trace.getByRole('button', { name: 'perception', exact: true }).click(); await expect(image).toHaveAttribute('src', /kind=view$/);
    const [after] = await Promise.all([context.waitForEvent('page'), trace.getByRole('link', { name: 'Frame after this attempt' }).first().click()]); await after.waitForLoadState(); await expect.poll(() => after.locator('img').evaluate((i: HTMLImageElement) => i.naturalWidth)).toBe(640); expect(after.url()).toContain('kind=after'); await after.close();
    if (!mobile) await audit.checkpoint('D016', 'section.trace');
    else await audit.checkpoint('D042', 'section.trace', 'trace');
    await trace.getByRole('button', { name: 'Close', exact: true }).click(); await expect(trace).toHaveCount(0);
    const table = page.getByRole('table', { name: 'Recorded runs' }); for (const seed of SEEDS) await expect(table.getByRole('row').filter({ hasText: seed })).toHaveCount(1);
    const row = table.getByRole('row').filter({ hasText: id }); await expect(row).toContainText('1/1'); await row.getByRole('button', { name: 'Replay' }).click(); await expect(row.getByRole('button', { name: 'Replay' })).toHaveAttribute('aria-pressed', 'true');
    if (!mobile) await audit.checkpoint('D017', '.td-id');
    await expect(table.getByRole('row').filter({ hasText: VIDEO }).locator('.td-v')).toHaveText('84 s mp4'); await expect(table.getByRole('row').filter({ hasText: VIDEO }).getByRole('link', { name: /Watch/ })).toHaveCount(0); await expect(row.locator('.td-v')).toHaveText('—');
    await audit.checkpoint(mobile ? 'D042' : 'D019', '.td-v');
  });

  test(`${mobile ? 'D043' : 'D018'} committed retry replay ${suffix}`, async ({ page, audit }) => {
    await open(page); let started = 0; page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/api/run')) started++; });
    await page.getByRole('table').getByRole('row').filter({ hasText: RETRY }).getByRole('button', { name: 'Replay' }).click();
    await expect(page.locator('#trace-h')).toBeFocused(); await expect(page.locator('#trace-h')).toBeInViewport(); await expect(page.locator('.tr-step')).toHaveCount(3); await expect(page.locator('.tr-step-head .ok-pill')).toHaveText(['placed after 2 attempts', 'placed after 2 attempts', 'placed after 2 attempts']); await expect(page.locator('.tr-inj').first()).toContainText('6 mm offset'); await expect(page.locator('.tr-why').first()).toContainText('Retry because:');
    const images = page.locator('.tr-shot img'); await expect(images).toHaveCount(6); for (const image of await images.all()) { await image.scrollIntoViewIfNeeded(); await expect.poll(() => image.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(640); }
    expect(started).toBe(0); await audit.checkpoint(mobile ? 'D043' : 'D018', 'section.trace');
  });

  test(`${mobile ? 'D043' : 'D020'} existing recorded video ${suffix}`, async ({ page, context, audit }) => {
    const video = videoPath(); test.skip(!video, 'UNTESTED: existing local run MP4 is absent; no generated substitute.');
    cpSync(video!, `${runs}/${VIDEO}-demo.mp4`);
    try {
      await open(page); const watch = page.getByRole('table').getByRole('row').filter({ hasText: VIDEO }).getByRole('link', { name: /Watch/ }); await expect(watch).toHaveAttribute('href', `/api/runs/${VIDEO}/video`);
      const range = await page.request.get(`${BASE}/api/runs/${VIDEO}/video`, { headers: { Range: 'bytes=0-1023' } }); expect(range.status()).toBe(206); expect(range.headers()['content-type']).toContain('video/mp4');
      const [tab] = await Promise.all([context.waitForEvent('page'), watch.click()]); await tab.waitForLoadState(); expect(tab.url()).toContain(`/api/runs/${VIDEO}/video`); await tab.close(); await audit.checkpoint(mobile ? 'D043' : 'D020', '.td-v');
    } finally { rmSync(`${runs}/${VIDEO}-demo.mp4`, { force: true }); }
  });

  test(`${mobile ? 'D043' : 'D021'} empty register ${suffix}`, async ({ page, audit }) => {
    const seeded = runs; runs = `${scratch}/empty`; mkdirSync(runs, { recursive: true }); await stop(); await start();
    try { await open(page); await expect(page.getByText('No recorded runs yet')).toBeVisible(); await expect(page.getByRole('button', { name: 'Replay', exact: true })).toHaveCount(0); await expect(page.getByRole('link', { name: /Watch/ })).toHaveCount(0); await audit.checkpoint(mobile ? 'D043' : 'D021', '.runs'); }
    finally { runs = seeded; await stop(); await start(); }
  });

  test(`${mobile ? 'D043' : 'D022'} rejected register request ${suffix}`, async ({ page, audit }) => {
    audit.allow(422, /^\/api\/runs/); await rewrite(page, '**/api/runs', u => u.searchParams.set('limit', '0'));
    await open(page); await expect(page.getByText('Could not load runs')).toBeVisible(); await expect(page.getByRole('table')).toHaveCount(0); await audit.checkpoint(mobile ? 'D043' : 'D022', '.runs');
  });

  test(`${mobile ? 'D045' : 'D023'} API rejects click and reset ${suffix}`, async ({ page, audit }) => {
    audit.allow(400, /^\/api\/segment/); audit.allow(422, /^\/api\/reset/);
    await open(page); await resetSeed(page);
    await rewrite(page, '**/api/segment', () => {}, { point: [640, 480], segmenter: 'local' }); await clickOn(page, 'red'); await expect(page.locator('.notice-error')).toContainText('Segment failed:', { timeout: 60_000 });
    await expect(page.getByText('Segmenting with SAM 2.1…')).toHaveCount(0); await expect(page.locator('.steps li')).toHaveCount(0);
    await page.getByRole('button', { name: 'Dismiss' }).click(); await expect(page.locator('.notice-error')).toHaveCount(0);
    await rewrite(page, '**/api/reset', () => {}, { family: 'audit-unknown-family' }); await page.getByRole('button', { name: 'Reset scene' }).click(); await expect(page.locator('.notice-error')).toContainText('Reset failed:'); await expect(page.getByRole('button', { name: 'Reset scene' })).toBeEnabled();
    await audit.checkpoint(mobile ? 'D045' : 'D023', '.notice-error'); await page.getByRole('button', { name: 'Dismiss' }).click();
  });

  test(`${mobile ? 'D045' : 'D024'} unknown trace ${suffix}`, async ({ page, audit }) => {
    audit.allow(404, /^\/api\/runs\//); await open(page); await rewrite(page, `**/api/runs/${RETRY}`, u => { u.pathname = '/api/runs/audit-unknown-run'; });
    await page.getByRole('table').getByRole('row').filter({ hasText: RETRY }).getByRole('button', { name: 'Replay' }).click();
    await expect(page.getByText('Could not load this run')).toBeVisible({ timeout: 15_000 }); await expect(page.locator('section.trace')).toContainText("no run 'audit-unknown-run'"); await expect(page.locator('.tr-shot img')).toHaveCount(0); await expect(page.getByText('Loading the run record…')).toHaveCount(0); await audit.checkpoint(mobile ? 'D045' : 'D024', 'section.trace');
  });

  test(`${mobile ? 'D045' : 'D025'} offline plan component states ${suffix}`, async ({ page, audit }) => {
    await page.goto(`${BASE}/audit-component.html?view=error`); await expect(page.getByText('Planning failed', { exact: true })).toBeVisible(); await expect(page.getByText('Local planning error for offline component audit.', { exact: true })).toBeVisible(); await expect(page.locator('.mk')).toHaveCount(0);
    await page.goto(`${BASE}/audit-component.html?view=empty`); await expect(page.getByText('Muse returned an empty plan for this instruction.')).toBeVisible(); await expect(page.locator('.mk')).toHaveCount(0); await audit.checkpoint(mobile ? 'D045' : 'D025');
  });

  test(`${mobile ? 'D044' : 'D026 D033 D034 D035 D036 D037 D038 D039'} all task families ${suffix}`, async ({ page, audit }) => {
    await open(page); await resetSeed(page); const first = await page.locator('.cam-stage img').getAttribute('src'); const image1 = await (await page.request.get(`${BASE}${first}`)).body();
    await clickStep(page, 'red', 1); expect((await post(page, '/api/reset', { seed: 1 })).status).toBe(200); await page.reload(); await expect(page.locator('.cam-stage img')).toBeVisible();
    const image2 = await (await page.request.get(`${BASE}/api/frame.png?camera=overhead`)).body(); expect(image2.equals(image1)).toBe(true);
    await freshScene(page); await expect(page.locator('.nb-head')).toContainText('classic'); await expect(page.locator('.nb-head')).toContainText('seed'); await expect(page.locator('.steps')).toHaveCount(0); await expect(page.locator('.mk')).toHaveCount(0); await expect(page.locator('.readout')).toContainText('No target yet'); await expect(page.locator('.run-state')).toHaveText('idle'); await expect(page.locator('.card-results')).toContainText('No run yet');
    if (!mobile) await audit.checkpoint('D026', '.nb-head');
    for (const [i, family] of ['sort-by-colour', 'stack-two', 'clear-except-X', 'tower-of-3', 'sort-by-shape', 'line-up', 'pick-from-bin'].entries()) {
      await page.getByRole('combobox', { name: 'Scene' }).selectOption(family); const response = page.waitForResponse(r => r.url().endsWith('/api/reset')); await page.getByRole('button', { name: 'Reset scene' }).click(); const r = await response; expect(r.status()).toBe(200); const data = await r.json(); expect(data.scene).toBe(family); expect(data.instruction.length).toBeGreaterThan(0); await expect(page.locator('.nb-head')).toContainText(family); await expect(page.getByLabel('What should the arm do?')).toHaveValue(data.instruction); await expect(page.getByRole('combobox', { name: 'Scene' })).toHaveValue(family); await expect(page.locator('.steps')).toHaveCount(0); await expect(page.locator('.mk-plan')).toHaveCount(0); await expect.poll(() => page.locator('.cam-stage img').evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(640); if (!mobile) await audit.checkpoint(`D0${33 + i}`, '.nb-head'); else await audit.checkpoint('D044', '.nb-head', family);
    }
    if (mobile) await audit.checkpoint('D044', '.nb-head');
  });

  test(`${mobile ? 'D044' : 'D028'} reload active run ${suffix}`, async ({ page, audit }) => {
    test.setTimeout(240_000); await open(page); await resetSeed(page); await clickStep(page, 'red', 1); const id = await startClicked(page); await page.reload(); await expect(page.locator('.tl')).toContainText('Plan from clicks'); await expect(page.locator('.run-state')).toHaveText('done', { timeout: 150_000 }); await expect(page.locator('section.trace h2')).toContainText(id); await expect(page.getByRole('table').getByRole('row').filter({ hasText: id })).toBeVisible(); audit.note({ runId: id }); await audit.checkpoint(mobile ? 'D044' : 'D028', '.tl-done');
  });

  test(`${mobile ? 'D045' : 'D029 D030'} server killed mid-run and retry ${suffix}`, async ({ page, audit }) => {
    test.setTimeout(240_000); await open(page); await resetSeed(page); await clickStep(page, 'red', 1); const id = await startClicked(page); await expect(page.locator('.tl')).toContainText('Plan from clicks'); audit.outage(); audit.allow(404, new RegExp(`^/api/runs/${id}$`));
    await stop(); await expect(page.locator('.offline')).toContainText('Start the server: python -m hasami_server', { timeout: 25_000 }); await expect(page.locator('.pills')).toContainText('Servernot reachable'); await expect(page.getByText('No camera feed', { exact: true })).toBeVisible(); if (!mobile) await audit.checkpoint('D030', '.offline');
    await start(); await page.getByRole('button', { name: 'Retry', exact: true }).click(); await expect(page.locator('.run-state')).toHaveText('failed', { timeout: 30_000 }); await expect(page.locator('.tl')).toContainText('The server restarted; this run was lost.'); await expect(page.getByText('Run streaming · live frame')).toHaveCount(0); await expect(page.locator('.cam-stage img')).toBeVisible();
    expect((await page.request.get(`${BASE}/api/runs/${id}`)).status()).toBe(404); expect(existsSync(`${runs}/${id}.json`)).toBe(false); audit.note({ lostRunId: id, missingRecord: 404 }); await audit.checkpoint(mobile ? 'D045' : 'D029', '.tl-error');
    await resetSeed(page); await clickStep(page, 'red', 1); const next = await startClicked(page); expect(next).not.toBe(id); await expect(page.locator('.run-state')).toHaveText('done', { timeout: 150_000 });
  });

  test(`${mobile ? 'D045' : 'D031'} frame API rejects camera ${suffix}`, async ({ page, audit }) => {
    audit.allow(422, /^\/api\/frame.png/); await rewrite(page, '**/api/frame.png?*', u => u.searchParams.set('camera', 'audit-invalid-camera')); await page.goto(`${BASE}/`); await expect(page.getByText('Frame failed to load', { exact: true })).toBeVisible(); await expect(page.getByText('GET /api/frame.png?camera=overhead', { exact: true })).toBeVisible(); await audit.checkpoint(mobile ? 'D045' : 'D031', '.cam');
  });
}
