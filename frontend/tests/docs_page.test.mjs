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

test('公开章节来自开发者文档源，不把内部报告装进页面', async () => {
  const { CHAPTER_GROUPS, CHAPTERS, EXAMPLE_FILES } = await source('/src/docs/content.js')
  const titles = CHAPTERS.map((chapter) => chapter.title)
  assert.deepEqual(titles, [
    '插件开发总览',
    '快速开始：从零创建一个插件',
    'Manifest、权限与运行时参考',
    '数据导入 API：从文件到整批凭证',
    '排错与错误码',
  ])
  assert.equal(CHAPTER_GROUPS.length, 4)
  assert.equal(EXAMPLE_FILES.length, 10)

  const publicSource = CHAPTERS.map((chapter) => `${chapter.title}\n${chapter.html}`).join('\n')
  for (const internalTerm of ['验收记录', '风险记录', '优化任务清单', 'P0：', 'P1：', 'P2：', 'plugin-verification.md', 'plugin-risk.md', 'plugin-roadmap.md']) {
    assert.ok(!publicSource.includes(internalTerm), `公开文档仍包含内部内容：${internalTerm}`)
  }
})

/**
 * 回归：目录项曾经一点就掉出文档页。
 *
 * 当时模板写的是 `href="#包和签名"`。应用是 hash 路由，`/#/docs` 的 hash 已被
 * 路由占用，裸片段会把整个 hash 替换掉，路由读到的路径变成「包和签名」，
 * 匹配不到任何路由，守卫随即把人送去登录页 —— 而服务器日志一切正常，
 * 页面也不会报错，只是安静地失效。
 *
 * 这类缺陷测试很容易放过：它不在任何 assert 覆盖的纯逻辑里，只有真点一下
 * 才会发现。所以这里从两头钉住 —— 锚点目标的形状，以及模板里不许再出现裸 `#`。
 */
test('目录锚点走路由 query，不会把 hash 换成陌生路径', async () => {
  const routerSrc = fs.readFileSync(path.join(FRONTEND, 'src', 'router', 'index.js'), 'utf8')
  // 前提：应用确实是 hash 路由。将来换掉路由，这条断言的前提就不成立了，
  // 那时要一并改这里，而不是让测试继续绿着骗人。
  assert.match(routerSrc, /createWebHashHistory/, '路由不再是 hash 路由了，请重新判断锚点该怎么做')

  const { sectionTo } = await source('/src/docs/anchor.js')
  assert.deepEqual(sectionTo('包和签名'), { name: 'docs', query: { s: '包和签名' } })

  // 把这个 query 位置还原成浏览器看到的 hash：路由读到的路径必须仍是 /docs。
  const { query, name } = sectionTo('包和签名')
  assert.equal(name, 'docs')
  assert.equal(`#/${name}?s=${encodeURIComponent(query.s)}`.split('?')[0], '#/docs',
    '锚点必须落在 /docs 路由下，而不是把 hash 整个换成锚点')

  // 模板这一页不允许出现任何原生 href。
  //
  // 判据特意写成「一个都没有」，而不是「不许以 # 开头」：后者只查得出
  // `href="#x"`，查不出实际写法 ``:href="`#${id}`"``（引号加反引号两个字符
  // 在 # 前面），于是测试全绿、缺陷照旧。跳转一律走 router-link 的 to。
  const view = fs.readFileSync(path.join(FRONTEND, 'src', 'views', 'docs', 'DocsView.vue'), 'utf8')
  assert.ok(
    !/\bhref\s*=/.test(view),
    '模板里还有原生 href：hash 路由下片段链接会把 hash 整个换掉，点一下就掉出文档页。跳转请一律用 router-link 的 to。'
  )
  assert.match(view, /<router-link[\s\S]{0,120}?:to="sectionTo\(item\.id\)"/,
    '目录项应当走 sectionTo(sectionTo 之外还有别的写法也要紧跟锚点检查)')
})

test('公开章节标题、正文标题和目录锚点保持同源', async () => {
  const { CHAPTERS, toc: allToc, EXAMPLES_SECTION } = await source('/src/docs/content.js')
  assert.ok(CHAPTERS.every(chapter => chapter.toc.length >= 4), '开发者章节过短，可能没有接上正文')

  const ids = allToc.map((x) => x.id)
  assert.equal(new Set(ids).size, ids.length, '文档的锚点 id 撞车了，后写的会覆盖前面的')
  assert.ok(allToc.some(item => item.id === EXAMPLES_SECTION.id), '示例代码没有进入目录')

  for (const chapter of CHAPTERS) {
    const sourceText = fs.readFileSync(path.join(REPO, 'docs', {
      '插件开发总览': 'plugin-overview.md',
      '快速开始：从零创建一个插件': 'plugin-guide.md',
      'Manifest、权限与运行时参考': 'plugin-spec.md',
      '数据导入 API：从文件到整批凭证': 'plugin-data.md',
      '排错与错误码': 'plugin-troubleshooting.md',
    }[chapter.title]), 'utf8')
    const headings = [...sourceText.matchAll(/^##\s+(.+)$/gm)].map((m) => m[1].trim())
    const inToc = new Set(chapter.toc.map((x) => x.title))
    assert.deepEqual(headings.filter((h) => !inToc.has(h)), [], `${chapter.title} 有标题没进目录`)
    assert.match(chapter.html, /<h2 id="[^"]+"/, `${chapter.title} 没有渲染可跳转的章节标题`)
  }
})

test('数据导入章节包含真实工作流和批次契约', async () => {
  const { dataDocument, toc: allToc, startHere } = await source('/src/docs/content.js')
  const titles = dataDocument.toc.map((x) => x.title)
  for (const title of ['一条完整工作流', '批次状态', '账套配置与版本', '去重、幂等与来源追溯']) {
    assert.ok(titles.includes(title), `数据导入章缺少 ${title}`)
  }
  assert.match(dataDocument.html, /ctx\.imports\.preview/, '正文里没有插件解析到持久预览的 API')
  assert.match(dataDocument.html, /services\.imports\.submit/, '正文里没有整批提交 API')
  assert.match(dataDocument.html, /<table>[\s\S]*services\.imports\.submit[\s\S]*<\/table>/,
    '标准服务方法没有渲染成表格')
  assert.ok(startHere(allToc).some((x) => /数据导入 API/.test(x.title)),
    '「从哪开始」没有指向数据导入章节')
})

test('文档的一级标题由页面当章节标题渲染，不混进正文', async () => {
  const { buildDocument } = await source('/src/docs/markdown.js')
  const { title, html, toc } = buildDocument([
    '# 写一个插件',
    '',
    '正文一句。',
    '',
    '## 第一步',
  ].join('\n'))
  assert.equal(title, '写一个插件')
  assert.deepEqual(toc.map((x) => x.title), ['第一步'], '一级标题不该进目录')
  assert.ok(!html.includes('写一个插件'), '文档标题又混进正文了，页面会出现第二个 h1')
  assert.ok(html.includes('第一步'), '正文本身不该被一起吃掉')
})

test('零入口的文档页挂 noindex，且离开时摘掉', () => {
  const view = fs.readFileSync(path.join(FRONTEND, 'src', 'views', 'docs', 'DocsView.vue'), 'utf8')
  assert.match(view, /noindex/, '零入口页没挂 noindex，爬虫猜得到路径就会收录')
  assert.match(view, /onUnmounted/, '离开文档页时必须摘掉 meta，否则整站都会被标成 noindex')
})

test('nginx 把 /docs 导到 /#/docs，否则文档链接打不开', () => {
  const conf = fs.readFileSync(path.join(REPO, 'deploy', 'nginx', 'acct.xins.live.conf'), 'utf8')
  assert.match(conf, /location\s*=\s*\/docs\s*\{/, '缺少 /docs 的精确匹配 location')
  // hash 路由下裸 /docs 会拿到首页 HTML，SPA 看不到路径，最后落到登录页
  assert.match(conf, /location\s*=\s*\/docs\s*\{[^}]*return\s+30\d\s+"?\/#\/docs"?/, '/docs 没有重定向到 /#/docs')
  // 必须排在 SPA 兜底之前，否则永远走不到。
  // 找 `try_files $uri` 这个指令本身而不是单词 —— 配置里解释原因的注释
  // 也提到了它，拿单词定位会被注释抢在前面。
  assert.ok(
    conf.indexOf('location = /docs') < conf.indexOf('try_files $uri'),
    '/docs 必须在 try_files 兜底之前匹配'
  )
})
