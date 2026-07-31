import base44 from "@base44/vite-plugin"
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    base44({
      // Support for legacy code that imports the base44 SDK with @/integrations, @/entities, etc.
      // can be removed if the code has been updated to use the new SDK imports from @base44/sdk
      legacySDKImports: process.env.BASE44_LEGACY_SDK_IMPORTS === 'true',
      hmrNotifier: true,
      navigationNotifier: true,
      analyticsTracker: true,
      visualEditAgent: true
    }),
    react(),
  ],
  server: {
    port: 5157,
    proxy: {
      // Forward all Base44-compatible API calls (including SSE) to the local FastAPI backend.
      // Without this, requests to /api/apps/.../integration-endpoints/Core/InvokeLLMStream
      // would 404 on Vite and the agent would never reply.
      '/api': {
        target: 'http://localhost:5002',
        changeOrigin: true,
        ws: false,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split large third-party deps into stable vendor chunks so the app
        // code chunk stays small and brows can cache them across deploys.
        // Without this Vite emits a single multi-MB bundle that re-downloads
        // in full on every app change.
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'query-vendor': ['@tanstack/react-query'],
          'radix-vendor': [
            '@radix-ui/react-dialog',
            '@radix-ui/react-dropdown-menu',
            '@radix-ui/react-select',
            '@radix-ui/react-tabs',
            '@radix-ui/react-tooltip',
            '@radix-ui/react-popover',
          ],
          'chart-vendor': ['recharts'],
          'markdown-vendor': ['react-markdown', 'remark-gfm'],
          'editor-vendor': ['react-quill'],
          '3d-vendor': ['three'],
        },
      },
    },
  },
});
