/// <reference types="vitest/config" />
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发时把 /api 转发给后端：浏览器只访问 Vite 这一个源，不需要在后端配置 CORS。
// 在 compose 外本机开发时后端是 localhost:8000；需要时用 VITE_API_TARGET 覆盖。
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // changeOrigin 保持 false：转发时保留浏览器的 Host（localhost:5173），与 Origin 一致，后端的 CSRF 检查才能通过（decisions/0023）
    proxy: { '/api': { target: apiTarget, changeOrigin: false } },
  },
  test: { environment: 'node' },
})
