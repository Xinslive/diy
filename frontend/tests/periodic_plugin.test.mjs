/**
 * 周期记账插件前端：科目选择与变量插入的判定逻辑。
 *
 * 插件源码在 ``plugins/`` 下，而 ``plugins/`` 是 gitignore 的：干净 checkout
 * 里没有这份源码。所以源码缺失时**跳过**，不是失败 —— 在本机全绿、在 CI 必红
 * 的用例等于没有用例。
 */
import { existsSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const PLUGIN_ENTRY = new URL('../../plugins/com.cwq.periodic/frontend/index.js', import.meta.url)
const plugin = existsSync(PLUGIN_ENTRY) ? await import(PLUGIN_ENTRY.href) : null

const SUBJECTS = [
  { code: '1001', name: '库存现金' },
  { code: '1002', name: '银行存款' },
  { code: '2202', name: '应付职工薪酬' },
  { code: '5401', name: '主营业务收入' },
]

function skip(t) {
  // t.skip() 只是**标记**跳过，不会中断函数体；所以这里必须自己返回 true，
  // 让调用方 `if (skip(t)) return` 真的停住 —— 否则源码缺失时用例会继续
  // 往下跑，在 null 上取值而报错。
  if (!plugin) {
    t.skip('本机没有周期记账插件源码')
    return true
  }
  return false
}

test('科目筛选：编码与名称都能搜到，大小写不敏感', t => {
  if (skip(t)) return
  const { matchSubjectList } = plugin
  assert.deepEqual(matchSubjectList(SUBJECTS, '1001').map(s => s.code), ['1001'])
  assert.deepEqual(matchSubjectList(SUBJECTS, '库存').map(s => s.code), ['1001'])
  assert.deepEqual(matchSubjectList(SUBJECTS, '100').map(s => s.code), ['1001', '1002'])
  // 搜不到就不该硬凑一个出来
  assert.deepEqual(matchSubjectList(SUBJECTS, '不存在'), [])
  // 空关键词列出全部
  assert.equal(matchSubjectList(SUBJECTS, '').length, 4)
})

test('科目筛选：搜不到也不截断，排在后面的科目照样选得上', t => {
  if (skip(t)) return
  const many = Array.from({ length: 300 }, (_item, i) => ({
    code: String(1000 + i), name: '科目' + i,
  }))
  const { matchSubjectList } = plugin
  assert.equal(matchSubjectList(many, '').length, 300)
  assert.equal(matchSubjectList(many, '1299').length, 1)
  assert.equal(matchSubjectList(many, '1299')[0].code, '1299')
})

test('完整编码、完整「编码 名称」都认得', t => {
  if (skip(t)) return
  const { exactSubjectCode } = plugin
  assert.equal(exactSubjectCode(SUBJECTS, '1001'), '1001')
  assert.equal(exactSubjectCode(SUBJECTS, '1001 库存现金'), '1001')
  assert.equal(exactSubjectCode(SUBJECTS, ' 1001 '), '1001')
  assert.equal(exactSubjectCode(SUBJECTS, '10011'), null)
  assert.equal(exactSubjectCode(SUBJECTS, ''), null)
})

test('回车选中：敲完整编码直接选它', t => {
  if (skip(t)) return
  const { chooseSubjectCode } = plugin
  // 用户报的场景：敲 1001 回车就选中，不必再点下拉
  assert.equal(chooseSubjectCode(SUBJECTS, '1001', 0), '1001')
  // 即使高亮在别处，完整编码优先 —— 那是用户明确敲出来的
  assert.equal(chooseSubjectCode(SUBJECTS, '1002', 3), '1002')
})

test('回车选中：没有完整编码时选高亮的那一条', t => {
  if (skip(t)) return
  const { chooseSubjectCode } = plugin
  assert.equal(chooseSubjectCode(SUBJECTS, '100', 0), '1001')
  assert.equal(chooseSubjectCode(SUBJECTS, '100', 1), '1002')
  // 高亮越界时落在最后一条，不越界
  assert.equal(chooseSubjectCode(SUBJECTS, '100', 99), '1002')
  assert.equal(chooseSubjectCode(SUBJECTS, '100', -3), '1001')
})

test('回车选中：一个都搜不到时什么都不选', t => {
  if (skip(t)) return
  const { chooseSubjectCode } = plugin
  assert.equal(chooseSubjectCode(SUBJECTS, 'zzz', 0), null)
})

test('变量插入：插在光标处，光标跟着往后挪', t => {
  if (skip(t)) return
  const { insertVariableText } = plugin
  const first = insertVariableText('', '本月区间', 0)
  assert.equal(first.text, '{本月区间}')
  assert.equal(first.caret, 6)

  const middle = insertVariableText('养老保险费', '上月区间', 2)
  assert.equal(middle.text, '养老{上月区间}保险费')
  assert.equal(middle.caret, 8)

  // 光标越界时落在末尾，不越界
  const beyond = insertVariableText('abc', '本月', 99)
  assert.equal(beyond.text, 'abc{本月}')
  const negative = insertVariableText('abc', '本月', -5)
  assert.equal(negative.text, '{本月}abc')
})

test('变量插入：多个变量依次插入不会互相吃掉', t => {
  if (skip(t)) return
  const { insertVariableText } = plugin
  let step = insertVariableText('', '本月区间', 0)
  step = insertVariableText(step.text, '上月区间', step.caret)
  assert.equal(step.text, '{本月区间}{上月区间}')
})

test('变量清单：插到选中的那条分录，按各条自己的光标位置插', t => {
  if (skip(t)) return
  const { variableInsertPlan } = plugin
  const entries = [{ summary: '养老保险费' }, { summary: '' }]

  // 清单只有一份，插到哪条由清单自己决定：选第 2 条就插第 2 条。
  const second = variableInsertPlan(entries, 1, '本月区间', {})
  assert.equal(second.index, 1)
  assert.equal(second.text, '{本月区间}')

  // 记过光标就插在光标处，光标位置按分录各记一份。
  const withCaret = variableInsertPlan(entries, 0, '本月区间', { 'entry:0': 2 })
  assert.equal(withCaret.text, '养老{本月区间}保险费')
  assert.equal(withCaret.caret, 8)
  // 第 2 条没有光标记录，不能借用第 1 条的。
  assert.equal(variableInsertPlan(entries, 1, '本月区间', { 'entry:0': 2 }).text, '{本月区间}')

  // 没记过光标：插到末尾，不是开头（插到开头看着像插错了位置）。
  assert.equal(variableInsertPlan(entries, 0, '本月区间', {}).text, '养老保险费{本月区间}')

  // 目标越界（那条分录被删了之类）就落到第 1 条，不越界。
  assert.equal(variableInsertPlan(entries, 9, '本月区间', {}).index, 0)

  // 没有分录就没什么可插。
  assert.equal(variableInsertPlan([], 0, '本月区间', {}), null)
})
