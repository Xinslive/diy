import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里用到的 el-* 才进包，样式跟着组件一起按需进。
    // 组件和指令（v-loading）都由解析器接管，所以 main.js 里不再整包注册。
    Components({
      resolvers: [ElementPlusResolver()],
      dts: false
    })
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // 文档页正文直接读仓库的 docs/，规范只有一份，不会与页面各说各话。
      // 代价是这个别名指向 vite 根（frontend/）之外，所以 server.fs 也要放开。
      '@docs': fileURLToPath(new URL('../docs', import.meta.url))
    }
  },
  server: {
    port: 5173,
    // vite 默认只允许读根目录以内的文件。不放开的话 dev 下 import '@docs/...'
    // 会被 403，而构建（Rollup）根本不拦 —— 于是变成「本地开发起不来、
    // 生产构建却是好的」这种最难查的差异。仓库根同时包含 frontend/，
    // 所以一条就够（fs.allow 是整体替换默认值的，不是追加）。
    fs: { allow: [fileURLToPath(new URL('..', import.meta.url))] },
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true }
    }
  },
  // 阈值压回 500：整包注册时 1.1MB 正好卡在 1200 以下，告警形同虚设。
  // 现在按需引入后入口只有几百 KB，500 才是能真正报警的线。
  build: { outDir: 'dist', chunkSizeWarningLimit: 500 }
})