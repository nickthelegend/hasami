// Shared fixtures: every test fails on a console error, an uncaught page error, or a failed same-origin
// request, unless the test declared that exact response as expected (e.g. the 503 setup_required the
// planner answers without MODEL_API_KEY: Chrome logs any 4xx/5xx fetch as a console error).
import { test as base, expect, type Page, type TestInfo } from '@playwright/test';

export const API = 'http://127.0.0.1:8420';
export const LANDING = 'http://127.0.0.1:8421';

type Allowed = { status: number; path: RegExp };

export interface Guard {
  /** Declare an expected non-2xx same-origin response (status + URL path pattern). */
  allow(status: number, path: RegExp): void;
  errors: string[];
}

const SAME_ORIGIN = [API, LANDING];
const isSameOrigin = (url: string) => SAME_ORIGIN.some((o) => url.startsWith(o));

export const test = base.extend<{ guard: Guard }>({
  guard: async ({ page }, use, testInfo) => {
    const allowed: Allowed[] = [];
    const errors: string[] = [];
    const ok = (status: number, url: string) => {
      const path = new URL(url).pathname + new URL(url).search;
      return allowed.some((a) => a.status === status && a.path.test(path));
    };
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const loc = m.location()?.url ?? '';
      const st = /status of (\d{3})/.exec(m.text());
      if (st && loc && isSameOrigin(loc) && ok(Number(st[1]), loc)) return;
      errors.push(`console: ${m.text()} ${loc}`);
    });
    page.on('response', (r) => {
      const url = r.url();
      if (!isSameOrigin(url) || r.status() < 400) return;
      if (ok(r.status(), url)) return;
      errors.push(`HTTP ${r.status()} ${r.request().method()} ${url}`);
    });
    page.on('requestfailed', (r) => {
      const url = r.url();
      if (!isSameOrigin(url)) return;
      const why = r.failure()?.errorText ?? '';
      // The client closes the event stream on purpose when a run ends; the browser reports that as aborted.
      if (/ERR_ABORTED/.test(why) && /\/api\/events/.test(url)) return;
      errors.push(`requestfailed: ${r.method()} ${url} ${why}`);
    });
    await use({ allow: (status, path) => allowed.push({ status, path }), errors });
    expect(errors, 'console errors / failed same-origin requests').toEqual([]);
  },
});

export { expect };

/** No horizontal scroll: the document is not wider than the viewport. */
export async function expectNoHScroll(page: Page) {
  const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  expect(sw, `scrollWidth ${sw} > innerWidth ${iw}`).toBeLessThanOrEqual(iw);
}

/** A screenshot copied into the report folder later, named by project. */
export async function shot(page: Page, info: TestInfo, name: string, fullPage = false) {
  const file = info.outputPath(`${info.project.name}-${name}.png`);
  await page.screenshot({ path: file, fullPage });
  await info.attach(name, { path: file, contentType: 'image/png' });
}

export const isMobile = (info: TestInfo) => info.project.name === 'mobile';
