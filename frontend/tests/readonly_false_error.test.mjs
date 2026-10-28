/**
 * **只读账号不许看到假的权限错误**（2026-10-28 修的老遗留）。
 *
 * 只读账号（VIEWER）在这个系统里能看、不能改。以前有三个地方会让它**看到
 * 一条权限错误，而页面其实好好地显示着** —— 用户被一句无关的话唬住，
 * 以为自己点错了什么。
 *
 * 这一组盯的就是那三处的修法与**判据**：
 *
 *  * **`neighbour`（前一张/后一张）** —— 前端把两颗按钮摆给只读账号看，
 *    后端却拒 → **前后端结论不一致**，所以**后端放开成只读**；
 *  * **`period-check`（断号体检）、`/next` 与 `/insert-preview`（写动作预填值）**
 *    —— 它们是**写动作的配套接口**，只读账号够不着，前端也不调 →
 *    两端结论一致，**后端的写闸留着**。
 *
 * **跨语言核对**：后端那几行依赖是 Python、前端这几处是 JS，两边各判一次的话
 * 迟早出现「界面能点、点了被拒」—— 所以这里**直接读后端源码**核对。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const read = (p) => fs.readFileSync(new URL(p, import.meta.url), 'utf8')
const VOUCHER_EDIT = read('../src/views/voucher/VoucherEdit.vue')
const VOUCHER_LIST = read('../src/views/voucher/VoucherList.vue')
const FLOW = read('../src/views/bank/FlowReconciliation.vue')
const API_VOUCHER = read('../src/api/voucher.js')
const API_BANK = read('../src/api/bankstatement.js')
const API_CLIENT = read('../src/api/client.js')
const BACKEND_VOUCHERS = read('../../backend/app/api/vouchers.py')

/** 去掉注释，只留会被渲染/执行的那部分。 */
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

/** 抠出某个函数（从它到下一个同级的 `async function` / `function` 之前）。 */
function blockOf(src, name) {
  const code = stripComments(src)
  const at = code.indexOf(`function ${name}`)
  assert.ok(at > 0, `找不到 ${name}`)
  const next = code.indexOf('\nfunction ', at + 10)
  const nextAsync = code.indexOf('\nasync function ', at + 10)
  const ends = [next, nextAsync].filter((i) => i > at)
  return code.slice(at, ends.length ? Math.min(...ends) : code.length)
}

// ---------------------------------------------------------------- 后端

test('后端：neighbour 挂的是「只查授权」，不是写权限', () => {
  // 前端把「前一张/后一张」两颗按钮摆给只读账号看 → 后端就不能拒。
  const code = stripComments(BACKEND_VOUCHERS)
  const at = code.indexOf('def neighbour(')
  assert.ok(at > 0, '找不到 neighbour 端点')
  const sig = code.slice(at, code.indexOf('):', at))
  assert.match(sig, /get_book_link/, 'neighbour 还挂着写权限 —— 只读账号翻不动凭证')
  assert.doesNotMatch(sig, /require_book_write/)
})

test('后端：period-check 仍然挂着写权限（它是「整理凭证」那道写动作的配套接口）', () => {
  // **别照着 neighbour 把它也放开**：那两处的结论不一致，这一处一致。
  const code = stripComments(BACKEND_VOUCHERS)
  const at = code.indexOf('def period_check(')
  assert.ok(at > 0, '找不到 period_check 端点')
  const sig = code.slice(at, code.indexOf('):', at))
  assert.match(sig, /require_book_write/)
})

// ---------------------------------------------------------------- 前端：写动作配套的那几处

test('凭证列表页：只读账号不调「断号体检」', () => {
  const block = blockOf(VOUCHER_LIST, 'loadCheck')
  assert.match(block, /if \(!store\.canWrite\)/)
  // **不能是「调了再兜住」** —— 那个接口后端按设计拒它，发了就有个 403。
  assert.match(block, /api\.voucherPeriodCheck/)
})

test('新建凭证页：只读账号不调那两趟「写动作预填值」', () => {
  const next = blockOf(VOUCHER_EDIT, 'loadNextDefaults')
  assert.match(next, /if \(!store\.canWrite\) return \{\}/)
  const ins = blockOf(VOUCHER_EDIT, 'loadInsertPreview')
  assert.match(ins, /if \(!store\.canWrite\) return null/)
})

test('流水明细页：只读账号不调「可制证清单」', () => {
  const block = blockOf(FLOW, 'loadPlan')
  assert.match(block, /if \(!store\.canWrite\)/)
})

// ---------------------------------------------------------------- 拦截器闭嘴

test('「这一趟的失败是预期的」由拦截器闭嘴（调用处的 catch 拦不住那条红字）', () => {
  assert.match(API_CLIENT, /error\.config\?\.silent/)
  // 五个会 403 的接口都标了 silent
  for (const [src, pat] of [
    [API_BANK, /plan-vouchers`[^)]*silent: true/s],
    [API_VOUCHER, /neighbour`[^)]*silent: true/s],
    [API_VOUCHER, /vouchers\/next`[^)]*silent: true/s],
    [API_VOUCHER, /insert-preview`[^)]*silent: true/s],
    [API_VOUCHER, /period-check`[^)]*silent: true/s],
  ]) {
    assert.match(src, pat, '这个接口没标 silent —— 403 的红字会照旧弹出来')
  }
})

test('真错照样要说：403 静默，500 与网络断了照常弹', () => {
  for (const [src, name] of [
    [VOUCHER_EDIT, 'loadNeighbour'],
    [FLOW, 'loadPlan'],
  ]) {
    const block = blockOf(src, name)
    assert.match(block, /status !== 403/, `${name}：不该把真错一起吞掉`)
  }
})
