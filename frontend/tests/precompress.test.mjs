/**
 * 构建期 brotli 预压缩（scripts/precompress.mjs）。
 *
 * 这个脚本的产物是 nginx 直接发给浏览器的东西 —— 压错了就是「线上白屏」
 * 或者「资源解不开」。所以盯住三件事：
 *   1. 压出来的那份**能原样解回原文**（不是「看起来是压缩过的字节」就行）；
 *   2. **只压该压的**（图片、字体不压；压了反而大的不压）；
 *   3. **原文件一个字节都不能动**（nginx 找不到 br 版本时要退回它）。
 */
import assert from 'node:assert/strict'
import { brotliDecompressSync } from 'node:zlib'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { precompress, BR_DIR } from '../scripts/precompress.mjs'

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

/** 造一个假的 dist 目录。 */
function makeDist(files) {
  const dir = mkdtempSync(join(tmpdir(), 'cwq-br-'))
  for (const [rel, content] of Object.entries(files)) {
    const p = join(dir, rel)
    mkdirSync(join(p, '..'), { recursive: true })
    writeFileSync(p, content)
  }
  return dir
}

const JS = 'export const a = 1;\n'.repeat(400)      // 压得动
const PNG = 'not-really-a-png'.repeat(400)           // 内容可压，但后缀不在白名单

t('可压缩的文本文件产出 brotli 版本，而且能原样解回来', () => {
  const dist = makeDist({ 'assets/app.js': JS })
  try {
    const r = precompress(dist)
    assert.equal(r.compressed, 1, `压了 ${r.compressed} 个`)
    const brPath = join(dist, BR_DIR, 'assets/app.js')
    assert.ok(existsSync(brPath), `br 树里没有 assets/app.js（目录结构没对上）`)
    const back = brotliDecompressSync(readFileSync(brPath)).toString('utf8')
    assert.equal(back, JS, '解回来的内容与原文不一致')
    assert.ok(r.compressedBytes < r.originalBytes, '压完没变小')
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('原文件一个字节都没动（nginx 找不到 br 版本时要退回它）', () => {
  const dist = makeDist({ 'assets/app.js': JS })
  try {
    precompress(dist)
    assert.equal(readFileSync(join(dist, 'assets/app.js'), 'utf8'), JS)
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('后缀不在白名单里的不压（图片内容再可压也不压）', () => {
  const dist = makeDist({ 'assets/logo.png': PNG })
  try {
    const r = precompress(dist)
    assert.equal(r.compressed, 0, '图片被压了')
    assert.ok(!existsSync(join(dist, BR_DIR, 'assets/logo.png')), '图片产出了 br 版本')
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('压了反而大的小文件不产出 br 版本（nginx 会自动退回原文件）', () => {
  const dist = makeDist({ 'assets/tiny.js': 'x' })
  try {
    const r = precompress(dist)
    assert.equal(r.compressed, 0, '压了反而大的文件也被压了')
    assert.ok(!existsSync(join(dist, BR_DIR, 'assets/tiny.js')))
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('上一版残留的 br 文件会被清掉（不能让删掉的旧哈希还留在盘上）', () => {
  const dist = makeDist({ 'assets/app.js': JS })
  try {
    const stale = join(dist, BR_DIR, 'assets/old-DEADBEEF.js')
    mkdirSync(join(stale, '..'), { recursive: true })
    writeFileSync(stale, 'stale')
    precompress(dist)
    assert.ok(!existsSync(stale), '上一版的残留还在')
    assert.ok(existsSync(join(dist, BR_DIR, 'assets/app.js')), '这一版的反而没了')
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('发布时会被改写的文件不压（index.html / runtime-config.js）', () => {
  const dist = makeDist({
    'index.html': '<html><script src="/runtime-config.js"></script></html>',
    'runtime-config.js': 'window.__CWQ_RUNTIME__={"apiBase":"/api"}',
    'assets/app.js': JS
  })
  try {
    const r = precompress(dist)
    assert.equal(r.compressed, 1, `只该压 assets/app.js，实际压了 ${r.compressed} 个`)
    assert.ok(!existsSync(join(dist, BR_DIR, 'index.html')), 'index.html 产出了 br 版本')
    assert.ok(!existsSync(join(dist, BR_DIR, 'runtime-config.js')), 'runtime-config.js 产出了 br 版本')
    // **这两个文件在发布时会被改写**（资源基址、API 基址），构建期压出来的
    // 那份是过期的 —— 留着它，哪天有人给这两个路径开了 br，线上就会发出去
    // 一份指回旧地址的 index.html。
    assert.ok(existsSync(join(dist, BR_DIR, 'assets/app.js')), 'assets/app.js 反而没压')
  } finally { rmSync(dist, { recursive: true, force: true }) }
})

t('dist 不存在时直接报错（不是静默产出空目录）', () => {
  assert.throws(() => precompress(join(tmpdir(), 'cwq-br-does-not-exist-xyz')), /不存在/)
})

process.exit(fail ? 1 : 0)
