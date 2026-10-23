/**
 * 语法自检（零依赖）。
 *
 * **作用范围**：
 *   · `src/**\/*.js` —— 整份文件做语法检查（`node --check`）；
 *   · `src/**\/*.vue` —— 只抠出 `<script>` / `<script setup>` 那一段做语法检查
 *     （模板不是 JavaScript，`node --check` 看不了；本仓库的 .vue 里也没有
 *     `<script lang="ts">`，所以不存在「TS 片段被当 JS 检查」的情况）；
 *   · `tests/**\/*.mjs` —— 整份文件做语法检查。
 *
 * **不做**什么（别指望它）：
 *   · 不查风格、不查命名、不查未使用的变量 —— 仓库里没有 eslint/prettier，
 *     加一套新的 lint 依赖会破坏 CI 的 `pnpm install --frozen-lockfile`；
 *   · 不查 .vue 的**模板**与 `<style>`；
 *   · 不查后端、不查 dist（构建产物）。
 *
 * 它只回答一件事：**这些文件在语法上还是不是一个合法的 JavaScript 模块**。
 * 够用，且不引入任何依赖。
 *
 * 用法：`npm run check`（在 frontend/ 下）。
 * 也可指定要查的目录：`node tests/check_syntax.mjs src/utils`（默认查 src 与 tests）。
 */
import { execFileSync } from 'node:child_process'
import { readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const ROOT = process.cwd()

/** 递归列出目录下的所有文件。 */
function walk(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

/** 抠出 .vue 里的脚本段（`<script>` 或 `<script setup>`）。 */
function scriptOf(source) {
  const a = source.indexOf('<script')
  if (a < 0) return null
  const b = source.indexOf('>', a) + 1
  if (b <= 0) return null
  const c = source.indexOf('</script>', b)
  if (c < 0) return null
  return source.slice(b, c)
}

let checked = 0
let tmpSeq = 0

const dirs = process.argv.slice(2).length ? process.argv.slice(2) : ['src', 'tests']
for (const dir of dirs) {
  const abs = join(ROOT, dir)
  let files = []
  try {
    files = walk(abs)
  } catch {
    continue          // 目录不存在就跳过，不算错
  }
  for (const file of files) {
    const rel = file.slice(ROOT.length + 1)
    if (file.endsWith('.js') || file.endsWith('.mjs')) {
      execFileSync(process.execPath, ['--check', file], { stdio: 'inherit' })
      checked++
      continue
    }
    if (!file.endsWith('.vue')) continue
    const code = scriptOf(readFileSync(file, 'utf8'))
    if (code === null) continue
    // .vue 的脚本段里可能有顶层 import —— 必须按 .mjs 存，node 才会按 ESM 解析
    const tmp = join(tmpdir(), `cwq-syntax-${process.pid}-${tmpSeq++}.mjs`)
    writeFileSync(tmp, code)
    try {
      execFileSync(process.execPath, ['--check', tmp], { stdio: 'inherit' })
    } catch (e) {
      console.error(`语法错误：${rel}`)
      throw e
    } finally {
      unlinkSync(tmp)
    }
    checked++
  }
}

console.log(`语法自检通过：${checked} 个文件（${dirs.join('、')} 下的 .js / .mjs 与 .vue 脚本段）`)
