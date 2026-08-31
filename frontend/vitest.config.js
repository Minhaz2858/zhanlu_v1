import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    setupFiles: './vitest.setup.js',
    // e2e specs are Playwright specs (run via `npm run test:e2e`); they
    // must NOT be collected by the unit-test runner. They assume a live
    // server + browser and fail spuriously under vitest.
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
  },
});
