/**
 * 「用了没导入」这类运行期 ReferenceError 的机械检查。
 *
 * 起因：`views/voucher/VoucherImport.vue` 的「下载导入模板」按钮调了
 * `downloadFile(...)`，而那个文件的 import 块里没有它 —— 点一下就
 * `ReferenceError: downloadFile is not defined`，整个 handler 失效。
 * 同一批的另外 5 个调用点（ReportView / OpeningBalance / GeneralLedger /
 * BalanceSheet / DetailLedger）全都正确 import 了，唯独漏这一个。
 *
 * **为什么这类 bug 能溜进主干。** Vite 构建**不会**报错：`<script setup>`
 * 里的自由标识符只有运行时才解析，构建期完全合法。它也不会被
 * `npm run build` 或现有的任何用例挡住 —— 没有一个用例会去点这个按钮。
 * 也就是说：它只能靠人手动点到才暴露，而那条路径在演示动线之外。
 *
 * 所以这里把它变成一条机械检查：**凡是调用了某个我们导出的工具函数，
 * 同一个文件就必须把它导进来。** 断言的是源码文本，不执行组件 ——
 * 要抓的恰恰是「构建期和运行期都抓不到、只有点击才炸」的那一类。
 *
 * 这里只盯 `downloadFile` 一个函数，不是要写成通用 lint：它已经是
 * 「跨 5 个视图共用、且历史上真的漏过一次」的那一个。铺开成通用规则
 * 之前，先确认这套断言不会对同名局部变量误报。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = path.join(ROOT, 'src')

/** 递归收集所有 .vue 文件（跳过 node_modules 等非源码目录）。 */
function vueFiles(dir) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...vueFiles(full))
    else if (entry.name.endsWith('.vue')) out.push(full)
  }
  return out
}

const files = vueFiles(SRC)

let pass = 0, fail = 0
const tests = []
const t = (name, fn) => tests.push([name, fn])

// ---------------------------------------------------------------------
// 先确认这个测试自己抓得到东西 —— 一个永远「通过」的守卫比没有更危险。
// ---------------------------------------------------------------------

t('采集器确实扫到了 .vue 文件（守卫自己没瞎）', () => {
  assert.ok(files.length > 20, `只扫到 ${files.length} 个 .vue，采集器八成坏了`)
})

// ---------------------------------------------------------------------
// 核心断言：调用了 downloadFile 就必须导入它
// ---------------------------------------------------------------------

const callers = files.filter((f) => /(?<![\w.])downloadFile\s*\(/.test(fs.readFileSync(f, 'utf8')))

t('确实有视图在调 downloadFile（否则下面的断言是空转）', () => {
  assert.ok(callers.length >= 5,
    `只找到 ${callers.length} 个调用方，正则八成不匹配了`)
})

for (const file of callers) {
  const rel = path.relative(ROOT, file)
  t(`调用 downloadFile 就必须导入：${rel}`, () => {
    const src = fs.readFileSync(file, 'utf8')
    // 只要有一行 import 里出现了这个标识符就算数：
    // `import { downloadFile } from '@/api/print'`、
    // `import * as printApi from '@/api/print'` + `printApi.downloadFile(...)`、
    // `import downloadFile from ...` 都放行。
    const imported = new RegExp(
      `import\\s+(?:[\\w*{},\\s]+from\\s+['"][^'"]+['"]|\\{[^}]*\\bdownloadFile\\b[^}]*\\})`)
      .test(src) || /\bdownloadFile\b\s*(?:,|})[^;]*from\s*['"][^'"]*print/.test(src)
    assert.ok(imported,
      `${rel} 调了 downloadFile 但没有 import 它 —— 运行时必然 ReferenceError。` +
      ` 本文件的下载按钮会整个失效。`)
  })
}

// ---------------------------------------------------------------------
// 反向断言：凭证导入页这个具体 case 必须已经修好。
// 单独钉一根 —— 上面的通用规则哪天被人放宽了，这根钉子还在。
// ---------------------------------------------------------------------

t('VoucherImport.vue 的 import 块里真的有 downloadFile（2026-10 的那个 bug）', () => {
  const src = fs.readFileSync(
    path.join(SRC, 'views/voucher/VoucherImport.vue'), 'utf8')
  assert.match(src, /import\s*\{[^}]*\bdownloadFile\b[^}]*\}\s*from\s*['"]@\/api\/print['"]/,
    '凭证导入页的「下载导入模板」按钮会报 ReferenceError')
})

for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0])
    fail++
  }
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
