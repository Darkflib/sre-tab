import { readFileSync } from 'node:fs';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Only the version string reaches the bundle. Importing package.json from
// application code would ship the whole manifest.
const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as {
  version: string;
};

// The app is served same-origin with the API in every deployment: either
// FastAPI mounts `dist/` at `/`, or a reverse proxy serves `dist/` and
// forwards `/api/` upstream. Hence `base: '/'` and relative API paths —
// no build-time API host is baked in.
//
// The production CSP is `script-src 'self'; style-src 'self'` with no
// `'unsafe-inline'`, so nothing here may inline a script or a stylesheet.
export default defineConfig({
  base: '/',
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // Keep every asset a real file rather than a data: URI, so the CSP
    // never has to permit `data:` for anything but images.
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    proxy: {
      // Dev only. Same-origin in production, so no CORS anywhere.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: false,
      },
    },
  },
});
