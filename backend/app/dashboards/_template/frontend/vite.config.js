import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base: './' → built assets use RELATIVE paths, so the bundle works when
// served at any sub-path, e.g. /api/dashboards/apps/{slug}/
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
  },
});
