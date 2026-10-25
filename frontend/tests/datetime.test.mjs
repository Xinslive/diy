import test from 'node:test'
import assert from 'node:assert/strict'
import { formatDate, formatDateTime, todayLocal } from '../src/utils/datetime.js'

test('接口回来的 UTC 时间按北京时间显示（这正是操作日志早前少 8 小时的原因）', () => {
  assert.equal(formatDateTime('2026-10-18T14:28:52.123456+00:00'), '2026-10-18 22:28:52')
  assert.equal(formatDateTime('2026-10-18T14:28:52Z'), '2026-10-18 22:28:52')
  // 没有时区标记的一律按 UTC 解读：后端就是这么存的，不能猜成当地时间。
  assert.equal(formatDateTime('2026-10-18T14:28:52'), '2026-10-18 22:28:52')
})

test('已经带 +08:00 的时间不会被重复加一次', () => {
  assert.equal(formatDateTime('2026-10-18T22:28:52+08:00'), '2026-10-18 22:28:52')
  assert.equal(formatDateTime('2026-10-18T22:28:52+0800'), '2026-10-18 22:28:52')
})

test('跨日、跨月、跨年按北京时间各自翻页', () => {
  // UTC 16:30 已经是北京次日 00:30
  assert.equal(formatDateTime('2026-10-18T16:30:00+00:00'), '2026-10-19 00:30:00')
  assert.equal(formatDateTime('2026-10-31T16:00:00+00:00'), '2026-11-01 00:00:00')
  assert.equal(formatDateTime('2026-12-31T16:30:00+00:00'), '2027-01-01 00:30:00')
  // 北京时间 09:17 仍当天，UTC 是前一天的 01:17
  assert.equal(formatDateTime('2026-10-18T01:17:00+00:00'), '2026-10-18 09:17:00')
})

test('负偏移时区同样按绝对时刻换算', () => {
  assert.equal(formatDateTime('2026-10-18T06:28:52-08:00'), '2026-10-18 22:28:52')
})

test('空值与认不出来的串都不该显示成空白', () => {
  assert.equal(formatDateTime(null), '')
  assert.equal(formatDateTime(undefined), '')
  assert.equal(formatDateTime(''), '')
  // 解析不了就把原样给出去，让人能看出这里坏了，而不是默默显示空白。
  assert.equal(formatDateTime('昨天'), '昨天')
})

test('分钟精度与日期部分', () => {
  assert.equal(formatDateTime('2026-10-18T14:28:52+00:00', { seconds: false }), '2026-10-18 22:28')
  assert.equal(formatDate('2026-10-18T16:30:00+00:00'), '2026-10-19')
  assert.equal(formatDate(''), '')
})

test('不该随浏览器时区改变结果', () => {
  const original = process.env.TZ
  try {
    for (const zone of ['UTC', 'America/New_York', 'Asia/Shanghai', 'Pacific/Kiritimati']) {
      process.env.TZ = zone
      assert.equal(formatDateTime('2026-10-18T14:28:52+00:00'), '2026-10-18 22:28:52', `TZ=${zone}`)
    }
  } finally {
    if (original === undefined) delete process.env.TZ
    else process.env.TZ = original
  }
})

test('todayLocal 取本地日历日期，不取 UTC 日期', () => {
  // 北京时间 2026-10-25 01:00 = UTC 2026-10-24 17:00。
  // 用 toISOString() 会拿到 10-24 —— 凭证日期落错一天就是落错期间。
  const original = process.env.TZ
  try {
    process.env.TZ = 'UTC'
    assert.equal(todayLocal(new Date('2026-10-24T17:00:00Z')), '2026-10-24')

    process.env.TZ = 'Asia/Shanghai'
    // 同一个瞬间，北京这边已经是 10-25 了。
    assert.equal(todayLocal(new Date('2026-10-24T17:00:00Z')), '2026-10-25')

    process.env.TZ = 'America/New_York'
    assert.equal(todayLocal(new Date('2026-10-24T17:00:00Z')), '2026-10-24')
  } finally {
    if (original === undefined) delete process.env.TZ
    else process.env.TZ = original
  }
})

test('todayLocal 补零成 YYYY-MM-DD', () => {
  assert.equal(todayLocal(new Date(2026, 0, 5)), '2026-01-05')
  assert.equal(todayLocal(new Date(2026, 10, 25)), '2026-11-25')
})
