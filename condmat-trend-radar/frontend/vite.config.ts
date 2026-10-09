import process from 'node:process';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiPort = process.env.CONDMAT_RADAR_API_PORT ?? '8000';
const apiTarget = process.env.VITE_API_PROXY ?? `http://127.0.0.1:${apiPort}`;

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': apiTarget },
  },
});