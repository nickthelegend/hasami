// The Twin and Real camera tabs. The photos are MuJoCo renders of a desk with an A4 sheet
// (GET /api/twin/sample.png, labelled synthetic); the webcam is never opened (HASAMI_REALCAM is unset).
import { test, expect, expectNoHScroll, shot } from './fixtures';
import type { APIRequestContext, Page } from '@playwright/test';

async function deskPhoto(request: APIRequestContext, seed: number) {
  const r = await request.get(`/api/twin/sample.png?seed=${seed}`);
  expect(r.status()).toBe(200);
  expect(r.headers()['x-hasami-photo']).toBe('synthetic (MuJoCo render)');
  return { buffer: await r.body(), hfov: Number(r.headers()['x-hasami-hfov']) };
}

/** Sheet corners (TL, TR, BR, BL) and the centre of a coloured object, read from the shown photo's pixels. */
async function readPhoto(page: Page, sel: string, colour: 'red' | 'orange') {
  const img = page.locator(sel);
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
  await img.scrollIntoViewIfNeeded();
  return img.evaluate((el: HTMLImageElement, colour: string) => {
    const w = el.naturalWidth, h = el.naturalHeight;
    const c = document.createElement('canvas');
    c.width = w;
    c.height = h;
    const ctx = c.getContext('2d')!;
    ctx.drawImage(el, 0, 0);
    const d = ctx.getImageData(0, 0, w, h).data;
    let tl = [0, 0], tr = [0, 0], br = [0, 0], bl = [0, 0];
    let a = Infinity, b = -Infinity, cc = -Infinity, e = Infinity;
    let su = 0, sv = 0, n = 0;
    for (let v = 0; v < h; v++) for (let u = 0; u < w; u++) {
      const i = (v * w + u) * 4, R = d[i], G = d[i + 1], B = d[i + 2];
      if (Math.min(R, G, B) > 215 && Math.max(R, G, B) - Math.min(R, G, B) < 25) {
        if (u + v < a) { a = u + v; tl = [u, v]; }
        if (u - v > b) { b = u - v; tr = [u, v]; }
        if (u + v > cc) { cc = u + v; br = [u, v]; }
        if (u - v < e) { e = u - v; bl = [u, v]; }
      }
      const hit = colour === 'red' ? R > 170 && G < 70 && B < 70 : R > 200 && G > 90 && G < 170 && B < 60;
      if (hit) { su += u; sv += v; n++; }
    }
    const r = el.getBoundingClientRect();
    const page = ([u, v]: number[]) => ({ x: r.left + ((u + 0.5) / w) * r.width, y: r.top + ((v + 0.5) / h) * r.height });
    const obj = n > 20 ? [su / n, sv / n] : null;
    return {
      corners: [tl, tr, br, bl].map(page),
      object: obj ? page(obj) : null,
      px: { w, h, corners: [tl, tr, br, bl], object: obj }, // image pixels, for the keyboard path
    };
  }, colour);
}

test('real camera: webcam is Setup required; an uploaded photo is calibrated on the sheet and an object located', async ({ page, guard, request }, info) => {
  guard.allow(503, /^\/api\/realcam\/grab$/);
  await page.goto('/');
  await page.getByRole('tab', { name: 'Real camera' }).click();
  await expect(page.getByRole('tab', { name: 'Real camera' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.realcam')).toContainText('No real frame yet');
  await expect(page.locator('.rc-arm')).toHaveText('arm: simulation · not connected to this camera');

  await page.getByRole('button', { name: 'Grab webcam frame' }).click();
  await expect(page.locator('.realcam .notice-error')).toContainText('Setup required: HASAMI_REALCAM');
  await shot(page, info, 'cam-01-webcam-setup-required');

  const photo = await deskPhoto(request, 3);
  const up = page.waitForResponse((r) => r.url().endsWith('/api/realcam/upload'));
  await page.locator('.realcam input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await up).status()).toBe(200);
  await expect(page.locator('.rc-step')).toContainText("Click the sheet's top-left corner (1 of 4");

  const found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red');
  for (const [i, p] of found.corners.entries()) {
    await page.mouse.click(p.x, p.y);
    if (i < 3) await expect(page.locator('.rc-dot')).toHaveCount(i + 1);
  }
  await expect(page.locator('.rc-step')).toHaveText('Type the object height, then click the object.', { timeout: 30_000 });
  await expect(page.getByRole('button', { name: 'Re-click sheet' })).toBeVisible();
  await shot(page, info, 'cam-02-sheet-calibrated');

  await page.getByLabel('Object height in millimetres').fill('25');
  expect(found.object).not.toBeNull();
  const seg = page.waitForResponse((r) => r.url().endsWith('/api/realcam/segment'));
  await page.mouse.click(found.object!.x, found.object!.y);
  expect((await seg).status()).toBe(200);
  const out = page.locator('.rc-out');
  await expect(out).toContainText(/x\s*-?\d+\.\d mm/);
  await expect(out).toContainText('25 mm (typed)');
  await expect(out).toContainText('corrected (assumed 70° FOV)');
  await expect(page.locator('.rc-mask')).toBeVisible();
  await expectNoHScroll(page);
  await shot(page, info, 'cam-03-object-located', true);

  await page.getByRole('button', { name: 'Re-click sheet' }).click();
  await expect(page.locator('.rc-step')).toContainText("Click the sheet's top-left corner");
  await expect(page.locator('.rc-out')).toHaveCount(0);
});

test('real camera: a click on the photo\'s last pixel column is sent as that pixel and segments', async ({ page, guard, request }) => {
  // On a w-wide photo a click at the right edge maps to x ~ w - 0.1. It used to be sent as is; the server rounded
  // it to column w and crashed (500 IndexError), later refused it (400). The dashboard now sends pixel w - 1.
  await page.goto('/');
  await page.getByRole('tab', { name: 'Real camera' }).click();
  const photo = await deskPhoto(request, 3);
  const up = page.waitForResponse((r) => r.url().endsWith('/api/realcam/upload'));
  await page.locator('.realcam input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await up).status()).toBe(200);
  const found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red');
  for (const [i, p] of found.corners.entries()) {
    await page.mouse.click(p.x, p.y);
    if (i < 3) await expect(page.locator('.rc-dot')).toHaveCount(i + 1);
  }
  await expect(page.locator('.rc-step')).toHaveText('Type the object height, then click the object.', { timeout: 30_000 });
  await page.getByLabel('Object height in millimetres').fill('25');
  const img = page.locator('.rc-view img:not(.rc-mask)');
  const box = (await img.boundingBox())!;
  const seg = page.waitForResponse((r) => r.url().endsWith('/api/realcam/segment'), { timeout: 60_000 });
  await page.mouse.click(box.x + box.width - 0.02, box.y + box.height / 2);
  const r = await seg;
  const sent = JSON.parse(r.request().postData() ?? '{}');
  // the pixel the click is in: whole numbers, the last column at most (the mouse lands on whole device pixels,
  // so the exact column depends on the photo's scale; any fraction sent here is what the server used to round)
  expect(sent.point.every((x: number) => Number.isInteger(x))).toBe(true);
  expect(sent.point[0]).toBeGreaterThanOrEqual(found.px.w - 4);
  expect(sent.point[0]).toBeLessThanOrEqual(found.px.w - 1);
  expect(r.status()).toBe(200);
  await expect(page.locator('.realcam .notice-error')).toHaveCount(0);
});

test('twin: a non-image is refused, a synthetic photo becomes a twin, refit, show it, run a task on it', async ({ page, guard, request }, info) => {
  guard.allow(400, /^\/api\/twin\?/);
  await page.goto('/');
  await page.getByRole('tab', { name: 'Twin' }).click();
  const twin = page.locator('section.twin');
  // the server keeps twins in memory: a twin built by an earlier test (or project) is shown, else the empty state
  const held = (await (await request.get('/api/twin')).json()).twin;
  if (held) await expect(twin.locator('.rc-step')).toContainText(`Twin ${held.twin_id}`);
  else await expect(twin).toContainText('No twin yet');

  await twin.locator('input[type=file]').setInputFiles({ name: 'notes.png', mimeType: 'image/png', buffer: Buffer.from('not a photo') });
  await expect(twin.locator('.notice-error')).toHaveText('not an image', { timeout: 30_000 });

  // a desk photo with an A4 sheet (a MuJoCo render), with its camera's field of view typed in
  const photo = await deskPhoto(request, 0);
  await page.getByLabel('Camera horizontal field of view in degrees').fill(photo.hfov.toFixed(1));
  const built = page.waitForResponse((r) => /\/api\/twin\?/.test(r.url()), { timeout: 150_000 });
  await twin.locator('input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await built).status()).toBe(200);
  const sbs = twin.locator('.tw-sbs img');
  await expect(sbs).toBeVisible({ timeout: 150_000 });
  await expect.poll(() => sbs.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
  await expect(twin.locator('.rc-step')).toContainText(/Twin \w+ · your photo/);
  const rows = twin.locator('.tw-table tbody tr');
  expect(await rows.count()).toBeGreaterThanOrEqual(3);
  await shot(page, info, 'twin-01-built', true);

  // type a height, refit
  const h = rows.first().locator('input.tw-h');
  await h.fill('40');
  await twin.getByRole('button', { name: 'Refit with typed heights' }).click();
  await expect(rows.first()).toContainText('typed', { timeout: 150_000 });

  // put it in the simulator: the arm's camera shows it
  await twin.getByRole('button', { name: "Show in the arm's camera" }).click();
  await expect(page.getByRole('combobox', { name: 'Scene' })).toHaveValue('twin');
  await expect(page.locator('.nb-head')).toContainText('twin');
  await shot(page, info, 'twin-02-in-arm-camera');

  // run a task on the twin; the run log streams and the goal is checked on the twin's physics
  const task = twin.getByLabel('Task to run on the twin');
  await expect(task).toBeVisible();
  const started = page.waitForResponse((r) => /\/api\/twin\/\w+\/run$/.test(r.url()));
  await twin.getByRole('button', { name: 'Run on the twin' }).click();
  expect((await started).status()).toBe(200);
  await expect(page.locator('.run-state')).toHaveText(/done|failed/, { timeout: 170_000 });
  await expect(page.locator('.tl')).toContainText('Plan from clicks');
  const goal = twin.locator('[role=status]');
  await expect(goal).toContainText(/Goal (met|not met) on the twin's physics/, { timeout: 15_000 });
  await shot(page, info, 'twin-03-task-run', true);

  // a reload keeps showing what the simulator holds: the scene picker says twin, not "3 cubes"
  await page.reload();
  await expect(page.locator('.nb-head')).toContainText('twin');
  await expect(page.getByRole('combobox', { name: 'Scene' })).toHaveValue('twin');
  await page.getByRole('tab', { name: 'Twin' }).click();

  // and the synthetic-photo button, labelled as such
  await twin.getByRole('button', { name: 'Try a synthetic photo' }).click();
  await expect(twin.locator('.rc-step')).toContainText('synthetic photo (MuJoCo render)', { timeout: 150_000 });
  await expect(twin.locator('figcaption')).toContainText('Synthetic photo (MuJoCo render)');
  await expectNoHScroll(page);
  await shot(page, info, 'twin-04-synthetic-button');
});

test('real camera: the sheet orientation is read from the clicked corners, and can be chosen', async ({ page, guard, request }, info) => {
  await page.goto('/');
  await page.getByRole('tab', { name: 'Real camera' }).click();
  // the synthetic desks lie the sheet landscape as the camera sees it
  const photo = await deskPhoto(request, 3);
  const up = page.waitForResponse((r) => r.url().endsWith('/api/realcam/upload'));
  await page.locator('.realcam input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await up).status()).toBe(200);
  const sheet = page.getByLabel('A4 sheet orientation, as you look at it');
  await expect(sheet).toHaveValue('auto');
  await expect(page.locator('.rc-step')).toContainText("Click the sheet's top-left corner (1 of 4, as you look at it)");

  const found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red');
  const cal = page.waitForResponse((r) => r.url().endsWith('/api/realcam/calibrate'));
  for (const [i, p] of found.corners.entries()) {
    await page.mouse.click(p.x, p.y);
    if (i < 3) await expect(page.locator('.rc-dot')).toHaveCount(i + 1);
  }
  const auto = await (await cal).json();
  expect(auto.orientation).toBe('landscape');
  await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 landscape (297 × 210 mm across × down), detected from the clicked corners.');
  await shot(page, info, 'cam-04-sheet-landscape-detected');

  // choosing portrait refits the same four clicks; the object then measures differently
  await page.getByLabel('Object height in millimetres').fill('25');
  const locate = async () => {
    const seg = page.waitForResponse((r) => r.url().endsWith('/api/realcam/segment'));
    await page.mouse.click(found.object!.x, found.object!.y);
    return (await (await seg).json()).target as { x_mm: number; y_mm: number };
  };
  const asLandscape = await locate();
  await sheet.selectOption('a4');
  await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 portrait (210 × 297 mm across × down), chosen.');
  await expect(page.locator('.rc-dot:not(.rc-click)')).toHaveCount(4);
  await expect(page.locator('.rc-out')).toHaveCount(0);
  const asPortrait = await locate();
  expect(Math.hypot(asPortrait.x_mm - asLandscape.x_mm, asPortrait.y_mm - asLandscape.y_mm)).toBeGreaterThan(10);
  await sheet.selectOption('a4-landscape');
  await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 landscape (297 × 210 mm across × down), chosen.');
  await expectNoHScroll(page);
  await shot(page, info, 'cam-05-sheet-chosen');
});

test('real camera: auto orientation follows the click order (first click on the top-right corner)', async ({ page, guard, request }, info) => {
  await page.goto('/');
  await page.getByRole('tab', { name: 'Real camera' }).click();
  const photo = await deskPhoto(request, 3); // its sheet lies landscape as the camera sees it
  const up = page.waitForResponse((r) => r.url().endsWith('/api/realcam/upload'));
  await page.locator('.realcam input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await up).status()).toBe(200);
  let found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red');
  expect(found.object).not.toBeNull();

  const calibrate = async (clicks: { x: number; y: number }[]) => {
    const cal = page.waitForResponse((r) => r.url().endsWith('/api/realcam/calibrate'));
    for (const [i, p] of clicks.entries()) {
      await page.mouse.click(p.x, p.y);
      if (i < 3) await expect(page.locator('.rc-dot:not(.rc-click)')).toHaveCount(i + 1);
    }
    const c = await cal;
    expect(c.status()).toBe(200);
    await expect(page.locator('.rc-step')).toHaveText('Type the object height, then click the object.', { timeout: 30_000 });
    return c.json();
  };
  const locate = async () => {
    const seg = page.waitForResponse((r) => r.url().endsWith('/api/realcam/segment'));
    await page.mouse.click(found.object!.x, found.object!.y);
    const s = await seg;
    expect(s.status()).toBe(200);
    return (await s.json()).target as { x_mm: number; y_mm: number };
  };

  // clicked from the top-left: edge 1 -> 2 is the long side
  const [tl, tr, br, bl] = found.corners;
  const a = await calibrate([tl, tr, br, bl]);
  expect(a.orientation).toBe('landscape');
  // height 0: the object is located on the table plane itself, so the two clicked frames must agree exactly
  // (with a typed height the parallax step also uses the camera pose, which an assumed 70 deg field of view
  // makes depend on the click order by a few mm)
  await page.getByLabel('Object height in millimetres').fill('0');
  const fromTl = await locate();

  // the same sheet clicked from the top-right: edge 1 -> 2 (top-right -> bottom-right) is the short side
  await page.getByRole('button', { name: 'Re-click sheet' }).click();
  found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red'); // the button click may have scrolled the page
  const [tl2, tr2, br2, bl2] = found.corners;
  const b = await calibrate([tr2, br2, bl2, tl2]);
  expect(b.orientation).toBe('portrait');
  expect(b.size_mm).toEqual([210, 297]);
  await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 portrait (210 × 297 mm across × down), detected from the clicked corners.');
  const fromTr = await locate();
  await expect(page.locator('.rc-out')).toContainText('not corrected');
  // the same object in the second frame (origin top-right, x down the right edge, y along the top edge leftwards)
  expect(Math.abs(fromTr.x_mm - fromTl.y_mm)).toBeLessThan(2);
  expect(Math.abs(fromTr.y_mm - (297 - fromTl.x_mm))).toBeLessThan(2);
  await expectNoHScroll(page);
  await shot(page, info, 'cam-06-sheet-clicked-from-top-right');
});

test('real camera: corners and the object can be marked with the keyboard (Tab, arrows, Enter)', async ({ page, guard, request }, info) => {
  await page.goto('/');
  await page.getByRole('tab', { name: 'Real camera' }).click();
  const photo = await deskPhoto(request, 3);
  const up = page.waitForResponse((r) => r.url().endsWith('/api/realcam/upload'));
  await page.locator('.realcam input[type=file]').setInputFiles({ name: 'desk.png', mimeType: 'image/png', buffer: photo.buffer });
  expect((await up).status()).toBe(200);
  const found = await readPhoto(page, '.rc-view img:not(.rc-mask)', 'red');
  expect(found.px.object).not.toBeNull();

  // Tab from the sheet picker lands on the photo, a button that says what to mark
  await page.getByLabel('A4 sheet orientation, as you look at it').focus();
  await page.keyboard.press('Tab');
  const photoBtn = page.getByRole('button', { name: /^Photo\./ });
  await expect(photoBtn).toBeFocused();
  await expect(photoBtn).toHaveAttribute('aria-label', /the sheet's top-left corner, 1 of 4/);

  // the crosshair starts in the middle; Shift+arrow moves 32 px, an arrow 8 px
  const { w, h } = found.px;
  let at = [Math.round(w / 2), Math.round(h / 2)];
  const moveTo = async (target: number[]) => {
    for (const axis of [0, 1]) {
      const [neg, pos] = axis === 0 ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown'];
      let d = target[axis] - at[axis];
      const key = d < 0 ? neg : pos;
      for (; Math.abs(d) >= 32; d -= Math.sign(d) * 32) await page.keyboard.press(`Shift+${key}`);
      for (; Math.abs(d) >= 4; d -= Math.sign(d) * 8) await page.keyboard.press(key);
      at[axis] = target[axis] - d;
    }
    await expect(page.locator('.rc-view .xhair')).toBeVisible();
  };
  const cal = page.waitForResponse((r) => r.url().endsWith('/api/realcam/calibrate'));
  for (const [i, c] of found.px.corners.entries()) {
    await moveTo(c);
    await page.keyboard.press('Enter');
    if (i < 3) {
      await expect(page.locator('.rc-dot:not(.rc-click)')).toHaveCount(i + 1);
      await expect(photoBtn).toHaveAttribute('aria-label', new RegExp(`, ${i + 2} of 4`));
    }
  }
  expect((await cal).status()).toBe(200);
  await expect(page.locator('.rc-sheet')).toHaveText('Sheet: A4 landscape (297 × 210 mm across × down), detected from the clicked corners.');
  await expect(photoBtn).toHaveAttribute('aria-label', /the object to locate/);
  await expect(photoBtn).toBeFocused();

  // and the object, with Space this time
  await moveTo(found.px.object!.map(Math.round));
  const seg = page.waitForResponse((r) => r.url().endsWith('/api/realcam/segment'));
  await page.keyboard.press(' ');
  expect((await seg).status()).toBe(200);
  await expect(page.locator('.rc-out')).toContainText(/x\s*-?\d+\.\d mm/);
  await expect(page.locator('.rc-out')).toContainText(/y\s*-?\d+\.\d mm/);
  await expect(page.locator('.rc-mask')).toBeVisible();
  await expectNoHScroll(page);
  await shot(page, info, 'cam-07-keyboard-located');
});

test('twin: after a reload the Twin tab shows the twin the server still holds', async ({ page, guard, request }, info) => {
  // build a twin straight through the API, as an earlier page load would have
  const photo = await deskPhoto(request, 1);
  const r = await request.post(`/api/twin?hfov_deg=${photo.hfov}&source=synthetic`, { data: photo.buffer, headers: { 'Content-Type': 'image/png' }, timeout: 150_000 });
  expect(r.status()).toBe(200);
  const tw = await r.json();

  await page.goto('/');
  await page.getByRole('tab', { name: 'Twin' }).click();
  const twin = page.locator('section.twin');
  const restoredOk = async () => {
    await expect(twin.locator('.rc-step')).toContainText(`Twin ${tw.twin_id} · synthetic photo (MuJoCo render)`);
    await expect(twin.locator('.rc-step')).toContainText('restored from the server');
    await expect(twin).not.toContainText('No twin yet');
    const sbs = twin.locator('.tw-sbs img');
    await expect(sbs).toBeVisible();
    await expect.poll(() => sbs.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
    await expect(twin.locator('.tw-table tbody tr')).toHaveCount(tw.objects.length);
    if (tw.tasks.length) await expect(twin.getByRole('button', { name: 'Run on the twin' })).toBeEnabled();
  };
  await restoredOk();
  await page.reload();
  await page.getByRole('tab', { name: 'Twin' }).click();
  await restoredOk();
  await shot(page, info, 'twin-05-restored-after-reload', true);
  // leaving the tab and coming back keeps it too
  await page.getByRole('tab', { name: 'Recorded runs' }).click();
  await page.getByRole('tab', { name: 'Twin' }).click();
  await restoredOk();
  await expectNoHScroll(page);
});
