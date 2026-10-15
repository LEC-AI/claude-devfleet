import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3100,
    proxy: {
      '/api': {
        target: process.env.DEVFLEET_API_URL || 'http://localhost:18801',
        changeOrigin: true,
      },
    },
  },
});
