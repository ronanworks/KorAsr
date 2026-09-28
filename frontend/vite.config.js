// frontend/vite.config.js
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // AudioWorklet.addModule needs a fetchable same-origin file, not an inlined
  // data URL. Keep worklet code as a separate emitted asset.
  build: { assetsInlineLimit: 0 },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
