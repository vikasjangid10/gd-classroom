import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    watch: { usePolling: true },
    // Invitation links are opened from other devices, and often through a tunnel whose
    // hostname is generated at run time. Vite rejects unknown Host headers by default,
    // which turns that into a blank page. Dev server only — never built or served.
    allowedHosts: true,
  },
})
