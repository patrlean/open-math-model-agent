import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    base: env.VITE_BASE_PATH || '/',
    plugins: [react()],
    build: {
      outDir: env.VITE_OUT_DIR || '../static',
      emptyOutDir: true,
      rollupOptions: {
        input: {
          dashboard: 'index.html',
          context: 'context.html',
        },
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': 'http://127.0.0.1:8765',
      },
    },
  }
})
