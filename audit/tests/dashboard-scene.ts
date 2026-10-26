// Find things on the overhead camera the way a user does: by looking at the picture. The test reads the
// pixels of the frame the dashboard is showing and returns where to click (page coordinates).
import { expect, type Locator, type Page } from '@playwright/test';

export type Where = { x: number; y: number; u: number; v: number };

/** The centre of a colour on the shown overhead frame: 'red' | 'green' | 'blue' cube, or the dark 'tray'. */
export async function locate(page: Page, what: 'red' | 'green' | 'blue' | 'tray'): Promise<Where> {
  const img = page.locator('.cam-stage img');
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
  const found = await img.evaluate((el: HTMLImageElement, what: string) => {
    const w = el.naturalWidth;
    const h = el.naturalHeight;
    const c = document.createElement('canvas');
    c.width = w;
    c.height = h;
    const ctx = c.getContext('2d')!;
    ctx.drawImage(el, 0, 0);
    const d = ctx.getImageData(0, 0, w, h).data;
    const us: number[] = [];
    const vs: number[] = [];
    for (let v = 0; v < h; v++) {
      for (let u = 0; u < w; u++) {
        const i = (v * w + u) * 4;
        const r = d[i], g = d[i + 1], b = d[i + 2];
        const hit =
          what === 'red' ? r > 120 && r - g > 70 && r - b > 70
          : what === 'green' ? g > 110 && g - r > 50 && g - b > 40
          : what === 'blue' ? b > 120 && b - r > 70 && b - g > 40
          : Math.max(r, g, b) < 90 && b - r >= 10; // the tray: dark, slightly blue (the arm's shadow is brown)
        if (hit) {
          us.push(u);
          vs.push(v);
        }
      }
    }
    if (us.length < 30) return null;
    const med = (a: number[]) => a.sort((x, y) => x - y)[a.length >> 1];
    const u = med(us);
    const v = med(vs);
    const r = el.getBoundingClientRect();
    return { u, v, x: r.left + ((u + 0.5) / w) * r.width, y: r.top + ((v + 0.5) / h) * r.height };
  }, what);
  expect(found, `${what} visible on the overhead frame`).not.toBeNull();
  return found!;
}

/** Click a spot on the camera stage (scrolls it into view first, then re-measures). */
export async function clickOn(page: Page, what: 'red' | 'green' | 'blue' | 'tray') {
  await page.locator('.cam-stage').scrollIntoViewIfNeeded();
  const p = await locate(page, what);
  await page.mouse.click(p.x, p.y);
  return p;
}

/** Number of painted (non-transparent) pixels on the mask overlay canvas. */
export async function maskPixels(canvas: Locator): Promise<number> {
  return canvas.evaluate((c: HTMLCanvasElement) => {
    if (!c.width || !c.height) return 0;
    const d = c.getContext('2d')!.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
    return n;
  });
}

/** Pick the "3 cubes" scene and press Reset scene, as a user does to start clean. */
export async function freshScene(page: Page) {
  await page.getByRole('combobox', { name: 'Scene' }).selectOption('classic');
  await resetScene(page);
  await expect(page.locator('.nb-head')).toContainText('classic');
}

/** Press Reset scene and wait until the camera shows the new scene's frame (it swaps in once decoded). */
export async function resetScene(page: Page) {
  const img = page.locator('.cam-stage img');
  await expect(img).toBeVisible();
  const before = await img.getAttribute('src');
  const reset = page.waitForResponse((r) => r.url().endsWith('/api/reset'));
  await page.getByRole('button', { name: 'Reset scene' }).click();
  expect((await reset).status()).toBe(200);
  await expect(page.getByRole('button', { name: 'Reset scene' })).toBeEnabled();
  await expect(img).not.toHaveAttribute('src', before!);
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
}

/** One clicked step: the object (wait for SAM's mask), then the target. */
export async function clickStep(page: Page, what: 'red' | 'green' | 'blue', n: number) {
  await clickOn(page, what);
  await expect(page.locator('.cam-hud')).toContainText('Now click where it goes', { timeout: 60_000 });
  await clickOn(page, 'tray');
  await expect(page.locator('.steps li:not(.pending)')).toHaveCount(n);
}
