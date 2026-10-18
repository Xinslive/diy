/**
 * 把仓库里的 Markdown 渲染成页面要的 `{ html, toc }`。
 *
 * 单独成文件而不是塞进 .vue：这里全是纯逻辑，能脱离组件单测，而组件模板
 * 是测不了的（仓库里已有同样的做法，见 utils/ 与 tests/ 的分工）。
 */
import MarkdownIt from 'markdown-it'

/**
 * 标题文字 → 锚点 id。
 *
 * 去掉行内 markdown 标记（`` ` ``、`*`、`_`、链接），小写，中文与字母数字
 * 原样保留，其余（空格、标点、斜杠）折成 `-`，首尾的 `-` 去掉。
 *
 * **中文不进转义**：HTML5 允许任意非空字符串做 id，浏览器会自己对 URL 里的
 * 中文编码，所以「包和签名」就是 `#包和签名` —— 比换成一串拼音或序号更好认，
 * 也不好记错。
 */
export function slugify(text) {
  return String(text)
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_~]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
}

/** 同一个标题出现两次时不能给同一个 id —— 后面的锚点就永远指不到。 */
function uniqueId(used, base) {
  const seed = base || 'section'
  if (!used.has(seed)) { used.add(seed); return seed }
  let n = 1
  while (used.has(`${seed}-${n}`)) n += 1
  const id = `${seed}-${n}`
  used.add(id)
  return id
}

/**
 * markdown-it 实例。
 *
 * `html: false` 是**安全决策**，不是省事：正文虽然来自本仓库、属可信内容，
 * 但关掉原始 HTML 只顺手一件事 —— 规范里哪天粘进一段 `<script>`，页面会
 * 把它转义成文本而不是执行。安全结论因此不依赖「我们保证没人写错」。
 */
const md = new MarkdownIt({
  html: false,
  linkify: false,
  breaks: false,
  typographer: false
})

const baseLinkOpen = md.renderer.rules.link_open
  || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))
const baseLinkClose = md.renderer.rules.link_close
  || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))

const hrefOf = (tokens, idx) => (tokens[idx] && tokens[idx].attrGet ? (tokens[idx].attrGet('href') || '') : '')

/**
 * 规范里的相对链接（如 `plugin-risk.md`、`plugin-example/plugin.json`）在
 * 部署站点上**不存在** —— 原样渲染就是一条死链，点了 404。
 *
 * 也不能改成仓库绝对地址：那些文件只在私有仓库里，改过去既是死链，
 * 又把仓库位置和私有路径公开了出去。所以**只留文字、去掉链接**：
 * 读者看到「plugin-risk.md」知道这里另有一份文档，而点不出一个坏的东西。
 */
function isRelativeLink(tokens, idx) {
  const href = hrefOf(tokens, idx)
  return href !== '' && !href.startsWith('#') && !/^[a-z][a-z0-9+.-]*:/i.test(href)
}

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  if (isRelativeLink(tokens, idx)) {
    // 记在 env 上，不能靠回看 tokens：link_close 与 link_open 之间还隔着
    // 文字，idx-1 拿到的是 text 而不是 link_open，距离还会随链接文字长度变。
    // markdown 的行内链接不能嵌套，所以一个计数就够。
    env.strippedLinkCloses = (env.strippedLinkCloses || 0) + 1
    return ''
  }
  const href = hrefOf(tokens, idx)
  // 外链新窗口打开并补 rel：没有理由让读者把当前页面的历史一起带走。
  if (/^https?:\/\//i.test(href)) {
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
  }
  return baseLinkOpen(tokens, idx, options, env, self)
}
md.renderer.rules.link_close = (tokens, idx, options, env, self) => {
  if (env.strippedLinkCloses > 0) {
    env.strippedLinkCloses -= 1
    return ''
  }
  return baseLinkClose(tokens, idx, options, env, self)
}

/**
 * 渲染一份 Markdown，同时产出目录。
 *
 * **目录和正文 id 来自同一遍 token 流**，这是本模块唯一会真正出错的地方：
 * 若目录改成拿正则去扫原文，代码围栏里的 `# 部署` 会被当成一级标题，目录
 * 就多出一项，而且点过去落到一个不存在的位置。同源则从构造上排除这种错位。
 *
 * @param {string} source
 * @returns {{ html: string, toc: Array<{level:number,title:string,id:string}> }}
 */
export function buildDocument(source) {
  const tokens = md.parse(String(source ?? ''), {})
  const toc = []
  const used = new Set()

  for (let i = 0; i < tokens.length; i += 1) {
    if (tokens[i].type !== 'heading_open') continue
    const inline = tokens[i + 1]
    if (!inline || inline.type !== 'inline') continue
    // 一级标题是文档名，不进目录：目录是「这一页里有什么」，不是把标题复述一遍。
    const level = Number(tokens[i].tag.slice(1))
    if (level < 2) continue
    const title = (inline.content || '').trim()
    if (!title) continue
    const id = uniqueId(used, slugify(title))
    tokens[i].attrSet('id', id)
    toc.push({ level, title, id })
  }

  return { html: md.renderer.render(tokens, md.options, {}), toc }
}
