/**
 * 乐观锁契约（审计 §3.15）：客户端把它读到的那一版带回去。
 *
 * **为什么行锁之外还要这一层。**
 *
 * 行锁挡的是「**已记账**的凭证被并发改写」—— 那是 §3.15 里最严重的一条
 * （数字错了，但借贷平、试算平、科目余额、总账、「整理就绪」体检**全过**）。
 *
 * 但**未记账**的凭证没有那道闸：两个人同时改同一张凭证，
 * A 存完 B 再存 —— B 的那份覆盖 A 的，**A 的改动人间蒸发**，而账上看着还是平的。
 * 行锁在那一刻只会串行化两次保存，**不会告诉 B「你盖掉的是别人的东西」**。
 *
 * 这份契约就是那句话：服务端回 409（「已被他人改动，请刷新」），
 * 而不是让后保存的人默默赢。
 *
 * 被测的是**真实源码**：从 ``VoucherEdit.vue`` 里断言令牌的三件事 ——
 * 读到它、带回去、409 之后不再自动保存。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const EDIT = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

test('令牌从单张响应里读到', () => {
  assert.match(EDIT, /voucherVersion\.value\s*=\s*v\.version\s*\|\|\s*''/,
    'load() 必须把服务端给的 version 存下来 —— 不存就没东西可带回去')
  assert.match(EDIT, /const voucherVersion = ref\(''\)/,
    '必须有 voucherVersion 这个状态')
})

test('保存时把令牌带回去', () => {
  assert.match(EDIT, /version:\s*voucherVersion\.value\s*\|\|\s*null/,
    '保存时必须把 version 放进请求体')
  assert.match(EDIT, /if \(saved\.version\) voucherVersion\.value = saved\.version/,
    '保存成功后必须换成**服务端刚给的新令牌** —— 令牌是一次性的，' +
    '留着旧的下趟就会被自己判成「版本过期」')
})

test('409 之后不再自动保存', () => {
  assert.match(EDIT, /versionConflict\.value\s*=\s*true/,
    '409 必须把冲突标志置上')
  assert.match(EDIT, /if \(versionConflict\.value\) return false/,
    '冲突之后要掐掉自动保存 —— 不掐的话人每敲一下就重试一次，' +
    '每次都 409、每次都弹红字，结果一条也没存上')
  assert.match(EDIT, /e\?\.response\?\.status\s*===\s*409/,
    '必须认得 409 这个状态码')
  assert.match(EDIT, /已被他人改动/,
    '必须告诉人「接下来怎么办」，而不只是「为什么失败」')
})
