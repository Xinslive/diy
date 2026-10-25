/**
 * 页面显示时间必须走 ``utils/datetime.js`` 的 ``formatDateTime``。
 *
 * # 为什么要有这条断言
 *
 * 2026-10-25 生产上真实踩到的一次：每日备份的调度任务在「计划任务」页显示
 * 「2026-10-25 03:47 成功」，而在「数据安全」页找不到对应的备份记录。
 *
 * 备份**确实产出了记录**（``sys_backup_run`` 里那几行在库里），是显示错位：
 *
 * * 后端把每条连接的会话时区钉在 UTC（``backend/app/core/db.py``），
 *   接口回来的时间串一律是 UTC；
 * * 「计划任务」页走 ``formatDateTime``，显示**北京时间**；
 * * 「数据安全」页当时自己写了 ``(t || '').replace('T', ' ').slice(0, 19)``，
 *   **不转时区**，直接显示 UTC —— 于是同一个瞬间被显示成两个时间。
 *
 * 差的不只是 8 小时，还是**一天**：每日备份的 cron 是北京时间 03:47
 * （= UTC 前一天 19:47），所以计划任务页显示 ``10-25 03:47``，
 * 数据安全页显示 ``10-24 19:47``。照着成功记录上的时间去找，自然找不到。
 *
 * ``utils/datetime.js`` 的模块说明里早就写了这个坑（「用户看到的时间比实际
 * 早了 8 小时」），但**光有说明挡不住第二个页面再写一遍**。所以这里用源码级
 * 断言把整个 ``src/`` 扫一遍：谁再自己截断时间串，谁就红。
 *
 * 思路照抄 tests/book_init.test.mjs（源码级断言那一节）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { formatDateTime } from '../src/utils/datetime.js'

const ROOT = fileURLToPath(new URL('../src/', import.meta.url))
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8')

/** 递归列出 ``src/`` 下的所有 .vue / .js 源文件。 */
function sourceFiles(dir = ROOT) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(full))
    else if (/\.(vue|js)$/.test(entry.name)) out.push(full)
  }
  return out
}

/**
 * 自己截断时间串的写法。这几个是最常见的「看起来没问题」写法：
 * 直接砍掉 ISO 串的时区部分，或者用 ``split('T')[0]`` 拿日期。
 *
 * ``slice(0, 10)`` 上挂了否定后顾：``toISOString().slice(0, 10)`` 是在
 * **造**一个日期（不是显示后端给的时间），不算这一类。
 *
 * 注意 ``utils/datetime.js`` 自己要在说明里引用这些写法，所以它自己被排除
 * —— 它是**唯一**允许出现这些字符串的地方。
 */
const NAIVE_PATTERNS = [
  /replace\(\s*['"]T['"]\s*,\s*['"] ['"]\s*\)/,
  /\.slice\(\s*0\s*,\s*19\s*\)/,
  /(?<!toISOString\(\))\.slice\(\s*0\s*,\s*10\s*\)/,
  /\.split\(\s*['"]T['"]\s*\)\s*\[\s*0\s*\]/,
  /\.substring\(\s*0\s*,\s*10\s*\)/,
  /\.substr\(\s*0\s*,\s*10\s*\)/,
]

/**
 * 把注释去掉再扫。
 *
 * 为什么必须去：页面里**会**写「这里别再用 `replace('T',' ')` 了」这样的说明，
 * 而那说明恰好包含被判定的写法。扫原文会让**写警示的人**变成违规者 ——
 * 这种测试最后会被人加豁免名单加回来，然后什么也拦不住。
 *
 * 只去「整行注释」与块注释：整行注释按行首（允许缩进）判断，块注释按
 * 星号-斜杠配对与 ``<!-- … -->`` 判断。这样不会误伤字符串里的 ``//``
 * （比如 ``https://``），也不会漏掉真代码。
 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')      // 块注释
    .replace(/<!--[\s\S]*?-->/g, '')        // Vue 模板注释
    .split('\n')
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))  // 整行注释
    .join('\n')
}

test('src/ 下没有页面自己截断时间串（时间一律走 formatDateTime）', () => {
  const offenders = []
  for (const file of sourceFiles()) {
    const rel = path.relative(ROOT, file)
    // 唯一允许出现这些写法的就是实现本身 —— 它的说明里要引用它们。
    if (rel === 'utils/datetime.js') continue
    const src = stripComments(fs.readFileSync(file, 'utf8'))
    for (const pattern of NAIVE_PATTERNS) {
      if (pattern.test(src)) offenders.push(`${rel} 命中 ${pattern}`)
    }
  }
  assert.deepEqual(offenders, [],
    '这些文件自己截断了时间串，会按 UTC 显示而不是北京时间：\n' + offenders.join('\n'))
})

test('「数据安全」页导入并使用 formatDateTime', () => {
  const src = read('views/system/DataSecurity.vue')
  assert.match(src, /import\s*\{[^}]*formatDateTime[^}]*\}\s*from\s*['"]@\/utils\/datetime['"]/,
    '数据安全页没有从 @/utils/datetime 引入 formatDateTime')
  assert.match(src, /formatDateTime\(/,
    '数据安全页没有真正调用 formatDateTime')
})

test('「数据安全」页与「计划任务」页对同一个瞬间显示同一个时间', () => {
  // 生产上那条记录的原始时刻：北京时间 2026-10-25 03:47 = UTC 2026-10-24 19:47。
  const iso = '2026-10-24T19:47:06+00:00'

  // 「计划任务」页：formatDateTime（北京时间）
  assert.equal(formatDateTime(iso), '2026-10-25 03:47:06')

  // 「数据安全」页：修好之后也是同一套 utils，显示必须一致。
  const src = read('views/system/DataSecurity.vue')
  const fmtTime = src.match(/const fmtTime\s*=\s*\(t\)\s*=>\s*formatDateTime\(t\)/)
  assert.ok(fmtTime, '数据安全页的 fmtTime 没有走 formatDateTime')
  assert.equal(formatDateTime(iso), '2026-10-25 03:47:06',
    '同一个瞬间在两个页面上必须显示成同一个时间')
})

test('每日备份的 cron（北京时间 03:47）在 UTC 里是前一天 19:47', () => {
  // 这条钉住「为什么会差一天」：3:47 - 8 小时 = 前一天 19:47。
  // 差 8 小时只是显示难看；差一天才会让人「按成功记录的时间去找」然后找不到。
  const utcHour = 3 - 8
  assert.equal(utcHour < 0 ? utcHour + 24 : utcHour, 19)
  assert.equal(formatDateTime('2026-10-24T19:47:00+00:00'), '2026-10-25 03:47:00')
})
