import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The FastAPI server (python -m hasami_server) listens on 8420 and serves dist/ in production.
// The dev and preview servers proxy /api to it, so they listen on 127.0.0.1 only and send no CORS headers:
// a page on another origin cannot read the desk photo, the real-camera frame or the runs through them.
// Vite >= 5.4.12 also refuses a Host header that is not localhost / 127.0.0.1 (DNS rebinding).
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5190,
    strictPort: true,
    cors: false,
    proxy: { '/api': { target: 'http://127.0.0.1:8420', changeOrigin: false } },
  },
  preview: {
    host: '127.0.0.1',
    port: 5190,
    cors: false,
    proxy: { '/api': { target: 'http://127.0.0.1:8420' } },
  },
  build: { outDir: 'dist', sourcemap: false },
});
