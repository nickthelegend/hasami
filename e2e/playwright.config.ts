// Browser E2E for Hasami in the installed Google Chrome (channel "chrome"), every test recorded
// (video, trace, screenshot). Two real servers on 127.0.0.1, started by serve.sh:
//   8420  python -m hasami_server: real MuJoCo + SAM 2.1, the built dashboard at /, run records in e2e/.runs
//   8421  the static landing (packages/landing)
// Needs the dashboard built first (pnpm --dir ../packages/dashboard build). Run: pnpm test:e2e
// Runs are real (tens of seconds each) and the server allows one at a time, so the suite is serial.
import { defineConfig } from '@playwright/test';

export const API = 'http://127.0.0.1:8420';
export const LANDING = 'http://127.0.0.1:8421';

export default defineConfig({
  testDir: './tests',
  outputDir: './test-results',
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { outputFolder: './playwright-report', open: 'never' }], ['json', { outputFile: './test-results.json' }]],
  use: {
    channel: 'chrome',
    baseURL: API,
    video: 'on',
    trace: 'on',
    screenshot: 'on',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'mobile', use: { viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } },
  ],
  webServer: [
    { command: './serve.sh api', url: `${API}/api/status`, reuseExistingServer: false, timeout: 180_000, stdout: 'pipe', stderr: 'pipe' },
    { command: './serve.sh landing', url: `${LANDING}/index.html`, reuseExistingServer: false, timeout: 30_000 },
  ],
});
