// Real server + Chromium audit. Every uploaded image originates at GET /api/twin/sample.png.
// Deliberate image transformations exercise invalid/empty input; no response or inference is mocked.
import { test as base, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { appendFileSync, closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const OUT = `${ROOT}audit/photo`, BASE = 'http://127.0.0.1:8420';
let scratch: string, server: ChildProcess | null = null;
const pause = (ms: number) => new Promise(r => setTimeout(r, ms));
function track(pid?: number) { if (pid && process.env.HASAMI_PHOTO_PIDS) { const started = execFileSync('ps', ['-p', String(pid), '-o', 'lstart='], { encoding: 'utf8' }).trim(); appendFileSync(process.env.HASAMI_PHOTO_PIDS, `${pid}|${started}\n`); } }
async function stop() {
  if (!server || server.exitCode !== null) return;
  const child = server; const exited = new Promise<void>(r => child.once('exit', () => r()));
  child.kill('SIGTERM'); await exited; server = null;
}
async function start() {
  const env = { ...process.env, HASAMI_HOST: '127.0.0.1', HASAMI_PORT: '8420', HASAMI_RUNS_DIR: `${scratch}/runs`, HASAMI_TWINS_DIR: `${scratch}/twins` };
  for (const key of ['MODEL_API_KEY', 'HASAMI_REALCAM', 'HASAMI_SO101_PORT', 'HASAMI_DISTURB']) delete env[key];
  if (existsSync('/Volumes/Extreme SSD/.cache/huggingface')) env.HF_HOME = '/Volumes/Extreme SSD/.cache/huggingface';
  const fd = openSync(`${scratch}/server.log`, 'a');
  server = spawn(`${ROOT}.venv/bin/python`, ['-m', 'hasami_server'], { cwd: ROOT, env, stdio: ['ignore', fd, fd] });
  closeSync(fd); track(server.pid);
  for (let i = 0; i < 180; i++) {
    try { if ((await fetch(`${BASE}/api/status`)).ok) return; } catch {}
    if (server.exitCode !== null) throw Error(`Server exited: ${readFileSync(`${scratch}/server.log`, 'utf8')}`);
    await pause(500);
  }
  throw Error('Server startup timeout');
}
base.beforeAll(async () => {
  try { await fetch(`${BASE}/api/status`); throw Error('Port 8420 is occupied; refusing to use another task’s server'); } catch (e) { if (!(e instanceof TypeError)) throw e; }
  mkdirSync(OUT, { recursive: true }); scratch = mkdtempSync(`${tmpdir()}/hasami-audit-photo-`);
  mkdirSync(`${scratch}/runs`); mkdirSync(`${scratch}/twins`);
  writeFileSync(`${OUT}/environment.json`, JSON.stringify({ base: BASE, scratch, variablesUnset: ['MODEL_API_KEY', 'HASAMI_REALCAM', 'HASAMI_SO101_PORT', 'HASAMI_DISTURB'], source: '/api/twin/sample.png', webcam: 'UNTESTED: no configured webcam; no device opened', realDeskPhoto: 'UNTESTED: real desk photo absent' }, null, 2));
  await start();
});
base.afterAll(async () => { await stop(); if (scratch) rmSync(scratch, { recursive: true, force: true }); });
type Audit = { allow: (status: number, path: RegExp) => void; outage: () => void; checkpoint: (id: string, stage?: string) => Promise<void>; note: (v: unknown) => void; watch: (p: Page) => void };
const test = base.extend<{ audit: Audit }>({
  audit: async ({ page }, use, info) => {
    const width = info.title.includes('375px') ? 375 : 1280;
    await page.setViewportSize({ width, height: width === 375 ? 812 : 800 });
    const ids = [...new Set(info.title.match(/[RTK]\d{3}/g) ?? [])];
    const scenario = info.title.split(' | ')[1].replace(/[^a-z0-9]+/gi, '-').toLowerCase();
    const allowed: { status: number; path: RegExp }[] = [], errors: string[] = [], network: unknown[] = [], consoleLog: unknown[] = [], notes: unknown[] = [];
    const screenshots: Record<string, string[]> = {}; let inOutage = false;
    const expected = (status: number, url: string) => allowed.some(a => a.status === status && a.path.test(new URL(url).pathname + new URL(url).search));
    const watch = (p: Page) => {
      p.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
      p.on('console', m => {
        const url = m.location().url, st = /status of (\d{3})/.exec(m.text());
        const declared = !!(st && url && expected(+st[1], url)) || inOutage && /net::ERR_(CONNECTION_REFUSED|CONNECTION_RESET|EMPTY_RESPONSE|INCOMPLETE_CHUNKED_ENCODING)/.test(m.text());
        consoleLog.push({ type: m.type(), text: m.text(), url, declared });
        if (m.type() === 'error' && !declared) errors.push(`console: ${m.text()} ${url}`);
      });
      p.on('response', r => {
        const item: Record<string, unknown> = { method: r.request().method(), url: r.url(), status: r.status(), declared: r.status() >= 400 && expected(r.status(), r.url()) }; network.push(item);
        if (r.status() >= 400) void r.json().then(body => { item.body = body; }).catch(() => {});
        if (r.status() >= 400 && !expected(r.status(), r.url())) errors.push(`HTTP ${r.status()} ${r.url()}`);
      });
      p.on('requestfailed', r => {
        const why = r.failure()?.errorText ?? '';
        const declared = /ERR_ABORTED/.test(why) && /\/api\/(events|frame\.png|realcam\/frame\.png)/.test(r.url()) || inOutage && /ERR_(CONNECTION_REFUSED|CONNECTION_RESET|EMPTY_RESPONSE|INCOMPLETE_CHUNKED_ENCODING|ABORTED)/.test(why);
        network.push({ method: r.method(), url: r.url(), failure: why, declared });
        if (!declared) errors.push(`requestfailed ${r.url()}: ${why}`);
      });
    };
    watch(page);
    const ps = execFileSync('ps', ['-axo', 'pid=,ppid=,comm='], { encoding: 'utf8' }).trim().split('\n').map(s => s.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/)).filter(Boolean);
    const descendants = new Set([process.pid]);
    for (let i = 0; i < 8; i++) for (const p of ps) if (descendants.has(+p![2])) descendants.add(+p![1]);
    for (const p of ps) if (descendants.has(+p![1]) && /Chrome|chrome/.test(p![3])) track(+p![1]);
    const checkpoint = async (id: string, stage = '') => {
      const focus: Record<string, string> = { R002: '.realcam .notice-setup', R003: '.realcam .rc-step', R004: '.rc-sheet', R005: '.rc-sheet', R006: '.rc-out', R007: '.rc-out', R008: '.realcam .notice-error', R009: '.rc-out', R011: '.realcam .rc-step', R012: '.realcam .notice-error', R015: '.realcam .notice-error', R016: '.realcam .notice-error', R017: '.rc-out', T002: '.twin .notice-error', T003: '.tw-sbs', T004: '.tw-sbs', T005: '.twin .notice-error', T006: '.tw-table', T007: '.twin .notice-error', T008: '.tw-sbs', T010: '.twin [role=status]', T011: '.twin .rc-step', T013: '.twin .notice-error' };
      const specific = focus[id] ? page.locator(focus[id]) : null;
      const region = specific && await specific.count() ? specific : page.locator((id.startsWith('R') || id === 'K007') ? 'section.realcam' : 'section.twin');
      await region.scrollIntoViewIfNeeded({ timeout: 10_000 });
      const name = `${id}-${width}-${scenario}${stage ? `-${stage}` : ''}.jpg`;
      await page.screenshot({ path: `${OUT}/${name}`, type: 'jpeg', quality: 28 });
      expect(statSync(`${OUT}/${name}`).size).toBeLessThanOrEqual(200_000);
      (screenshots[id] ??= []).push(name);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    };
    await use({ allow: (status, path) => allowed.push({ status, path }), outage: () => { inOutage = true; }, checkpoint, note: v => notes.push(v), watch });
    const status = info.status === 'passed' && errors.length === 0 ? 'PASS' : 'FAIL';
    const log = `${width}-${scenario}.json`;
    writeFileSync(`${OUT}/${log}`, JSON.stringify({ ids, width, status, test: info.title, assertions: info.errors.map(e => e.message), errors, console: consoleLog, network, notes, screenshots }, null, 2));
    for (const id of ids) {
      if (!screenshots[id] && info.status === 'passed') await checkpoint(id).catch(() => {});
      writeFileSync(`${OUT}/${id}-${width}-${scenario}.json`, JSON.stringify({ id, width, status, log, screenshots: screenshots[id] }, null, 2));
    }
    expect(errors, 'No unexpected console/page/network errors').toEqual([]);
  },
});
async function open(page: Page, tab: 'Real camera' | 'Twin') { await page.goto(`${BASE}/`); await page.getByRole('tab', { name: tab, exact: true }).click(); }
async function post(page: Page, path: string, body?: unknown) {
  return page.evaluate(async ({ path, body }) => { const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Hasami': '1' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); return { status: r.status, body: await r.json() }; }, { path, body });
}
async function get(page: Page, path: string) { return page.evaluate(async path => { const r = await fetch(path); return { status: r.status, body: await r.json() }; }, path); }
async function sample(page: Page, seed = 3, transform?: 'tiny' | 'no-sheet' | 'empty') {
  return page.evaluate(async ({ seed, transform }) => {
    const r = await fetch(`/api/twin/sample.png?seed=${seed}${transform === 'empty' ? '&empty=true' : ''}`), blob = await r.blob();
    let bytes = new Uint8Array(await blob.arrayBuffer());
    if (transform && transform !== 'empty') {
      const img = await createImageBitmap(blob), c = document.createElement('canvas');
      c.width = transform === 'tiny' ? 3 : 100;
      c.height = transform === 'tiny' ? 3 : 100;
      const ctx = c.getContext('2d')!;
      ctx.drawImage(img, 0, 0, c.width, c.height, 0, 0, c.width, c.height);
      bytes = new Uint8Array(await (await new Promise<Blob>(resolve => c.toBlob(b => resolve(b!), 'image/png'))).arrayBuffer());
    }
    return { data: Array.from(bytes), hfov: Number(r.headers.get('x-hasami-hfov')), provenance: r.headers.get('x-hasami-photo'), transform };
  }, { seed, transform });
}
async function upload(page: Page, section: 'realcam' | 'twin', photo: { data: number[] }, expectStatus = 200) {
  const path = section === 'realcam' ? /\/api\/realcam\/upload$/ : /\/api\/twin\?/;
  const response = page.waitForResponse(r => path.test(r.url()), { timeout: 180_000 });
  await page.locator(`.${section} input[type=file]`).setInputFiles({ name: `synthetic-sample-${Date.now()}.png`, mimeType: 'image/png', buffer: Buffer.from(photo.data) });
  const r = await response; const body = await r.json(); expect(r.status(), JSON.stringify(body)).toBe(expectStatus); return body;
}
async function readPhoto(page: Page) {
  const img = page.locator('.rc-view img:not(.rc-mask)');
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
  return img.evaluate((el: HTMLImageElement) => {
    const w = el.naturalWidth, h = el.naturalHeight, c = document.createElement('canvas'); c.width = w; c.height = h;
    const ctx = c.getContext('2d')!; ctx.drawImage(el, 0, 0); const d = ctx.getImageData(0, 0, w, h).data;
    let tl = [0, 0], tr = [0, 0], br = [0, 0], bl = [0, 0], a = Infinity, b = -Infinity, cc = -Infinity, e = Infinity, su = 0, sv = 0, n = 0;
    for (let v = 0; v < h; v++) for (let u = 0; u < w; u++) {
      const i = (v * w + u) * 4, R = d[i], G = d[i + 1], B = d[i + 2];
      if (Math.min(R, G, B) > 215 && Math.max(R, G, B) - Math.min(R, G, B) < 25) {
        if (u + v < a) { a = u + v; tl = [u, v]; } if (u - v > b) { b = u - v; tr = [u, v]; }
        if (u + v > cc) { cc = u + v; br = [u, v]; } if (u - v < e) { e = u - v; bl = [u, v]; }
      }
      if (R > 170 && G < 70 && B < 70) { su += u; sv += v; n++; }
    }
    return { w, h, corners: [tl, tr, br, bl], object: n > 20 ? [su / n, sv / n] : null };
  });
}
async function clickPixel(page: Page, point: number[]) {
  const img = page.locator('.rc-view img:not(.rc-mask)'); await img.scrollIntoViewIfNeeded();
  const box = (await img.boundingBox())!, dims = await img.evaluate((el: HTMLImageElement) => [el.naturalWidth, el.naturalHeight]);
  await page.mouse.click(box.x + (point[0] + 0.5) * box.width / dims[0], box.y + (point[1] + 0.5) * box.height / dims[1]);
}
async function calibrate(page: Page, corners: number[][], expected = 200) {
  const response = page.waitForResponse(r => /\/api\/realcam\/calibrate$/.test(r.url()));
  for (let i = 0; i < 4; i++) { await clickPixel(page, corners[i]); if (i < 3) await expect(page.locator('.rc-dot:not(.rc-click)')).toHaveCount(i + 1); }
  const r = await response; expect(r.status()).toBe(expected);
  if (expected === 200) await expect(page.locator('.realcam .rc-step')).toHaveText('Type the object height, then click the object.');
  return r.json();
}
async function locate(page: Page, point: number[], status = 200) {
  const response = page.waitForResponse(r => /\/api\/realcam\/segment$/.test(r.url()), { timeout: 90_000 });
  await clickPixel(page, point); const r = await response; expect(r.status()).toBe(status);
  return { body: await r.json(), sent: r.request().postDataJSON() };
}
async function readyCamera(page: Page) {
  await page.goto(`${BASE}/`); expect((await post(page, '/api/realcam/clear')).status).toBe(200);
  await page.getByRole('tab', { name: 'Real camera', exact: true }).click();
  const photo = await sample(page); const frame = await upload(page, 'realcam', photo);
  const found = await readPhoto(page); expect(found.object).not.toBeNull();
  await calibrate(page, found.corners); return { photo, frame, found };
}
async function decoded(page: Page) { await expect.poll(() => page.locator('.tw-sbs img').evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0); }
for (const width of [1280, 375]) {
  const mobile = width === 375;
  const tags = (desktop: string, narrow: string) => mobile ? `${desktop} ${narrow}` : desktop;
  test(`${tags('R001 R002 K007 R003 R004 R005 R006 R007 R008 R010 R011 R015', 'R018 R019')} | camera-main ${width}px`, async ({ page, audit }) => {
    test.setTimeout(300_000);
    audit.allow(503, /^\/api\/realcam\/grab$/); audit.allow(400, /^\/api\/realcam\/upload$/);
    await page.goto(`${BASE}/`); await post(page, '/api/realcam/clear'); await page.getByRole('tab', { name: 'Real camera', exact: true }).click();
    await expect(page.locator('.realcam')).toContainText('No real frame yet');
    for (const name of ['Grab webcam frame', 'Upload photo']) await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
    expect((await get(page, '/api/realcam')).body.frame_id).toBeNull();
    await expect(page.locator('.rc-arm')).toHaveText('arm: simulation · not connected to this camera'); await audit.checkpoint('R001');
    const grabReply = page.waitForResponse(r => r.url().endsWith('/api/realcam/grab'));
    await page.getByRole('button', { name: 'Grab webcam frame' }).click(); const grab = await grabReply;
    expect(grab.status()).toBe(503); const setup = await grab.json(); expect(setup.setup_required).toBe('HASAMI_REALCAM');
    const notice = page.locator('.realcam [role=status].notice-setup'); await expect(notice).toContainText('Setup required: HASAMI_REALCAM');
    expect((await notice.innerText()).match(/Setup required:/g)).toHaveLength(1); await expect(page.locator('.realcam .notice-error')).toHaveCount(0);
    audit.note({ grab: setup, cameraOpened: false, sourceReview: 'grab_frame rejects absent HASAMI_REALCAM before importing/opening cv2' }); await audit.checkpoint('R002'); expect((await notice.innerText()).match(/HASAMI_REALCAM/g)).toHaveLength(1); await audit.checkpoint('K007');
    const photo = await sample(page); expect(photo.provenance).toBe('synthetic (MuJoCo render)');
    const frame = await upload(page, 'realcam', photo); expect(frame.source).toBe('upload'); expect(frame.frame_id).toBeTruthy(); expect(frame.calibration).toBeNull();
    await expect(page.locator('.realcam .rc-step')).toHaveText("Click the sheet's top-left corner (1 of 4, as you look at it).");
    await expect(page.locator('.rc-view img:not(.rc-mask)')).toHaveAttribute('alt', 'Uploaded photo');
    const found = await readPhoto(page); expect([frame.width, frame.height]).toEqual([found.w, found.h]); await expect(page.locator('.rc-source')).toHaveText('Uploaded photo · single frame'); audit.note({ uploadedFrame: frame, fixture: 'seed=3 MuJoCo render; never a webcam frame' }); await audit.checkpoint('R003');
    await upload(page, 'realcam', { data: Array.from(Buffer.from('not a photo')) }, 400);
    await expect(page.locator('.realcam .notice-error')).toHaveText('not an image'); expect((await get(page, '/api/realcam')).body.frame_id).toBe(frame.frame_id); await audit.checkpoint('R015');
    const cal = await calibrate(page, found.corners); expect(cal.size_mm).toEqual([297, 210]);
    await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 landscape (297 × 210 mm across × down), detected from the clicked corners.');
    await expect(page.locator('.rc-dot:not(.rc-click)')).toHaveCount(4);
    const aligned = await page.locator('.rc-view').evaluate(el => { const image = el.querySelector('img')!.getBoundingClientRect(); return Array.from(el.querySelectorAll('.rc-dot')).map(dot => { const r = dot.getBoundingClientRect(); return { x: r.x + r.width / 2 - image.x, y: r.y + r.height / 2 - image.y, w: image.width, h: image.height }; }); });
    aligned.forEach((d, i) => { expect(Math.abs(d.x - cal.corners[i][0] / found.w * d.w)).toBeLessThan(1); expect(Math.abs(d.y - cal.corners[i][1] / found.h * d.h)).toBeLessThan(1); }); audit.note({ calibration: cal }); await audit.checkpoint('R004');
    await page.getByLabel('Object height in millimetres').fill('25'); const a = await locate(page, found.object!); expect(a.body.target.height_mm).toBe(25);
    expect(a.body.arm).toBe('sim'); expect(a.body.mask_png).toBeTruthy();
    for (const field of ['x', 'y', 'yaw', 'size', 'height', 'parallax']) await expect(page.locator('.rc-out')).toContainText(field);
    await expect(page.locator('.rc-out')).toContainText('25 mm (typed)'); await expect(page.locator('.rc-out')).toContainText('corrected (assumed 70° FOV)'); await expect(page.locator('.rc-mask')).toBeVisible();
    audit.note({ target: a.body.target, segmenter: a.body.segmenter }); await audit.checkpoint('R007');
    const before = await page.locator('.rc-out').innerText(); let segments = 0;
    const count = (r: any) => { if (r.url().endsWith('/api/realcam/segment')) segments++; }; page.on('request', count);
    for (const value of ['nope', '-1', '301']) {
      await page.getByLabel('Object height in millimetres').fill(value); await clickPixel(page, found.object!);
      await expect(page.locator('.realcam .notice-error')).toHaveText('Type the object height in mm (0-300) first.');
      expect(await page.locator('.rc-out').innerText()).toBe(before);
    }
    expect(segments).toBe(0); page.off('request', count); await audit.checkpoint('R008');
    await page.getByLabel('Object height in millimetres').fill('25');
    const sheet = page.getByLabel('A4 sheet orientation, as you look at it'); await sheet.selectOption('a4');
    await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 portrait (210 × 297 mm across × down), chosen.');
    await expect(page.locator('.rc-out')).toHaveCount(0); const b = await locate(page, found.object!);
    expect(Math.hypot(a.body.target.x_mm - b.body.target.x_mm, a.body.target.y_mm - b.body.target.y_mm)).toBeGreaterThan(10);
    await sheet.selectOption('a4-landscape'); await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 landscape (297 × 210 mm across × down), chosen.'); await audit.checkpoint('R005');
    const edge = await locate(page, [found.w - 0.55, found.h / 2]); expect(edge.sent.point.every(Number.isInteger)).toBe(true); expect(edge.sent.point[0]).toBeLessThanOrEqual(found.w - 1); expect(edge.sent.point[1]).toBeLessThanOrEqual(found.h - 1);
    await expect(page.locator('.realcam .notice-error')).toHaveCount(0); let outsideRequests = 0; const outside = (r: any) => { if (r.url().endsWith('/api/realcam/segment')) outsideRequests++; }; page.on('request', outside);
    const box = (await page.locator('.rc-view').boundingBox())!; await page.mouse.click(box.x - 2, box.y + box.height / 2); await pause(100); expect(outsideRequests).toBe(0); page.off('request', outside); audit.note({ lastPixel: edge.sent }); await audit.checkpoint('R010');
    await page.getByRole('button', { name: 'Re-click sheet' }).click();
    await expect(page.locator('.rc-dot')).toHaveCount(0); await expect(page.locator('.rc-sheet')).toHaveCount(0); await expect(page.locator('.rc-out')).toHaveCount(0);
    await expect(page.locator('.realcam .rc-step')).toHaveText("Click the sheet's top-left corner (1 of 4, as you look at it).");
    await sheet.selectOption('auto'); await calibrate(page, found.corners); await audit.checkpoint('R011');
    await page.getByLabel('Object height in millimetres').fill('0'); const tl = await locate(page, found.object!);
    await page.getByRole('button', { name: 'Re-click sheet' }).click();
    const [p0, p1, p2, p3] = found.corners; const alternate = await calibrate(page, [p1, p2, p3, p0]); expect(alternate.size_mm).toEqual([210, 297]);
    const tr = await locate(page, found.object!); expect(Math.abs(tr.body.target.x_mm - tl.body.target.y_mm)).toBeLessThan(2); expect(Math.abs(tr.body.target.y_mm - (297 - tl.body.target.x_mm))).toBeLessThan(2);
    await expect(page.locator('.rc-out')).toContainText('not corrected'); audit.note({ topLeft: tl.body.target, topRight: tr.body.target }); await audit.checkpoint('R006');
    if (mobile) { await audit.checkpoint('R018'); await audit.checkpoint('R019'); }
  });

  test(`${tags('R009', 'R019')} | camera-keyboard ${width}px`, async ({ page, audit }) => {
    test.setTimeout(180_000); await page.goto(`${BASE}/`); await post(page, '/api/realcam/clear'); await page.getByRole('tab', { name: 'Real camera', exact: true }).click();
    await upload(page, 'realcam', await sample(page)); const found = await readPhoto(page);
    await page.getByLabel('A4 sheet orientation, as you look at it').focus(); await page.keyboard.press('Tab');
    const button = page.getByRole('button', { name: /^Photo\./ }); await expect(button).toBeFocused();
    let at = [Math.round(found.w / 2), Math.round(found.h / 2)];
    const move = async (target: number[]) => {
      for (let axis = 0; axis < 2; axis++) {
        const [neg, pos] = axis === 0 ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown']; let d = target[axis] - at[axis]; const key = d < 0 ? neg : pos;
        for (; Math.abs(d) >= 32; d -= Math.sign(d) * 32) await page.keyboard.press(`Shift+${key}`);
        for (; Math.abs(d) >= 4; d -= Math.sign(d) * 8) await page.keyboard.press(key); at[axis] = target[axis] - d;
      }
    };
    const response = page.waitForResponse(r => r.url().endsWith('/api/realcam/calibrate'));
    for (const [i, p] of found.corners.entries()) { await expect(button).toHaveAttribute('aria-label', new RegExp(`, ${i + 1} of 4`)); await move(p); await page.keyboard.press('Enter'); if (i < 3) await expect(page.locator('.rc-dot')).toHaveCount(i + 1); }
    expect((await response).status()).toBe(200); await expect(button).toHaveAttribute('aria-label', /the object to locate/);
    await move(found.object!.map(Math.round)); const seg = page.waitForResponse(r => r.url().endsWith('/api/realcam/segment'));
    await page.keyboard.press(' '); expect((await seg).status()).toBe(200); await expect(page.locator('.rc-out')).toContainText(/x\s*-?\d+\.\d mm/); await expect(page.locator('.rc-out')).toContainText(/y\s*-?\d+\.\d mm/); await expect(page.locator('.rc-mask')).toBeVisible();
    for (let i = 0; i < 35; i++) await page.keyboard.press('Shift+ArrowRight');
    const bounds = await page.locator('.xhair').getAttribute('style'); expect(parseFloat(bounds!.split('left: ')[1])).toBeCloseTo((found.w - 1) / found.w * 100, 3); audit.note({ keyboardBounds: bounds }); await audit.checkpoint('R009'); if (mobile) await audit.checkpoint('R019');
  });

  test(`${tags('R012 R013 R014', 'R020')} | camera-replacement ${width}px`, async ({ page, browser, audit }) => {
    test.setTimeout(180_000); audit.allow(409, /^\/api\/realcam\/segment$/); audit.allow(404, /^\/api\/realcam\/frame.png$/);
    const a = await readyCamera(page); const second = await browser.newContext({ viewport: { width, height: mobile ? 812 : 800 } });
    try {
      const bPage = await second.newPage(); audit.watch(bPage); await open(bPage, 'Real camera'); await expect(bPage.locator('.rc-dot')).toHaveCount(4);
      const b = await upload(bPage, 'realcam', await sample(bPage, 5)); expect(b.frame_id).not.toBe(a.frame.frame_id); const f = await readPhoto(bPage); await calibrate(bPage, f.corners);
      expect(await bPage.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width); audit.note({ secondContextViewport: width, noHorizontalScroll: true });
      const stale = await locate(page, a.found.object!, 409); expect(stale.body.replaced).toBe(true); expect(stale.body.error).toContain('the frame was replaced in another tab');
      await expect(page.locator('.realcam .notice-error')).toContainText('The frame was replaced in another tab'); await expect(page.locator('.rc-view img:not(.rc-mask)')).toHaveAttribute('src', `/api/realcam/frame.png?frame_id=${b.frame_id}`);
      await expect(page.locator('.rc-dot')).toHaveCount(4); await expect(page.locator('.rc-out')).toHaveCount(0); await audit.checkpoint('R012');
      await page.reload(); await page.getByRole('tab', { name: 'Real camera', exact: true }).click(); await expect(page.locator('.rc-dot')).toHaveCount(4); await expect(page.locator('.rc-sheet')).toBeVisible();
      const restored = await readPhoto(page); const target = await locate(page, restored.object!); expect(target.body.frame_id).toBe(b.frame_id); expect(target.body.target.x_mm).toEqual(expect.any(Number)); await audit.checkpoint('R013');
      const cleared = page.waitForResponse(r => r.url().endsWith('/api/realcam/clear')); await page.getByRole('button', { name: 'Forget photo' }).click(); expect((await (await cleared).json()).cleared).toBe(true);
      await expect(page.locator('.realcam')).toContainText('No real frame yet'); await expect(page.locator('.rc-dot,.rc-mask,.rc-out')).toHaveCount(0); expect((await get(page, '/api/realcam')).body.frame_id).toBeNull();
      const missing = await get(page, '/api/realcam/frame.png'); expect(missing.status).toBe(404); expect(missing.body.error).toBe('no real-camera frame yet: grab or upload one');
      await page.reload(); await page.getByRole('tab', { name: 'Real camera', exact: true }).click(); await expect(page.locator('.realcam')).toContainText('No real frame yet'); await audit.checkpoint('R014'); if (mobile) await audit.checkpoint('R020');
    } finally { await second.close(); }
  });

  test(`${tags('R016', 'R020')} | camera-failures ${width}px`, async ({ page, audit }) => {
    test.setTimeout(180_000); audit.allow(400, /^\/api\/realcam\/calibrate$/); await readyCamera(page);
    await page.getByRole('button', { name: 'Re-click sheet' }).click(); const failure = await calibrate(page, [[100, 100], [100, 100], [100, 100], [100, 100]], 400);
    expect(failure.error).toBe('degenerate points (three or more on a line?)'); await expect(page.locator('.realcam .notice-error')).toHaveText(failure.error); await expect(page.locator('.rc-dot,.rc-sheet')).toHaveCount(0); await audit.checkpoint('R016', 'degenerate');
    audit.outage(); await stop();
    try { await page.getByRole('button', { name: 'Grab webcam frame' }).click(); await expect(page.locator('.realcam .notice-error')).toHaveText('The Hasami server is not reachable.'); await expect(page.getByRole('button', { name: 'Grab webcam frame' })).toBeEnabled(); await audit.checkpoint('R016', 'outage'); if (mobile) await audit.checkpoint('R020'); }
    finally { await start(); }
  });

  test(`${tags('R017', 'R020')} | camera-small-mask ${width}px`, async ({ page, audit }) => {
    test.setTimeout(120_000); await readyCamera(page); const tiny = await sample(page, 3, 'tiny');
    const frame = await upload(page, 'realcam', tiny); expect([frame.width, frame.height]).toEqual([3, 3]);
    const cal = await post(page, '/api/realcam/calibrate', { frame_id: frame.frame_id, corners: [[0, 0], [2, 0], [2, 2], [0, 2]], sheet: 'a4-landscape', hfov_deg: 70 }); expect(cal.status).toBe(200);
    await page.reload(); await page.getByRole('tab', { name: 'Real camera', exact: true }).click(); const seg = await locate(page, [1, 1]);
    expect(seg.body.target).toBeNull(); expect(seg.body.target_error).toBe('mask too small to locate'); await expect(page.locator('.rc-out')).toHaveText('targetmask too small to locate');
    audit.note({ fixture: '3x3 top-left crop of seed-3 server sample; real SAM inference, at most 9 mask pixels', target_error: seg.body.target_error }); await audit.checkpoint('R017'); if (mobile) await audit.checkpoint('R020');
  });
  test(`${tags('T001 T002 T003 T004 T005 T006 T007 T008 T009 T010 T011 T013', 'T015 T016 T017')} | twin-main ${width}px`, async ({ page, audit }) => {
    test.setTimeout(600_000); await stop(); await start();
    audit.allow(400, /^\/api\/twin\?/); audit.allow(409, /^\/api\/twin\/[^/]+\/adjust$/);
    await open(page, 'Twin'); const twin = page.locator('section.twin');
    await expect(twin).toContainText('Your desk, twinned'); await expect(twin).toContainText('digital twin from one photo · simulated arm');
    await expect(twin).toContainText('No photo yet.'); await expect(twin).toContainText('No twin yet'); await expect(twin.getByRole('button', { name: 'Run on the twin' })).toHaveCount(0); await audit.checkpoint('T001');
    const invalid = await upload(page, 'twin', { data: Array.from(Buffer.from('not a photo')) }, 400); expect(invalid.error).toBe('not an image'); await expect(twin.locator('.notice-error')).toHaveText('not an image'); await expect(twin).toContainText('No twin yet'); await audit.checkpoint('T002');
    const photo = await sample(page, 0); let builds = 0; const count = (r: any) => { if (/\/api\/twin\?/.test(r.url())) builds++; }; page.on('request', count);
    for (const fov of ['nonsense', '19', '151']) {
      await page.getByLabel('Camera horizontal field of view in degrees').fill(fov);
      await twin.locator('input[type=file]').setInputFiles({ name: `invalid-fov-${fov}.png`, mimeType: 'image/png', buffer: Buffer.from(photo.data) });
      await expect(twin.locator('.notice-error')).toHaveText('Field of view: 20-150 degrees (a phone main camera is about 65-70).');
    }
    expect(builds).toBe(0); page.off('request', count); await audit.checkpoint('T005');
    await page.getByLabel('Camera horizontal field of view in degrees').fill(String(photo.hfov));
    const built = await upload(page, 'twin', photo); expect(built.twin_id).toBeTruthy(); expect(built.photo.source).toBe('upload'); expect(built.objects.length).toBeGreaterThanOrEqual(3); expect(built.tasks.length).toBeGreaterThan(0);
    await decoded(page); await expect(twin.locator('.rc-step')).toContainText(`Twin ${built.twin_id} · your photo`); await expect(twin.locator('.tw-table tbody tr')).toHaveCount(built.objects.length);
    audit.note({ built, fixture: 'seed-0 MuJoCo rendered photo uploaded through ordinary photo path; no real desk was measured' }); await audit.checkpoint('T003');
    // A second client starts a real task; this idle tab tries refitting and must show the real Busy response.
    const foreign = await post(page, `/api/twin/${built.twin_id}/run`, { task: built.tasks[0].id }); expect(foreign.status).toBe(200);
    const busy = page.waitForResponse(r => /\/api\/twin\/[^/]+\/adjust$/.test(r.url())); await twin.getByRole('button', { name: 'Refit with typed heights' }).click(); expect((await busy).status()).toBe(409);
    await expect(twin.locator('.notice-error')).toContainText('Busy:'); await expect(twin.getByRole('button', { name: 'Refit with typed heights' })).toBeEnabled(); await decoded(page); await audit.checkpoint('T013', 'busy');
    await expect.poll(async () => (await get(page, '/api/status')).body.run.busy, { timeout: 180_000, intervals: [1000] }).toBe(false);
    // Listen independently to the real SSE stream so goal ordering is checked, as well as the UI result.
    await page.evaluate(() => new Promise<void>(resolve => {
      const es = new EventSource('/api/events'); (window as any).__photoEvents = []; (window as any).__photoRecordChecks = []; (window as any).__photoSource = es;
      es.addEventListener('done', async (e: any) => { const id = JSON.parse(e.data).run_id; const r = await fetch(`/api/runs/${id}`); (window as any).__photoRecordChecks.push({ run_id: id, status: r.status }); });
      for (const type of ['goal', 'done', 'error']) es.addEventListener(type, (e: any) => { if (e.data) (window as any).__photoEvents.push({ type, data: JSON.parse(e.data) }); });
      es.onopen = () => resolve();
    }));
    const started = page.waitForResponse(r => /\/api\/twin\/[^/]+\/run$/.test(r.url())); await twin.getByRole('button', { name: 'Run on the twin' }).click(); const response = await started; expect(response.status()).toBe(200); const run = await response.json();
    for (const key of ['run_id', 'task', 'instruction', 'label', 'planner']) expect(run[key]).toBeTruthy();
    await expect(page.locator('.run-state')).toHaveText('done', { timeout: 180_000 });
    await expect(twin.locator('[role=status]')).toContainText(/Goal (met|not met) on the twin's physics/);
    await expect.poll(() => page.evaluate(id => (window as any).__photoRecordChecks.find((r: any) => r.run_id === id)?.status, run.run_id)).toBe(200);
    const record = (await get(page, `/api/runs/${run.run_id}`)).body; expect(record.twin.twin_id).toBe(built.twin_id); expect(record.twin.task).toBe(run.task);
    await expect(twin.locator('[role=status]')).toContainText(record.goal.ok ? "Goal met on the twin's physics" : "Goal not met on the twin's physics");
    const events = await page.evaluate(() => { (window as any).__photoSource.close(); return (window as any).__photoEvents; });
    const goalIndex = events.findIndex((e: any) => e.type === 'goal' && e.data.run_id === run.run_id), doneIndex = events.findIndex((e: any) => e.type === 'done' && e.data.run_id === run.run_id);
    expect(goalIndex).toBeGreaterThanOrEqual(0); expect(doneIndex).toBeGreaterThan(goalIndex); expect(events[goalIndex].data.ok).toBe(record.goal.ok); expect(events[goalIndex].data.detail).toEqual(record.goal.detail);
    audit.note({ run, goal: record.goal, twin: record.twin, events }); await audit.checkpoint('T010');
    const height = twin.locator('.tw-table tbody tr').first().locator('input.tw-h'); let adjustments = 0; const adjustCount = (r: any) => { if (/\/adjust$/.test(r.url())) adjustments++; }; page.on('request', adjustCount);
    for (const value of ['nope', '4', '151']) { await height.fill(value); await twin.getByRole('button', { name: 'Refit with typed heights' }).click(); await expect(twin.locator('.notice-error')).toHaveText('Height for object 0: 5-150 mm.'); }
    expect(adjustments).toBe(0); page.off('request', adjustCount); await audit.checkpoint('T007');
    await height.fill('40'); const adjusted = page.waitForResponse(r => /\/adjust$/.test(r.url()), { timeout: 180_000 }); await twin.getByRole('button', { name: 'Refit with typed heights' }).click(); const refitReply = await adjusted; expect(refitReply.status()).toBe(200); const refit = await refitReply.json();
    expect(refit.twin_id).toBe(built.twin_id); expect(refit.objects[0].size_mm[2]).toBe(40); expect(refit.objects[0].height_source).toBe('typed'); await decoded(page);
    await expect(twin.locator('.tw-table tbody tr').first()).toContainText('typed'); await expect(twin.locator('[role=status]')).toHaveCount(0); audit.note({ adjusted: refit }); await audit.checkpoint('T006');
    const rendered: string[] = [];
    for (const side of ['far', 'near', 'left', 'right']) {
      await page.getByLabel('Where the simulated arm stands').selectOption(side);
      const reply = page.waitForResponse(r => /\/adjust$/.test(r.url()), { timeout: 180_000 }); await twin.getByRole('button', { name: 'Refit with typed heights' }).click(); const r = await reply; expect(r.status()).toBe(200); const placement = await r.json();
      expect(placement.arm.side).toBe(side); expect(placement.arm.offset_mm).toBe(215); await decoded(page);
      await expect(twin.locator('.tw-lede')).toContainText('215 mm from the sheet');
      rendered.push(await twin.locator('.tw-sbs img').evaluate((el: HTMLImageElement) => { const c = document.createElement('canvas'); c.width = 96; c.height = 36; c.getContext('2d')!.drawImage(el, 0, 0, 96, 36); return c.toDataURL(); }));
      audit.note({ placement: placement.arm, sceneCamera: placement.scene.cameras }); await audit.checkpoint('T008', side);
    }
    expect(new Set(rendered).size).toBe(4);
    const oldFrame = await page.locator('.cam-stage img').getAttribute('src');
    const loaded = page.waitForResponse(r => /\/load$/.test(r.url())); await twin.getByRole('button', { name: "Show in the arm's camera" }).click(); const loadedReply = await loaded; expect(loadedReply.status()).toBe(200); const loadBody = await loadedReply.json(); expect(loadBody.scene).toBe('twin'); expect(loadBody.twin_id).toBe(built.twin_id);
    await expect(page.getByRole('combobox', { name: 'Scene', exact: true })).toHaveValue('twin'); await expect(page.locator('.nb-head')).toContainText('twin'); await expect(page.locator('.cam-stage img')).not.toHaveAttribute('src', oldFrame!); await expect(page.locator('.run-state')).toHaveText('idle'); await expect(page.locator('.steps li')).toHaveCount(0); await expect(page.locator('.cam-stage[role=button]')).not.toHaveAttribute('aria-disabled', 'true'); await audit.checkpoint('T009');
    await page.reload(); await page.getByRole('tab', { name: 'Twin', exact: true }).click();
    for (let i = 0; i < 2; i++) {
      await expect(twin.locator('.rc-step')).toContainText(`Twin ${built.twin_id}`); await expect(twin.locator('.rc-step')).toContainText('restored from the server'); await expect(twin.locator('.tw-table tbody tr')).toHaveCount(built.objects.length); await expect(page.getByLabel('Where the simulated arm stands')).toHaveValue('right'); await expect(page.getByLabel('Camera horizontal field of view in degrees')).toHaveValue(String(built.sheet.hfov_deg)); await expect(page.getByRole('combobox', { name: 'Scene', exact: true })).toHaveValue('twin'); await decoded(page);
      if (i === 0) { await page.getByRole('tab', { name: 'Recorded runs', exact: true }).click(); await page.getByRole('tab', { name: 'Twin', exact: true }).click(); }
    }
    await audit.checkpoint('T011');
    const seeds: number[] = [];
    for (let i = 0; i < 2; i++) {
      const sampleReply = page.waitForResponse(r => /\/api\/twin\/sample.png\?seed=/.test(r.url())); const buildReply = page.waitForResponse(r => /\/api\/twin\?/.test(r.url()), { timeout: 180_000 });
      await twin.getByRole('button', { name: 'Try a synthetic photo' }).click(); const s = await sampleReply; seeds.push(Number(new URL(s.url()).searchParams.get('seed'))); const b = await buildReply; expect(b.status()).toBe(200);
      const q = new URL(b.url()).searchParams; expect(q.get('source')).toBe('synthetic'); expect(Number(q.get('hfov_deg'))).toBe(Number(s.headers()['x-hasami-hfov'])); await decoded(page);
      await expect(twin.locator('.rc-step')).toContainText('synthetic photo (MuJoCo render)'); await expect(twin.locator('figcaption')).toContainText('Synthetic photo (MuJoCo render)');
    }
    expect(seeds).toEqual([0, 1]); audit.note({ syntheticSeeds: seeds }); await audit.checkpoint('T004');
    await upload(page, 'twin', { data: Array.from(Buffer.from('not a photo')) }, 400); await expect(twin.locator('.notice-error')).toHaveText('not an image'); await decoded(page); await expect(twin.getByRole('button', { name: 'Upload a desk photo' })).toBeEnabled(); await audit.checkpoint('T013', 'failed-build');
    if (mobile) for (const id of ['T015', 'T016', 'T017']) await audit.checkpoint(id);
  });

  test(`${tags('T012', 'T015 T016')} | twin-empty-input ${width}px`, async ({ page, audit }) => {
    test.setTimeout(240_000); audit.allow(422, /^\/api\/twin\?/); await open(page, 'Twin');
    const noSheet = await sample(page, 0, 'no-sheet'); const error = await upload(page, 'twin', noSheet, 422); expect(error.error).toContain('no sheet of paper found'); await expect(page.locator('.twin .notice-error')).toHaveText(error.error); await audit.checkpoint('T012', 'no-sheet');
    const photo = await sample(page, 0, 'empty'); await page.getByLabel('Camera horizontal field of view in degrees').fill(String(photo.hfov)); const empty = await upload(page, 'twin', photo); expect(empty.objects).toHaveLength(0); expect(empty.tasks).toHaveLength(0); await decoded(page);
    await expect(page.locator('.twin')).toContainText('No objects found'); await expect(page.locator('.twin')).toContainText('No task: no object is both reachable and graspable.'); await expect(page.getByRole('button', { name: 'Run on the twin' })).toHaveCount(0);
    audit.note({ input: 'GET /api/twin/sample.png?seed=0&empty=true; genuine full-resolution MuJoCo render of the desk and A4 sheet without objects', twin: empty }); await audit.checkpoint('T012', 'no-objects'); if (mobile) for (const id of ['T015', 'T016']) await audit.checkpoint(id);
  });

  test(`${tags('T013 T014', 'T017')} | twin-outage-and-lifetime ${width}px`, async ({ page, audit }) => {
    test.setTimeout(240_000); audit.allow(404, /^\/api\/twin\/[^/]+$/); await open(page, 'Twin');
    const photo = await sample(page, 0); await page.getByLabel('Camera horizontal field of view in degrees').fill(String(photo.hfov)); await upload(page, 'twin', photo);
    const current = (await get(page, '/api/twin')).body.twin; expect(current).not.toBeNull(); await decoded(page); audit.outage(); await stop();
    try {
      await page.getByRole('button', { name: "Show in the arm's camera" }).click(); await expect(page.locator('.twin .notice-error')).toHaveText('The Hasami server is not reachable.');
      await expect(page.getByRole('button', { name: "Show in the arm's camera" })).toBeEnabled(); await expect(page.locator('.tw-sbs img')).toBeVisible(); await audit.checkpoint('T013', 'outage');
    } finally { await start(); }
    expect((await get(page, '/api/twin')).body.twin).toBeNull(); const expired = await get(page, `/api/twin/${current.twin_id}`); expect(expired.status).toBe(404); expect(expired.body.error).toContain('upload the photo again');
    await page.reload(); await page.getByRole('tab', { name: 'Twin', exact: true }).click(); await expect(page.locator('.twin')).toContainText('No twin yet'); await expect(page.locator('.twin')).toContainText('No photo yet.'); await audit.checkpoint('T014'); if (mobile) await audit.checkpoint('T017');
  });
}
