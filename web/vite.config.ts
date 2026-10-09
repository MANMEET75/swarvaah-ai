import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/v1': {
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8000',
        headers: process.env.ADMIN_API_KEY ? { 'X-API-Key': process.env.ADMIN_API_KEY } : {},
      },
      '/health': process.env.VITE_PROXY_TARGET || 'http://localhost:8000',
    },
  },
})
