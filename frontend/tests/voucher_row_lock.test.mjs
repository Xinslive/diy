/**
 * 凭证分录的「锁定到第几行」（**线上那份 `utils/voucherRows.js`**）+
 * 科目按 id 的索引（`VoucherEdit.vue`）。
 *
 * 用户报的现象是「点进凭证页就卡住」：那张凭证有 154 条分录，打开一次
 * 主线程被占 41 秒（2026-10-16 实测），整页点不动。根因是两处相乘的
 * O(n²)：
 *   ① `blocked(i)` 挂在**每一行**上问「第 i 行能不能填」，早先每行从头
 *      扫到 i —— 154 行就是 1.2 万次判断；
 *   ② 每次判断都要问「这个科目是不是末级」，那一下是拿 find 在两千条
 *      科目里**线性翻**的。
 * 154² / 2 × 2000 次响应式代理读取 = 2400 万次，41 秒就是这么来的。
 *
 * 这里钉三件事：判定本身（第一条没写完的在哪儿）、**判定只跑一遍**
 * （谓词调用次数必须等于行数 —— 这条是防 O(n²) 复辟的关键），
 * 以及科目查找必须走 Map 索引、不许再退回线性 find。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const VITE = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const { findFirstProblem } = await VITE.ssrLoadModule('/src/utils/voucherRows.js')

const SRC = fs.readFileSync(new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

// 「没写完」= 没科目 / 挂到了上级科目 / 没金额（与 VoucherEdit 的 rowProblem 同一口径）
const unfinished = (r) => !r.subject_id || r.nonLeaf || !r.amount
const rows = (...flags) => flags.map((f, i) => ({ no: i + 1, subject_id: f === '-' ? null : 1, nonLeaf: f === 'N', amount: f === '-' || f === 'A' ? '' : 100 }))

console.log('== 锁的是行：前面有没写完的，后面全锁 ==')

t('第一条没写完的在下标 1 → 从第 3 行（0 起 2）起都锁着', () => {
  const r = rows('OK', '-', 'OK', 'OK')
  assert.equal(findFirstProblem(r, unfinished), 1)
})
t('全写完了：没有锁', () => {
  assert.equal(findFirstProblem(rows('OK', 'OK', 'OK'), unfinished), 3)
})
t('空表', () => {
  assert.equal(findFirstProblem([], unfinished), 0)
})
t('第一条就没写完 → 除了它自己，后面全锁', () => {
  assert.equal(findFirstProblem(rows('-', 'OK'), unfinished), 0)
})
t('挂在上级科目上算「没写完」（跟没科目、没金额同一口径）', () => {
  assert.equal(findFirstProblem(rows('OK', 'N'), unfinished), 1)
})
t('没金额也算「没写完」', () => {
  assert.equal(findFirstProblem(rows('OK', 'A'), unfinished), 1)
})

console.log('== 只扫一遍：这是 O(n²) 的护栏 ==')

t('谓词被调用的次数 = 行数，不是行数²', () => {
  const r = rows(...Array(154).fill('OK'))
  let calls = 0
  findFirstProblem(r, (x) => { calls++; return unfinished(x) })
  assert.equal(calls, 154)
})
t('第 7 行才有问题时也不会白扫后面的', () => {
  const r = rows(...Array(7).fill('OK'), '-', ...Array(146).fill('OK'))
  let calls = 0
  const at = findFirstProblem(r, (x) => { calls++; return unfinished(x) })
  assert.equal(at, 7)
  assert.equal(calls, 8)   // 到第 7 行为止，第 8 行一判断就出来了
})

console.log('== 页面真的走这两份判定 ==')

t('locked/blocked 问的是这一个下标，不是每行重扫', () => {
  assert.ok(/const blocked = \(i\) => firstProblemAt\.value < i/.test(SRC),
    'blocked(i) 应当是一次比较，而不是每行从头扫')
  assert.ok(/findFirstProblem\(form\.entries, rowProblem\)/.test(SRC),
    'firstProblemAt 没有走 utils/voucherRows.js')
  assert.ok(!/for \(let j = 0; j < i; j\+\+\)/.test(SRC), 'blocker 里还留着那个 O(行数) 的循环')
})

t('科目查找走 id 索引，不再线性 find', () => {
  assert.ok(/const subjectById = computed\(\(\) => new Map\(subjects\.value\.map/.test(SRC),
    '缺少 subjectById 索引 —— subjectOf 每行问好几次，两千条里线性翻就是几百万次代理读取')
  assert.ok(!/subjects\.value\.find\(/.test(SRC), '还有地方在 subjectOf/subjectText 里线性 find')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
