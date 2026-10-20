import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The FastAPI server (python -m hasami_server) listens on 8420 and serves dist/ in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5190,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:8420', changeOrigin: false } },
  },
  preview: { port: 5190, proxy: { '/api': { target: 'http://127.0.0.1:8420' } } },
  build: { outDir: 'dist', sourcemap: false },
});
