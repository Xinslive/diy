#!/usr/bin/env node
/**
 * 量一量某个路由**首屏要下多少**（构建产物的依赖图，不是单个 chunk 的大小）。
 *
 * 用法：node tools/measure_route_graph.mjs frontend/dist/.vite/manifest.json
 *
 * 为什么不能只看 chunk 大小：路由是 `() => import(...)` 进来的，
 * 它**静态依赖**的那些 chunk 会跟着一起下；**动态依赖**的不会。
 * 单看「VoucherEdit 自己多大」会把「它拽着哪些东西」算漏。
 */
import { readFileSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'

const manifestPath = process.argv[2] || 'frontend/dist/.vite/manifest.json'
// manifest 落在 dist/.vite/ 下，而它记的文件路径是相对 **dist/** 的。
const distDir = join(dirname(manifestPath), '..')
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))

function eagerGraph(key) {
  const seen = new Set()
  let raw = 0
  const walk = (k) => {
    const entry = manifest[k]
    if (!entry || seen.has(k)) return
    seen.add(k)
    for (const imp of entry.imports || []) walk(imp)
    raw += statSync(join(distDir, entry.file)).size
  }
  walk(key)
  return { files: [...seen], raw }
}

const entryKey = Object.keys(manifest).find((k) => manifest[k].isEntry)
const targets = process.argv.slice(3)

console.log('=== 首屏（index.html 的入口）===')
const e = eagerGraph(entryKey)
console.log(`  ${e.files.length} 个文件 / ${(e.raw / 1024).toFixed(1)} KB raw`)
for (const f of e.files) console.log(`    ${manifest[f].file}  ${(statSync(join(distDir, manifest[f].file)).size / 1024).toFixed(1)} KB`)

if (!targets.length) {
  for (const k of Object.keys(manifest)) {
    if (manifest[k].isEntry) continue
    const g = eagerGraph(k)
    console.log(`${manifest[k].file.padEnd(42)} ${g.files.length} 个 / ${(g.raw / 1024).toFixed(1)} KB`)
  }
} else {
  for (const t of targets) {
    const key = Object.keys(manifest).find((k) => k.includes(t) && !manifest[k].isEntry)
    if (!key) { console.log(`找不到 ${t}`); continue }
    const g = eagerGraph(key)
    console.log(`\n=== ${manifest[key].file} ===`)
    console.log(`  ${g.files.length} 个文件 / ${(g.raw / 1024).toFixed(1)} KB raw`)
    for (const f of g.files) console.log(`    ${manifest[f].file}  ${(statSync(join(distDir, manifest[f].file)).size / 1024).toFixed(1)} KB`)
  }
}
