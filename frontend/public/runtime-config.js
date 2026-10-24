/*
 * 运行时配置 —— 发布时由 deploy 步骤整体改写本文件（不动代码、不重建）。
 * 默认值 = 源站同源部署：API 走 /api，静态资源走 /。
 * 详见 frontend/src/config.js 与 README「动静分离 / CDN 部署」。
 */
window.__CWQ_RUNTIME__ = {
  apiBase: '/api',
  assetBase: '/',
}
