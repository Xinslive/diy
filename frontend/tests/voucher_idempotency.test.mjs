/**
 * 新建凭证的幂等：请求发出去了、响应丢了，**不能**变成两张凭证（审计 §3.28）。
 *
 * 失败场景（原文）：
 *
 *   新凭证 POST 发出 → 服务端已入库 → 响应在网络上丢了（30s 超时）
 *   → 客户端以为没存上 → 再敲一个字 → 自动保存又发一次 POST
 *   → 库里两张内容几乎相同的凭证
 *
 * **这套机制有两半，缺一不可**：
 *
 *   · **服务端**：按 ``Idempotency-Key`` 去重，同一把键只建一张凭证，
 *     重复的请求回放**上一次那张**（HTTP 200 + ``Idempotent-Replay: true``）。
 *   · **客户端**：收到回放时，**补一次 PUT** 把当前内容收敛上去 ——
 *     因为回放回来的是**上一次那一版**的内容，而用户此刻可能又敲了几个字。
 *     不补这一步，界面显示的内容与库里不一致，而且**不报错**。
 *
 * 这里测的是**前端这一半**（服务端那一半在
 * ``backend/tests/test_audit_fix_voucher_idempotency.py``）。
 *
 * 被测的是**真实源码**：判定函数直接从 ``utils/idempotency.js`` 引进来跑，
 * 流程部分从 ``VoucherEdit.vue`` / ``api/voucher.js`` 的源码里抠出来断言 ——
 * 抄一份等价实现只能证明抄的那份是对的。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { isReplay } from '../src/utils/idempotency.js'

const EDIT = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/voucher.js', import.meta.url), 'utf8')

test('回放的判定认得两种响应头形态', () => {
  // axios 在浏览器里给的是 AxiosHeaders（有 .get()）
  assert.equal(isReplay({ get: (k) => (k === 'idempotent-replay' ? 'true' : null) }), true)
  // 别的适配器给的是普通对象
  assert.equal(isReplay({ 'idempotent-replay': 'true' }), true)
  assert.equal(isReplay({ 'Idempotent-Replay': 'true' }), true)
  // 没有这个头 = 正常新建
  assert.equal(isReplay({}), false)
  assert.equal(isReplay(null), false)
  assert.equal(isReplay(undefined), false)
  // 大小写与空白都不该让它变成「不是回放」
  assert.equal(isReplay({ 'idempotent-replay': 'TRUE' }), true)
  assert.equal(isReplay({ 'idempotent-replay': ' True ' }), true)
  // 别的值不是回放
  assert.equal(isReplay({ 'idempotent-replay': 'false' }), false)
  assert.equal(isReplay({ 'idempotent-replay': '1' }), false)
})

test('新建凭证带上了幂等键', () => {
  assert.match(API, /'Idempotency-Key'\s*:\s*idempotencyKey/,
    'api/voucher.js 的 createVoucher 必须带上 Idempotency-Key 头')
  assert.match(API, /replayed:\s*isReplay\(r\.headers\)/,
    'createVoucher 必须把「这是回放」这件事告诉调用方')
})

test('回放之后要补一次 PUT 把当前内容收敛上去', () => {
  assert.match(EDIT, /created\.replayed/,
    'VoucherEdit.vue 必须认得回放')
  assert.match(EDIT, /replayed\s*\n?\s*\?\s*await api\.updateVoucher\(/,
    '回放之后必须补一次 updateVoucher —— 不补的话用户新敲的字就白敲了')
})

test('一把键只管一张新凭证，换凭证就换键', () => {
  // 键在第一次创建时生成，之后一直复用（直到这张落库）
  assert.match(EDIT, /if \(wasNew && !createKey\.value\) createKey\.value = newIdempotencyKey\(\)/,
    '幂等键必须在第一次创建时生成一次，之后复用同一把')
  // 换一张凭证就换一把
  assert.match(EDIT, /createKey\.value = ''/,
    '重置表单时必须把幂等键清掉 —— 不清的话新凭证会带着上一张的键')
})
