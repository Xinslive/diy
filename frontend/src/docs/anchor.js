/**
 * 文档页锚点的目标位置。
 *
 * 单独成文件是为了能被单测（见 tests/docs_page.test.mjs）：.vue 模板测不了，
 * 而「锚点会落到哪里」恰好是这一页最容易错、错了又最难看出原因的地方。
 */

/**
 * 目录锚点 → 路由位置。
 *
 * **不能写成 `href="#锚点"`。** 应用用的是 `createWebHashHistory`，URL 形如
 * `/#/docs`，hash 已经被路由占着。裸片段会把整个 hash 换成锚点（`/#包和签名`），
 * 路由读到的路径变成「包和签名」，匹配不到任何路由 —— 守卫随即把人送去登录页。
 * 症状是「点一下目录就掉出文档页」，服务器日志里什么异常都没有。
 *
 * 放进 query 后 URL 是 `/#/docs?s=包和签名`：仍由路由解析，刷新、复制链接、
 * 新标签页打开都成立。
 *
 * @param {string} id 正文的锚点 id
 * @returns {{ name: string, query: { s: string } }}
 */
export function sectionTo(id) {
  return { name: 'docs', query: { s: String(id ?? '') } }
}
