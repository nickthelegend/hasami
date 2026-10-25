// The landing page (packages/landing/index.html) and the lab notebook it links to (evidence.html: the
// full page of sections 01-06), served as static files. The results sheet is checked row for row
// against the committed benchmark JSON it quotes.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test, expect, expectNoHScroll, shot, isMobile, LANDING } from './fixtures';

const BENCH = fileURLToPath(new URL('../../deployments/bench/', import.meta.url));
const REPO = 'https://github.com/nickthelegend/hasami';
const DASHBOARD = 'https://hasami-api.201-18-198-162.sslip.io/';
const EVIDENCE = `${LANDING}/evidence.html`;
const SECTIONS = [
  ['01', 'Frame', 'top'],
  ['02', 'Cut', 'cut'],
  ['03', 'Results', 'sheet'],
  ['04', 'Replay', 'replay'],
  ['05', 'Twin', 'twin'],
  ['06', 'Apparatus', 'apparatus'],
] as const;

test.beforeEach(async ({ page }) => {
  await page.goto(`${LANDING}/index.html`);
});

test('hero renders, every image loads, no horizontal scroll', async ({ page, guard }, info) => {
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('SAM cuts it out. The arm picks it up.');
  await expect(page.locator('.hud .simlabel')).toHaveText('MuJoCo simulation · official SO-101 model');
  await expect(page.locator('.readout')).toContainText('203.6 mm');
  await expect(page.locator('.nums b')).toHaveText(['146/160', '1106/1111', '0.14 mm']);
  await expect(page.locator('.nums span')).toHaveText(['Tasks', 'Checks', 'Error']);
  await expect(page.locator('#live .wait')).toContainText('Setup required');
  await expect(page.locator('#live .wait code')).toHaveText(['MODEL_API_KEY', 'HASAMI_SO101_PORT']);
  await expectNoHScroll(page);
  await shot(page, info, 'land-01-hero');
  // the lab notebook: scroll the whole page once, then every <img> and SVG <image> must have decoded
  await page.goto(EVIDENCE);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('The lab notebook: every run, every number.');
  for (let y = 0; y < (await page.evaluate(() => document.body.scrollHeight)); y += 600) {
    await page.evaluate((y) => window.scrollTo(0, y), y);
    await page.waitForTimeout(40);
  }
  const imgs = page.locator('img');
  const n = await imgs.count();
  expect(n).toBeGreaterThanOrEqual(7);
  for (let i = 0; i < n; i++) {
    await expect.poll(() => imgs.nth(i).evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth), { message: `img ${i}` }).toBeGreaterThan(0);
  }
  for (const src of ['assets/frame-hero.webp', 'assets/twin-sbs.webp']) {
    const r = await page.request.get(`${LANDING}/${src}`);
    expect(r.status(), src).toBe(200);
  }
  await expectNoHScroll(page);
  await shot(page, info, 'land-02-full', true);
});

test('landing links, then the lab notebook: section nav 01-06 and its hero links scroll to their sections', async ({ page, guard }, info) => {
  await expect(page.getByRole('link', { name: 'Open the live dashboard' })).toHaveAttribute('href', DASHBOARD);
  await page.getByRole('link', { name: 'See the evidence' }).click();
  await expect(page).toHaveURL(/evidence\.html#sheet$/);
  await expect(page.locator('#sheet')).toBeInViewport();
  await page.goto(`${LANDING}/index.html`);
  await page.getByRole('link', { name: 'Read the lab notebook' }).click();
  await expect(page).toHaveURL(/evidence\.html$/);
  const nav = page.getByRole('navigation', { name: 'Sections' });
  if (isMobile(info)) {
    // under 1100 px the section index folds away; the footer nav and the hero links carry navigation
    await expect(nav).toBeHidden();
  } else {
    for (const [no, name, id] of SECTIONS) {
      await nav.getByRole('link', { name: `${no}${name}` }).click();
      await expect(page).toHaveURL(new RegExp(`#${id}$`));
      await expect(page.locator(`#${id}`)).toBeInViewport();
    }
    await shot(page, info, 'land-03-nav-apparatus');
  }
  await page.goto(EVIDENCE);
  await page.getByRole('link', { name: 'Read the results sheet' }).click();
  await expect(page).toHaveURL(/#sheet$/);
  await expect(page.locator('#sheet')).toBeInViewport();
  await page.getByRole('link', { name: 'Run it locally' }).click();
  await expect(page).toHaveURL(/#run$/);
  await expect(page.locator('#run')).toBeInViewport();
  await expect(page.locator('#run pre')).toHaveText(
    '$ uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e \'.[dev]\' -c constraints.txt\n' +
      '$ (cd packages/dashboard && pnpm install && pnpm build)\n' +
      '$ python -m hasami_server',
  );
  await expect(page.locator('#run .run-note')).toHaveText('Then open http://127.0.0.1:8420/. The first use downloads about 150 MB of SAM 2.1 weights.');
  const footer = page.getByRole('navigation', { name: 'Footer' });
  for (const [, , id] of SECTIONS.slice(1)) {
    await footer.locator(`a[href="#${id}"]`).click();
    await expect(page).toHaveURL(new RegExp(`#${id}$`));
    await expect(page.locator(`#${id}`)).toBeInViewport();
  }
  // the notebook's brand mark goes back to the landing, whose brand mark goes to its top
  await page.getByRole('link', { name: 'Hasami home' }).click();
  await expect(page).toHaveURL(/\/index\.html$|\/$/);
  await page.getByRole('link', { name: 'Hasami, top of page' }).click();
  await expect(page).toHaveURL(/#top$/);
  await expect(page.locator('#top')).toBeInViewport();
  // the landing's own run block (the footer CTA)
  await expect(page.locator('#run pre')).toHaveText(
    '$ uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e \'.[dev]\' -c constraints.txt\n' +
      '$ (cd packages/dashboard && pnpm install && pnpm build)\n' +
      '$ python -m hasami_server',
  );
  // from a fresh load, the first Tab reaches the skip link
  await page.goto(`${LANDING}/index.html`);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#main$/);
});

test('every link resolves: anchors to sections, the notebook, the dashboard, the code', async ({ page, guard, request }, info) => {
  const landing = await page.locator('a[href]').evaluateAll((as) => as.map((a) => a.getAttribute('href')!));
  for (const h of landing) {
    if (h.startsWith('#')) await expect(page.locator(`[id="${h.slice(1)}"]`), h).toHaveCount(1);
    else expect(['evidence.html', 'evidence.html#sheet', DASHBOARD, REPO], h).toContain(h);
  }
  await page.goto(EVIDENCE);
  const hrefs = await page.locator('a[href]').evaluateAll((as) => as.map((a) => a.getAttribute('href')!));
  expect(hrefs.length).toBeGreaterThan(15);
  for (const h of hrefs) {
    if (h.startsWith('#')) {
      await expect(page.locator(`[id="${h.slice(1)}"]`), h).toHaveCount(1);
    } else {
      expect(['./', REPO], 'the only outside link is the code; ./ is the landing').toContain(h);
    }
  }
  // the repo is created when Nick pushes it; record its state rather than pretend
  const gh = await request.get(REPO, { maxRedirects: 0, failOnStatusCode: false }).catch(() => null);
  info.annotations.push({ type: 'github', description: `${REPO} -> HTTP ${gh?.status() ?? 'unreachable'}` });
});

test('the scissors cut once around the real SAM mask', async ({ page, guard }, info) => {
  const blade = page.locator('#blade');
  const at0 = await blade.getAttribute('transform');
  await page.locator('.loupe').scrollIntoViewIfNeeded();
  await page.waitForTimeout(1400);
  const mid = await blade.getAttribute('transform');
  expect(mid).not.toBe(at0);
  expect(mid).toContain('rotate(');
  await shot(page, info, 'land-04-cut-mid');
  await page.waitForTimeout(2600);
  const end = await blade.getAttribute('transform');
  await page.waitForTimeout(500);
  expect(await blade.getAttribute('transform')).toBe(end); // it cuts once, then stops
  await shot(page, info, 'land-05-cut-done');
});

test.describe('reduced motion', () => {
  test.use({ reducedMotion: 'reduce' });
  test('the cut is shown finished, nothing moves', async ({ page, guard }, info) => {
    const blade = page.locator('#blade');
    const first = await blade.getAttribute('transform');
    expect(first).toContain('rotate(');
    await expect(page.locator('#reveal-p')).toHaveAttribute('stroke-dashoffset', '0');
    await page.waitForTimeout(1500);
    expect(await blade.getAttribute('transform')).toBe(first);
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior)).toBe('auto');
    await shot(page, info, 'land-06-reduced-motion');
  });
});

type Fam = { ran: number; success: number; steps_ok: number; steps: number; iou_mean: number; verify: { checks: number; agree: number } };

test('results sheet matches the committed benchmark JSON row for row', async ({ page, guard }, info) => {
  await page.goto(EVIDENCE);
  const sheet = page.locator('#sheet');
  await sheet.scrollIntoViewIfNeeded();
  await expect(sheet.locator('.tally')).toContainText('146/160');
  const rows = sheet.locator('table.res').nth(1).locator('tbody tr');
  const texts = await rows.evaluateAll((trs) => trs.map((tr) => Array.from(tr.querySelectorAll('td')).map((td) => td.textContent!.trim())));
  let file: string | null = null;
  let checked = 0;
  for (const cells of texts) {
    const m = /^(\d{8}T\d{6}Z\.json)/.exec(cells[0]);
    if (cells.length === 1 && m) {
      file = m[1];
      continue;
    }
    const fam = (JSON.parse(readFileSync(BENCH + file!, 'utf8')).families as Record<string, Fam>)[cells[0]];
    expect(fam, `${file} ${cells[0]}`).toBeTruthy();
    expect(cells[2], `${file} ${cells[0]} tasks`).toBe(`${fam.success}/${fam.ran}`);
    expect(cells[3], `${file} ${cells[0]} steps`).toBe(`${fam.steps_ok}/${fam.steps}`);
    expect(cells[6], `${file} ${cells[0]} check = physics`).toBe(`${fam.verify.agree}/${fam.verify.checks}`);
    expect(cells[7], `${file} ${cells[0]} IoU`).toBe(fam.iou_mean.toFixed(3));
    checked++;
  }
  expect(checked).toBe(32);
  await shot(page, info, 'land-07-results-sheet');
  await expectNoHScroll(page);
});

test('replay strip and the apparatus list', async ({ page, guard }, info) => {
  await page.goto(EVIDENCE);
  const replay = page.locator('#replay');
  await replay.scrollIntoViewIfNeeded();
  await expect(replay.locator('.frame')).toHaveCount(6);
  await expect(replay.locator('.verdict.no')).toHaveCount(3);
  await expect(replay.locator('.verdict.ok')).toHaveCount(3);
  await expect(replay.locator('.replay-foot')).toContainText('3/3');
  await shot(page, info, 'land-08-replay');
  const items = page.locator('ul.apparatus > li');
  await expect(items).toHaveCount(9);
  await expect(items.filter({ hasText: 'Muse planner' }).locator('.stamp')).toHaveText('Setup required: MODEL_API_KEY');
  await expect(items.filter({ hasText: 'Real SO-101 arm' }).locator('.stamp')).toHaveText('Setup required: HASAMI_SO101_PORT');
  await expect(items.filter({ hasText: 'SAM 2.1 hiera-tiny' }).locator('.stamp')).toHaveText('running');
  await items.first().scrollIntoViewIfNeeded();
  await shot(page, info, 'land-09-apparatus');
  await expectNoHScroll(page);
});
