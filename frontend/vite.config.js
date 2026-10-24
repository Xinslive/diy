import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

// 部署形态：**动静分离**。
//
//   VITE_BASE      —— 静态资源的公共前缀。默认 '/'（源站同源）。
//                     整站挂 CDN 时设成 CDN 域名，例如
//                     `VITE_BASE=https://cdn.example.com/`。
//   VITE_FORMAT   —— 'iife' 时打成一个普通脚本（不是 ES module）。
//
// **为什么不把 CDN 域名写死在这里**：CI 每个 commit 只构建一份产物，同一份
// 产物要能装到不同环境（生产 / 预发 / 本地）。绝大多数情况下**根本不需要
// 在构建期知道 CDN 域名** —— 发布时由 deploy 步骤改写 dist/index.html 里的
// 资源地址即可（见 deploy/deploy.sh），一份产物走多个环境。
//
// `VITE_BASE` 留给「自己构建、自己托管整站 CDN」的场景：那时整站（含
// index.html）都在 CDN 域名下，构建期把 base 定死最省事。
//
// **VITE_FORMAT=iife 是什么、什么时候用**：默认产物是 ES module，而
// **跨域加载 ES module 必须对方返回 `Access-Control-Allow-Origin`**（模块
// 脚本一律走 CORS 校验，没有「不校验」的选项）。绝大多数 CDN 都能加这个
// 响应头；但如果用的 CDN 不允许自定义响应头，就只能改成普通脚本 —— 普通
// 脚本默认不走 CORS 校验。
// 代价：iife 不能做代码分割（整包一个文件），首屏多下载一些用不到的代码。
// 本项目默认仍用 ES module（首屏只下需要的 chunk 更省流量）。

const FORMAT = (process.env.VITE_FORMAT || '').toLowerCase()
const iife = FORMAT === 'iife'

export default defineConfig({
  base: process.env.VITE_BASE || '/',
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
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 500,
    ...(iife ? {
      // 单入口 + 全部内联 = 一个普通脚本。跨域加载它不需要 CORS 响应头。
      rollupOptions: { output: { format: 'iife', inlineDynamicImports: true } }
    } : {})
  }
})
