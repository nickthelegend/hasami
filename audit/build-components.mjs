import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';
const root = fileURLToPath(new URL('../', import.meta.url));
const dashboard = createRequire(`${root}packages/dashboard/package.json`);
const vite = createRequire(dashboard.resolve('vite'));
await vite('esbuild').build({
  entryPoints: [`${root}audit/plan-component.tsx`],
  bundle: true, jsx: 'automatic', minify: true,
  alias: { react: `${root}packages/dashboard/node_modules/react`, 'react-dom': `${root}packages/dashboard/node_modules/react-dom` },
  define: { 'process.env.NODE_ENV': '"production"' },
  outfile: `${root}packages/dashboard/dist/audit-component.js`,
});
writeFileSync(`${root}packages/dashboard/dist/audit-component.html`, '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/audit-component.css"></head><body><div id="root"></div><script src="/audit-component.js"></script></body></html>');
