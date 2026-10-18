/**
 * 文档页（`/#/docs`）的行为与约束。
 *
 * **为什么其中一半在断言「没有入口」。** 需求是「用户不访问 /docs 都不知道
 * 还有文档」。这件事只靠人记得住：半年后有人觉得「顺便在系统管理里加个
 * 文档链接挺合理」，需求就无声失效了，而且**没有任何测试会红** —— 页面还在、
 * 路由还在、构建还是绿的，只是它突然有了一个入口。所以这里把「搜不到入口」
 * 本身当成一条要长期守住的性质。
 *
 * 另一半是渲染层的结构性风险：目录与正文锚点必须同源，否则点目录会落到
 * 不存在的位置（代码围栏里的 `# 注释` 会被当成标题就是那个来源）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { source } from './plugin_test_support.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const REPO = path.resolve(FRONTEND, '..')

function walk(dir, filter, found = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, filter, found)
    else if (filter(full)) found.push(full)
  }
  return found
}

/** 文档页自己的文件不算「别处出现入口」：它当然要知道自己存在。 */
function isDocsOwnFile(file) {
  const rel = path.relative(FRONTEND, file)
  return rel.startsWith(`src${path.sep}views${path.sep}docs${path.sep}`)
    || rel.startsWith(`src${path.sep}docs${path.sep}`)
    || rel === path.join('src', 'router', 'index.js')
}

// 模式锚定 `#/docs`：README 里有 `docs/plugin-spec.md` 这种**仓库路径**，
// 用裸 `/docs` 匹配会把它误判成入口，于是测试反过来逼人删掉正常文档链接。
const ENTRY_PATTERN = /#\/docs/

test('全站没有任何指向文档页的入口', () => {
  const scanned = [
    ...walk(path.join(FRONTEND, 'src'), (f) => /\.(vue|js|mjs)$/.test(f)).filter((f) => !isDocsOwnFile(f)),
    path.join(REPO, 'README.md'),
    path.join(REPO, 'ARCHITECTURE.md'),
  ]
  assert.ok(scanned.length > 20, `扫描到的文件太少（${scanned.length}），规则本身可能失效`)
  const hits = scanned.filter((f) => ENTRY_PATTERN.test(fs.readFileSync(f, 'utf8')))
  assert.deepEqual(hits, [], `这些文件里出现了文档页入口：\n${hits.join('\n')}`)
})

test('导航里没有文档条目（动态派生的插件菜单也算在内）', async () => {
  const { visibleNav } = await source('/src/router/nav.js')
  for (const isAdmin of [true, false]) {
    for (const module of visibleNav(isAdmin)) {
      for (const item of module.items) {
        assert.ok(
          !ENTRY_PATTERN.test(JSON.stringify(item)) && item.name !== 'docs',
          `导航里出现了文档入口：${module.key} / ${item.title}`
        )
      }
    }
  }
})

test('文档路由存在且免登录，并排在 catch-all 之前', () => {
  const src = fs.readFileSync(path.join(FRONTEND, 'src', 'router', 'index.js'), 'utf8')
  assert.match(src, /path:\s*'\/docs'/, '文档路由不见了')
  // 免登录是这份文档能被「知道网址的人」直接打开的前提
  assert.match(src, /path:\s*'\/docs'[\s\S]{0,220}?meta:\s*\{\s*public:\s*true/)
  const docsAt = src.indexOf("path: '/docs'")
  const catchAllAt = src.indexOf(':pathMatch')
  assert.ok(docsAt > 0 && catchAllAt > docsAt, '文档路由必须排在 catch-all 之前，否则永远匹配不到')
})

test('slugify 保留中文，重复标题不会撞 id', async () => {
  const { slugify } = await source('/src/docs/markdown.js')
  assert.equal(slugify('包和签名'), '包和签名')
  assert.equal(slugify('Python SDK'), 'python-sdk')
  assert.equal(slugify('`code` 与 **强调**'), 'code-与-强调')
})

test('代码围栏里的 # 不是标题，目录与正文锚点完全对齐', async () => {
  const { buildDocument } = await source('/src/docs/markdown.js')
  const { html, toc } = buildDocument([
    '# 标题',
    '',
    '```bash',
    '# 部署前先备份',
    'echo hi',
    '```',
    '',
    '## 信任与授权',
    '',
    '正文。',
    '',
    '## 重复',
    '',
    '## 重复',
  ].join('\n'))

  assert.deepEqual(toc.map((x) => x.title), ['信任与授权', '重复', '重复'])
  assert.ok(!toc.some((x) => x.title.includes('部署')), '代码块里的注释被当成了标题')

  // 目录给出去的每个 id，正文里都要真的有同名锚点；反过来也一样。
  const inHtml = [...html.matchAll(/<h[23] id="([^"]+)"/g)].map((m) => m[1])
  assert.deepEqual(inHtml, toc.map((x) => x.id))
  assert.equal(new Set(inHtml).size, inHtml.length, '正文里有重复的锚点 id')
})

test('原始 HTML 被转义，不会变成可执行标记', async () => {
  const { buildDocument } = await source('/src/docs/markdown.js')
  const { html } = buildDocument('段落 <script>alert(1)</script> 结束。')
  assert.ok(!html.includes('<script'), '原始 HTML 被原样输出了')
  assert.match(html, /&lt;script&gt;/)
})

test('规范里的相对链接不渲染成死链，外链照常保留', async () => {
  const { buildDocument } = await source('/src/docs/markdown.js')
  const { html } = buildDocument([
    '见 [plugin-risk.md](plugin-risk.md) 与 [example](plugin-example/plugin.json)。',
    '',
    '外部见 [示例](https://example.com/a)。',
  ].join('\n'))
  // 部署站点上不存在这些相对路径，点了就是 404；仓库又是私有的，
  // 改成绝对地址既死又泄露仓库位置 —— 所以只留文字。
  assert.ok(!html.includes('href="plugin-risk.md"'), '相对链接仍然渲染成了可点的死链')
  assert.ok(!html.includes('href="plugin-example/plugin.json"'))
  assert.match(html, /见 plugin-risk\.md 与 example/)
  // 去掉 <a> 就要连 </a> 一起去掉，否则会留下一堆孤立闭合标签。
  // 只看第一段：第二段里那个外链的 </a> 是合法的。
  const firstParagraph = html.slice(0, html.indexOf('</p>'))
  assert.ok(!firstParagraph.includes('</a>'), '去掉了链接却留下了孤立的 </a>')
  // 外链不该被这条规则误伤：它有真实地址，还要新窗口 + rel
  assert.match(html, /href="https:\/\/example\.com\/a"[^>]*rel="noopener noreferrer"|rel="noopener noreferrer"[^>]*href="https:\/\/example\.com\/a"/)
})

test('正文确实来自仓库的规范文件，不是页面里另写的一份', async () => {
  const spec = fs.readFileSync(path.join(REPO, 'docs', 'plugin-spec.md'), 'utf8')
  const { specDocument, EXAMPLE_FILES } = await source('/src/docs/content.js')
  assert.ok(specDocument.toc.length >= 8, `规范目录只有 ${specDocument.toc.length} 项，规范可能没接上`)
  // 规范里的每个二级标题都必须出现在目录里
  const headings = [...spec.matchAll(/^##\s+(.+)$/gm)].map((m) => m[1].trim())
  const inToc = new Set(specDocument.toc.map((x) => x.title))
  assert.deepEqual(headings.filter((h) => !inToc.has(h)), [])
  assert.equal(EXAMPLE_FILES.length, 5)
})
