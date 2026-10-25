// The dashboard, the way a user clicks it, against the real server (MuJoCo + SAM 2.1). Every run here is
// a real simulated pick-and-place; no request is routed or stubbed.
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test, expect, expectNoHScroll, shot, API } from './fixtures';
import { clickOn, clickStep, freshScene, maskPixels, resetScene } from './scene';

const EVIDENCE_RETRY_RUN = '20261021T053203432Z'; // committed run: every first grasp pushed 6 mm off, then retried
const EVIDENCE_VIDEO_RUN = '20261021T053112874Z'; // committed run with a demo video
const VIDEO_FILE = fileURLToPath(new URL(`../../deployments/runs/${EVIDENCE_VIDEO_RUN}-demo.mp4`, import.meta.url));

test('opens on the live overhead camera with honest status and empty states', async ({ page, guard }, info) => {
  await page.goto('/');
  // the first Tab reaches the skip link, which goes to the camera
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to the camera' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#cam$/);
  await expect(page.locator('#cam')).toBeFocused();
  await expect(page.locator('#cam')).toBeInViewport();
  await expect(page.getByRole('heading', { level: 1 })).toBeInViewport();
  const pills = page.getByLabel('System status');
  await expect(pills).toContainText('Planner');
  await expect(pills).toContainText('Setup required: MODEL_API_KEY');
  await expect(pills).toContainText('SAM 2.1');
  await expect(pills).toContainText('local');
  await expect(pills).toContainText('Real arm');
  await expect(pills).toContainText('Setup required: HASAMI_SO101_PORT');

  const img = page.locator('.cam-stage img');
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(640);
  await expect(page.locator('.osd-top')).toContainText('overhead · 640×480');
  await expect(page.locator('.cam-hud')).toContainText('Click an object to pick');

  // run log, results and readout start empty
  await expect(page.getByRole('heading', { name: 'Run log' })).toBeVisible();
  await expect(page.locator('.run-state')).toHaveText('idle');
  await expect(page.locator('.tl-wrap')).toContainText('No run yet');
  await expect(page.locator('.card-results')).toContainText('No run yet');
  await expect(page.getByLabel('3D target from mask and depth')).toContainText('No target yet');

  // the register is seeded with the four committed evidence runs
  const rows = page.getByRole('table', { name: 'Recorded runs' }).getByRole('row').filter({ has: page.locator('.td-id') });
  expect(await rows.count()).toBeGreaterThanOrEqual(4);
  for (const id of ['20261020T221547959Z', '20261020T221658918Z', '20261021T053112874Z', '20261021T053203432Z']) {
    await expect(rows.filter({ hasText: id })).toHaveCount(1);
  }
  await expectNoHScroll(page);
  await shot(page, info, 'dash-01-open', true);

  // camera toggle: demo view, then back
  await page.getByRole('group', { name: 'Camera' }).getByRole('button', { name: 'demo' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('/ demo');
  await expect(page.locator('.cam-hud')).toContainText('Clicks work on the overhead camera');
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.src)).toContain('camera=demo');
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
  await shot(page, info, 'dash-02-demo-camera');
  await page.getByRole('group', { name: 'Camera' }).getByRole('button', { name: 'overhead' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('/ overhead');
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.src)).toContain('camera=overhead');

});

test('Muse instruction shows Setup required and never a canned plan', async ({ page, guard, request }, info) => {
  guard.allow(503, /^\/api\/plan$/);
  await page.goto('/');
  await freshScene(page);
  const box = page.getByLabel('What should the arm do?');
  await page.getByLabel('Objects in the scene').getByRole('button', { name: 'green cube' }).click();
  await expect(box).toHaveValue('put the green cube in the tray');

  const plan = page.waitForResponse((r) => r.url().endsWith('/api/plan'));
  await page.getByRole('button', { name: 'Plan', exact: true }).click();
  expect((await plan).status()).toBe(503);
  const setup = page.locator('.setup');
  await expect(setup).toContainText('Setup required: MODEL_API_KEY');
  await expect(setup).toContainText('No plan is shown without it');
  await expect(setup.getByRole('link', { name: 'dev.meta.ai' })).toHaveAttribute('href', 'https://dev.meta.ai/api-keys/');
  await expect(page.locator('.plan ol')).toHaveCount(0);
  await expect(page.locator('.mk-plan')).toHaveCount(0);

  // Enter in the box submits too, with the same honest answer
  await box.fill('stack the blue cube on the red cube');
  const plan2 = page.waitForResponse((r) => r.url().endsWith('/api/plan'));
  await box.press('Enter');
  expect((await plan2).status()).toBe(503);
  await expect(setup).toContainText('Setup required: MODEL_API_KEY');

  // running an instruction is not possible without the planner, and the API agrees
  await expect(page.getByRole('button', { name: 'Run instruction' })).toBeDisabled();
  await expect(page.locator('.hint')).toContainText('Setup required: MODEL_API_KEY');
  const run = await request.post('/api/run', { data: { instruction: 'put the red cube in the tray' } });
  expect(run.status()).toBe(503);
  expect((await run.json()).setup_required).toBe('MODEL_API_KEY');
  await expectNoHScroll(page);
  await shot(page, info, 'dash-03-muse-setup-required', true);
});

test('clicked steps: pick, place, remove, clear, a bare-table click, keyboard', async ({ page, guard }, info) => {
  await page.goto('/');
  await freshScene(page);
  const steps = page.locator('.steps');
  const runBtn = page.locator('.btn-run');

  await clickOn(page, 'red');
  await expect.poll(() => maskPixels(page.locator('canvas.cam-mask')), { timeout: 60_000 }).toBeGreaterThan(100);
  await expect(page.locator('.cam-hud')).toContainText('Now click where it goes');
  await expect(steps.locator('li.pending')).toContainText('click a target');
  await expect(runBtn).toBeDisabled();
  await expect(page.locator('.hint')).toContainText('Click a target for step 1 before running.');

  await clickOn(page, 'tray');
  await expect(steps.locator('li')).toHaveCount(1);
  await expect(runBtn).toHaveText(/Run 1 clicked step$/);
  await expect(runBtn).toBeEnabled();
  await page.getByRole('button', { name: 'Remove step 1' }).click();
  await expect(page.locator('.steps')).toHaveCount(0);
  await expect(page.getByText('Click an object on the overhead camera, then the spot it should go.')).toBeVisible();

  await clickStep(page, 'green', 1);
  await clickStep(page, 'blue', 2);
  await expect(runBtn).toHaveText(/Run 2 clicked steps$/);
  await shot(page, info, 'dash-04-two-clicked-steps', true);
  await page.getByRole('button', { name: 'Clear', exact: true }).click();
  await expect(page.locator('.steps')).toHaveCount(0);

  // a click on bare table finds nothing to grasp: an honest notice, no step
  await page.locator('.cam-stage').scrollIntoViewIfNeeded();
  const box = (await page.locator('.cam-stage img').boundingBox())!;
  await page.mouse.click(box.x + box.width * 0.06, box.y + box.height * 0.06);
  const notice = page.locator('.notice-error');
  await expect(notice).toContainText('No graspable object', { timeout: 60_000 });
  await shot(page, info, 'dash-05-bare-table-notice');
  await notice.getByRole('button', { name: 'Dismiss' }).click();
  await expect(notice).toHaveCount(0);
  await expect(page.locator('.steps')).toHaveCount(0);

  // keyboard: focus the camera, move the crosshair, Enter clicks
  const stage = page.locator('.cam-stage');
  await stage.focus();
  await page.keyboard.press('ArrowLeft');
  await expect(page.locator('.xhair')).toBeVisible();
  const seg = page.waitForResponse((r) => r.url().endsWith('/api/segment'));
  await page.keyboard.press('Enter');
  expect((await seg).status()).toBe(200);
  await expectNoHScroll(page);
});

test('click an object, click the tray, Run: run log, result, agent trace, register, replay, reset', async ({ page, guard, request, context }, info) => {
  await page.goto('/');
  // a fresh scene first, as a user would
  const head = page.locator('.nb-head');
  await expect(head).toContainText('seed');
  await freshScene(page);
  const before = await head.textContent();
  await resetScene(page);
  await expect(head).not.toHaveText(before!); // a new seed: the cubes moved

  await clickOn(page, 'red');
  await expect.poll(() => maskPixels(page.locator('canvas.cam-mask')), { timeout: 60_000 }).toBeGreaterThan(100);
  const readout = page.getByLabel('3D target from mask and depth');
  await expect(readout).toContainText('Click 1: object');
  await expect(readout).toContainText('sam2.1-hiera-tiny');
  await expect(readout.locator('dd').last()).toHaveText(/^0\.\d{3}$/); // IoU vs truth
  await shot(page, info, 'dash-06-mask-overlay');

  await clickOn(page, 'tray');
  await expect(page.locator('.steps li')).toHaveCount(1);
  await shot(page, info, 'dash-07-step-ready');

  const started = page.waitForResponse((r) => r.url().endsWith('/api/run') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /Run 1 clicked step/ }).click();
  const runId: string = (await (await started).json()).run_id;
  expect(runId).toMatch(/^\d{8}T\d{9}Z$/);

  // the run log streams every kind of event, then done
  const log = page.locator('.tl');
  await expect(page.locator('.run-state')).toHaveText('done', { timeout: 150_000 });
  for (const kind of ['plan', 'segment', 'target', 'phase', 'check', 'result', 'done']) {
    await expect(log.locator('.tl-kind', { hasText: new RegExp(`^${kind}$`) }).first()).toBeVisible();
  }
  for (const phase of ['above object', 'descend', 'close jaws', 'lift', 'lower', 'open jaws', 'retreat']) {
    await expect(log.getByText(phase, { exact: true })).toBeVisible();
  }
  await expect(log.locator('.tl-result')).toContainText('red cube');
  await expect(log.locator('.tl-result .ok-pill')).toHaveText(/placed/);
  await expect(log.locator('.tl-done')).toContainText('1/1 placed');

  // results: placed, localisation error in mm, IoU
  const results = page.locator('.card-results');
  await expect(results.locator('.big').nth(0)).toHaveText('1/1');
  await expect(results.locator('.big').nth(1)).toHaveText(/^\d+\.\d\s*mm$/);
  await expect(results.locator('.big').nth(2)).toHaveText(/^0\.\d\d$/);
  await shot(page, info, 'dash-08-run-done', true);

  // the agent trace of this run opens by itself, with the step images
  const trace = page.locator('section.trace');
  await expect(trace.getByRole('heading', { name: /Agent trace/ })).toContainText(runId);
  await expect(trace.locator('.tr-step-head .ok-pill')).toHaveText(/placed/);
  const shotImg = trace.locator('.tr-shot img').first();
  await shotImg.scrollIntoViewIfNeeded();
  await expect.poll(() => shotImg.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBe(640);
  await expect(trace.locator('figcaption').first()).toContainText('sam2.1-hiera-tiny');
  await shot(page, info, 'dash-09-trace-perception');
  // perception vs truth
  await trace.getByRole('button', { name: 'truth' }).click();
  await expect(trace.getByRole('button', { name: 'truth' })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => shotImg.evaluate((el: HTMLImageElement) => el.src)).toContain('kind=view_truth');
  await expect.poll(() => shotImg.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBe(640);
  await expect(trace.locator('figcaption').first()).toContainText('simulator truth');
  await shot(page, info, 'dash-10-trace-truth');
  await trace.getByRole('button', { name: 'perception' }).click();
  await expect.poll(() => shotImg.evaluate((el: HTMLImageElement) => el.src)).toMatch(/kind=view$/);
  // the frame after the attempt opens in a new tab
  const [after] = await Promise.all([context.waitForEvent('page'), trace.getByRole('link', { name: 'Frame after this attempt' }).click()]);
  await after.waitForLoadState();
  expect(after.url()).toContain(`/api/runs/${runId}/steps/0.png`);
  expect(after.url()).toContain('kind=after');
  await expect.poll(() => after.locator('img').evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(640);
  await after.close();

  // the register lists the run; Close hides the trace, Replay brings it back
  await trace.getByRole('button', { name: 'Close' }).click();
  await expect(page.locator('section.trace')).toHaveCount(0);
  const table = page.getByRole('table', { name: 'Recorded runs' });
  const row = table.getByRole('row').filter({ hasText: runId });
  await expect(row).toContainText('1/1');
  await row.getByRole('button', { name: 'Replay' }).click();
  await expect(page.locator('section.trace h2')).toContainText(runId);
  await expect(row.getByRole('button', { name: 'Replay' })).toHaveAttribute('aria-pressed', 'true');

  // replay a committed run with retries: three steps, each placed after a retry, and why
  const old = table.getByRole('row').filter({ hasText: EVIDENCE_RETRY_RUN });
  await old.getByRole('button', { name: 'Replay' }).click();
  await expect(page.locator('section.trace h2')).toContainText(EVIDENCE_RETRY_RUN);
  // the trace sits above the register, rows away from the clicked button: Replay brings it into view
  await expect(page.locator('#trace-h')).toBeInViewport();
  await expect(page.locator('#trace-h')).toBeFocused();
  await expect(page.locator('.tr-step')).toHaveCount(3);
  await expect(page.locator('.tr-step-head .ok-pill').first()).toHaveText('placed after 2 attempts');
  await expect(page.locator('.tr-why').first()).toContainText('Retry because: the check failed (');
  await expect(page.locator('.tr-inj').first()).toContainText('6 mm offset');
  const oldImg = page.locator('.tr-shot img').first();
  await oldImg.scrollIntoViewIfNeeded();
  await expect.poll(() => oldImg.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBe(640);
  await shot(page, info, 'dash-11-replay-retry-run');

  // the video column: a recorded run's video can be watched
  // (the MP4s are not in git; serve.sh links them in when this checkout has them)
  const vrow = table.getByRole('row').filter({ hasText: EVIDENCE_VIDEO_RUN });
  if (existsSync(VIDEO_FILE)) {
    const watch = vrow.getByRole('link', { name: /Watch/ });
    await expect(watch).toBeVisible();
    const href = await watch.getAttribute('href');
    expect(href).toBe(`/api/runs/${EVIDENCE_VIDEO_RUN}/video`);
    const video = await request.get(href!, { headers: { Range: 'bytes=0-1023' } });
    expect(video.status()).toBe(206);
    expect(video.headers()['content-type']).toContain('video/mp4');
    const [tab] = await Promise.all([context.waitForEvent('page'), watch.click()]);
    await tab.waitForLoadState();
    expect(tab.url()).toBe(`${API}${href}`);
    await tab.close();
  } else {
    await expect(vrow.locator('.td-v')).toHaveText('84 s mp4'); // named, not linked: the file is not here
  }
  await expect(table.getByRole('row').filter({ hasText: runId }).locator('.td-v')).toHaveText('—');

  // reset clears the run log and the results
  await page.getByRole('button', { name: 'Reset scene' }).click();
  await expect(page.locator('.run-state')).toHaveText('idle');
  await expect(page.locator('.tl-wrap')).toContainText('No run yet');
  await expect(page.locator('.card-results')).toContainText('No run yet');
  await expectNoHScroll(page);
});

test('the run log follows a run that is already going when the page loads', async ({ page, guard }, info) => {
  await page.goto('/');
  await freshScene(page);
  for (const [i, c] of (['red', 'green', 'blue'] as const).entries()) await clickStep(page, c, i + 1);
  const started = page.waitForResponse((r) => r.url().endsWith('/api/run') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /Run 3 clicked steps/ }).click();
  const runId: string = (await (await started).json()).run_id;
  await page.reload(); // or a second tab, or the dashboard opened mid-run

  await expect(page.locator('.run-state')).not.toHaveText('idle');
  await expect(page.locator('.tl')).toContainText('Plan from clicks');
  await shot(page, info, 'dash-12-reloaded-mid-run');
  await expect(page.locator('.run-state')).toHaveText('done', { timeout: 150_000 });
  await expect(page.locator('.tl-done')).toContainText('/3 placed');
  await expect(page.locator('section.trace h2')).toContainText(runId);
  await expect(page.getByRole('table', { name: 'Recorded runs' }).getByRole('row').filter({ hasText: runId })).toBeVisible();
  await shot(page, info, 'dash-13-reloaded-run-done', true);
});

test('task scenes: pick a family, reset, the instruction follows', async ({ page, guard }, info) => {
  await page.goto('/');
  await page.getByRole('combobox', { name: 'Scene' }).selectOption('stack-two');
  await page.getByRole('button', { name: 'Reset scene' }).click();
  await expect(page.locator('.nb-head')).toContainText('stack-two');
  await expect(page.getByLabel('What should the arm do?')).toHaveValue(/stack|on top|on the/i);
  await shot(page, info, 'dash-14-stack-two-scene');
  await page.getByRole('combobox', { name: 'Scene' }).selectOption('classic');
  await page.getByRole('button', { name: 'Reset scene' }).click();
  await expect(page.locator('.nb-head')).toContainText('classic');
});

test('the register tabs are an ARIA tab widget: arrow keys, Home / End, a labelled tabpanel', async ({ page, guard }, info) => {
  await page.goto('/');
  const list = page.getByRole('tablist', { name: 'Register' });
  const runs = list.getByRole('tab', { name: 'Recorded runs' });
  const twin = list.getByRole('tab', { name: 'Twin' });
  const cam = list.getByRole('tab', { name: 'Real camera' });
  await expect(runs).toHaveAttribute('aria-selected', 'true');
  // roving tabindex: only the selected tab is in the Tab order
  await expect(runs).toHaveAttribute('tabindex', '0');
  await expect(twin).toHaveAttribute('tabindex', '-1');
  const panel = page.getByRole('tabpanel');
  await expect(panel).toHaveCount(1);
  await expect(panel).toHaveAttribute('aria-labelledby', (await runs.getAttribute('id'))!);
  await expect(runs).toHaveAttribute('aria-controls', (await panel.getAttribute('id'))!);

  await runs.focus();
  await page.keyboard.press('ArrowRight');
  await expect(twin).toBeFocused();
  await expect(twin).toHaveAttribute('aria-selected', 'true');
  await expect(runs).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tabpanel', { name: 'Twin' })).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(cam).toBeFocused();
  await expect(page.getByRole('tabpanel', { name: 'Real camera' })).toBeVisible();
  await page.keyboard.press('ArrowRight'); // wraps
  await expect(runs).toBeFocused();
  await page.keyboard.press('ArrowLeft'); // wraps back
  await expect(cam).toBeFocused();
  await page.keyboard.press('Home');
  await expect(runs).toBeFocused();
  await expect(page.getByRole('tabpanel', { name: 'Recorded runs' })).toBeVisible();
  await page.keyboard.press('End');
  await expect(cam).toBeFocused();
  await expect(cam).toHaveAttribute('aria-selected', 'true');
  await shot(page, info, 'dash-15-tabs-keyboard');
  await page.keyboard.press('Home'); // leave the register on its default tab
});

test('a click on the last half-pixel of the overhead frame segments pixel 639, not 640', async ({ page, guard }, info) => {
  // The click maps to u ~ 639.9; it used to be rounded to 640 and the server answered "Segment failed: point
  // (640.0, ...) is outside the 640 x 480 frame". The dashboard now sends the pixel the click is in.
  await page.goto('/');
  await freshScene(page);
  const stage = page.locator('.cam-stage');
  await stage.scrollIntoViewIfNeeded();
  const box = (await page.locator('.cam-stage img').boundingBox())!;
  const seg = page.waitForResponse((r) => r.url().endsWith('/api/segment'));
  await page.mouse.click(box.x + box.width - 0.02, box.y + box.height - 0.02);
  const r = await seg;
  const sent = JSON.parse(r.request().postData() ?? '{}');
  // the mouse lands on whole device pixels: at 1280 px that is u, v ~ 639.97, 479.97; at 375 px (DPR 2) the
  // nearest landing spot is a pixel or two inside, so there the pixel is checked to be a whole, in-frame one
  if (info.project.name === 'desktop') expect(sent.point).toEqual([639, 479]);
  expect(sent.point.every((x: number) => Number.isInteger(x))).toBe(true);
  expect(sent.point[0]).toBeLessThanOrEqual(639);
  expect(sent.point[1]).toBeLessThanOrEqual(479);
  expect(r.status()).toBe(200);
  await expect(page.getByText(/Segment failed|outside the 640/)).toHaveCount(0);
});
