#!/usr/bin/env node
/**
 * 校验 ARCHITECTURE.md 的目录锚点：目录里每个 `#链接` 都必须命中正文里真实存在的标题 id。
 *
 * 用法：
 *   node tools/archdoc-toc.mjs                 # 校验（CI 用这个）
 *   node tools/archdoc-toc.mjs --emit-map       # 产出「章节标题 → 锚点」映射（重建文档时用）
 *
 * **为什么必须用真库算，不能自己写一份 slugify。**
 * 锚点是「标题文字 → id」的一套约定，而这套约定在各处并不一致：
 * GitHub 用 github-slugger（标点直接删掉），本仓库的 `frontend/src/docs/markdown.js`
 * 用另一套（标点折成 `-`）。两份实现自己写出来时很容易只对齐其中一套，
 * 于是在 GitHub 上看着好好的目录，点进去却落到页首 —— 而且没有任何报错。
 *
 * 所以这里直接用 github-slugger 算 —— 和 GitHub 渲染这份文档时用的是同一套规则。
 * 依赖装在 tools/archdoc-toc/ 下（`npm --prefix tools/archdoc-toc i`）。
 */
import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * github-slugger 装在 `tools/archdoc-toc/` 下（这个脚本自己的小依赖目录）。
 *
 * **为什么不用裸 `import ... from 'github-slugger'`**：Node 从**脚本所在目录**往上找
 * `node_modules`，而它装在子目录里 —— 裸导入会报 ERR_MODULE_NOT_FOUND。
 * 显式按路径导入才找得到。
 */
const { slug } = await import(
  resolve(HERE, 'archdoc-toc', 'node_modules', 'github-slugger', 'index.js')
)

const EMIT_MAP = process.argv.includes('--emit-map')
const docArg = process.argv.slice(2).find((a) => !a.startsWith('--'))
const DOC = resolve(HERE, '..', docArg || 'ARCHITECTURE.md')
const MAP_OUT = resolve(HERE, '.archdoc-toc-map.json')

const doc = readFileSync(DOC, 'utf8')
const lines = doc.split('\n')

/**
 * 正文标题 → 真实 id。
 *
 * **同名标题必须复现 GitHub 的去重规则**（第二个同名标题拿到 `-1` 后缀），
 * 否则目录里指向第二个同名章节的链接会算到第一个头上 —— 静默跳错位置。
 */
const seen = new Map()
const ids = new Set()
const titles = []
const chapterMap = {}
let fence = false

for (const line of lines) {
  const s = line.trim()
  if (s.startsWith('```')) { fence = !fence; continue }
  if (fence) continue
  const m = /^(#{1,6})\s+(.*)$/.exec(line)
  if (!m) continue
  const text = m[2]
  let id = slug(text)
  const n = seen.get(id) || 0
  seen.set(id, n + 1)
  if (n > 0) id = `${id}-${n}`
  ids.add(id)
  titles.push({ level: m[1].length, text, id })

  // 章节单独记一份：重建文档时目录要用。
  // **两种格式都要认** —— 重建前是 `## 11.13 标题`，重建后是 `### §11.13 标题`，
  // 而重建脚本正是靠这份映射给新旧两种形态生成一致的锚点。
  //
  // **锚点必须按「去噪后的标题」算**：旧标题带尾部日期（`（2026-10-21 改）`），
  // 重建后日期被移到正文首行、标题只剩主题 —— 用旧标题算出的锚点会带着日期，
  // 与重建后的真实 id 对不上，目录点进去就落到页首。
  const denoise = (t) =>
    t.replace(/\*\*|__/g, '').replace(/\s*（[^）]*\d{4}-\d{2}-\d{2}[^）]*）\s*$/, '').trim()
  const cm = /^§(\d+(?:\.\d+)?)\s+(.*)$/.exec(text)
  if (cm) {
    const clean = `§${cm[1]} ${denoise(cm[2])}`
    chapterMap[clean] = slug(clean)
  } else {
    const om = /^(\d+(?:\.\d+)?)\.?\s+(.*)$/.exec(text)
    if (om) {
      const clean = `§${om[1]} ${denoise(om[2])}`
      chapterMap[clean] = slug(clean)
    }
  }
}

/** 目录里的锚点链接：[显示文字](#锚点) */
const links = [...doc.matchAll(/\[([^\]]+)\]\(#([^)]+)\)/g)].map((m) => [m[1], m[2]])

if (EMIT_MAP) {
  writeFileSync(MAP_OUT, JSON.stringify(chapterMap, null, 1))
  console.log(`已写出章节锚点映射：${Object.keys(chapterMap).length} 条 -> ${MAP_OUT}`)
  process.exit(0)
}

const broken = links.filter(([, anchor]) => !ids.has(anchor))

console.log(`文档：${DOC}`)
console.log(`标题 ${titles.length} 个，唯一 id ${ids.size} 个`)
console.log(`目录链接 ${links.length} 条，断链 ${broken.length} 条`)
for (const [label, anchor] of broken) {
  console.log(`  ✗ ${label} -> #${anchor}`)
}

/** 同名标题会被 GitHub 自动加后缀，不算断链，但值得知道它们在哪。 */
const dupes = [...seen.entries()].filter(([, n]) => n > 1)
if (dupes.length) {
  console.log('\n同名标题（GitHub 自动加 -1/-2 后缀）：')
  for (const [id, n] of dupes) console.log(`  ${id} x${n}`)
}

if (broken.length) {
  console.error('\n✗ 目录有断链')
  process.exit(1)
}
console.log('\n✓ 目录全部命中')
