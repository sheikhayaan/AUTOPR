import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The SPA calls a single base ('/api/*'). In dev, Vite proxies that to the
// FastAPI backend on :8000 and strips the prefix — so the browser stays
// same-origin (no CORS preflight) and the same client code works unchanged
// behind any reverse proxy in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.AUTOPR_API_URL || 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
