import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/v1': process.env.VITE_PROXY_TARGET || 'http://localhost:8000', '/health': process.env.VITE_PROXY_TARGET || 'http://localhost:8000' } },
})
