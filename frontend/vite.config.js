import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Production: set VITE_API_URL=https://devfleet.yourdomain.com in Netlify env vars
  // Development: leave VITE_API_URL unset — the proxy below handles /api/* → localhost:18801
  server: {
    port: 3100,
    proxy: {
      '/api': {
        target: 'http://localhost:18801',
        changeOrigin: true,
      },
    },
  },
});
