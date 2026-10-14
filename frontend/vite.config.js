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
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true }
    }
  },
  // 阈值压回 500：整包注册时 1.1MB 正好卡在 1200 以下，告警形同虚设。
  // 现在按需引入后入口只有几百 KB，500 才是能真正报警的线。
  build: { outDir: 'dist', chunkSizeWarningLimit: 500 }
})