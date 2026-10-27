/**
 * 民宿报销插件前端：规则标识、账套覆盖与科目存在性。
 *
 * 插件源码在 ``plugins/`` 下，而 ``plugins/`` 是 gitignore 的：干净 checkout
 * 里没有这份源码。所以源码缺失时**跳过**，不是失败 —— 在本机全绿、在 CI 必红
 * 的用例等于没有用例。
 */
import { existsSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const PLUGIN_ENTRY = new URL('../../plugins/com.cwq.reimburse/frontend/index.js', import.meta.url)
const plugin = existsSync(PLUGIN_ENTRY) ? await import(PLUGIN_ENTRY.href) : null

function skip(t) {
  // t.skip() 只是**标记**跳过，不会中断函数体；所以这里必须自己返回 true，
  // 让调用方 `if (skip(t)) return` 真的停住。
  if (!plugin) {
    t.skip('本机没有民宿报销插件源码')
    return true
  }
  return false
}

const RULE = { all: ['医', '社保'], any: [], not: [], code: '221104' }

test('规则标识：关键词组合，改了关键词就是另一条规则', t => {
  if (skip(t)) return
  const { ruleKey } = plugin
  assert.equal(ruleKey(RULE), '医+社保||')
  assert.equal(ruleKey({ all: ['社保'], any: [], not: [] }), '社保||')
  // 关键词换了，覆盖就不再命中这条规则 —— 旧覆盖自然作废
  assert.notEqual(ruleKey(RULE), ruleKey({ all: ['社保'], any: [], not: [] }))
})

test('规则标识：全角、空格、大小写都归一化，跟后端一致', t => {
  if (skip(t)) return
  const { ruleKey, normTerm } = plugin
  assert.equal(normTerm('　医　'), '医')
  assert.equal(normTerm('ＡＢＣ'), 'abc')
  assert.equal(normTerm(' 社保 '), '社保')
  // 后端存回来的规则已经是归一化过的，再算一次标识不能变
  assert.equal(ruleKey({ all: ['医+社保'], any: [], not: [] }), ruleKey(RULE))
})

test('生效科目：账套覆盖优先，其次共用规则', t => {
  if (skip(t)) return
  const { effectiveCode, ruleKey } = plugin
  assert.equal(effectiveCode(RULE, null), '221104')
  assert.equal(effectiveCode(RULE, {}), '221104')
  const override = { subject_codes: { [ruleKey(RULE)]: '56021202' } }
  assert.equal(effectiveCode(RULE, override), '56021202')
  // 覆盖的是别的规则时不该串到这条规则上
  const other = { subject_codes: { '社保||': '56021205' } }
  assert.equal(effectiveCode(RULE, other), '221104')
})

test('写入与清除覆盖：清空即回到共用规则', t => {
  if (skip(t)) return
  const { withOverrideCode, ruleKey } = plugin
  const key = ruleKey(RULE)
  const written = withOverrideCode(null, key, '56021202')
  assert.deepEqual(written.subject_codes, { [key]: '56021202' })
  const cleared = withOverrideCode(written, key, '')
  assert.equal(cleared.subject_codes, undefined)
  // 原对象不被改动：Vue 的响应式靠这个，别就地改出来
  assert.deepEqual(written.subject_codes, { [key]: '56021202' })
})

test('本账套没有的科目：共用规则里的编码未必每本账都一样', t => {
  if (skip(t)) return
  const { missingSubjectCodes } = plugin
  const chart = [{ code: '1001', name: '库存现金' }, { code: '221104', name: '应付社会保险费' }]
  assert.deepEqual(missingSubjectCodes(chart, ['1001', '221104']), [])
  assert.deepEqual(missingSubjectCodes(chart, ['221104', '56021202']), ['56021202'])
  // 空编码不算缺失
  assert.deepEqual(missingSubjectCodes(chart, ['']), [])
})
