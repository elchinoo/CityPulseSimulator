import { defineConfig } from '@playwright/test';

/**
 * Records docs/VIDEO-SCRIPT.md: real Google Chrome (no browser download), 1920x1080, one video of the whole run.
 * Everything is local: a throwaway Postgres and `cpsim serve` on 127.0.0.1, started by the spec itself. Output goes to ./out (git-ignored).
 *   cd docs/video && npm install && npx playwright test         (RECORD_PACE=0.3 for a quick rehearsal, HEADED=1 to watch)
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'record.spec.ts',
  outputDir: './out/artifacts',
  workers: 1,
  retries: 0,
  timeout: 20 * 60_000,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    headless: !process.env.HEADED,
    viewport: { width: 1920, height: 1080 },
    video: { mode: 'on', size: { width: 1920, height: 1080 } },
    bypassCSP: true, // the app's strict Content-Security-Policy would block the caption and terminal overlays the recorder draws
    screenshot: 'off',
    trace: 'off',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
});
