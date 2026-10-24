/**
 * 发票预览页的性能与交互（src/views/voucher/InvoiceImport.vue，**线上那份源码**）：
 *
 *  ① 科目下拉必须 persistent=false；
 *  ② 有问题的发票才给「重新解析」按钮；
 *  ③ 「这张」那个残骸标签要去掉。
 *
 * ① 是**性能**上的事，不是审美：Element Plus 的 el-select 默认
 * persistent=true —— 那一大堆 el-option 组件在下拉**关着的时候就已经渲染
 * 好了**（只是用 v-show 藏起来）。批次全部展开时，几十张发票各带好几个科目
 * 下拉，每个下拉都要摆一遍整张科目表：
 *
 *     选项元素 15635 → 141，DOM 节点 47900 → 14372，JS 堆 441MB → 159MB
 *
 * 这一条没人会主动想起来加，所以拿源码钉住。
 *
 * ②③ 只看源码就够：它们说的都是「那一栏上有没有这个东西」。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 科目下拉不要提前渲染选项 ==')

t('每一个带科目选项的下拉都关了 persistent', () => {
  // 带 <option v-for="s in subjects"> 的下拉，一个都不能漏
  const selects = VIEW.match(/<el-select[^>]*>[\s\S]*?<\/el-select>/g) || []
  const subjectSelects = selects.filter((s) => s.includes('v-for="s in subjects"'))
  assert.ok(subjectSelects.length >= 3, `只认出 ${subjectSelects.length} 个科目下拉`)
  for (const s of subjectSelects) {
    assert.ok(s.includes(':persistent="false"'),
      '有个科目下拉没关 persistent —— 选项会在下拉关着时就全部渲染出来')
  }
})

t('配置区那几个科目下拉也关了 persistent', () => {
  // 自动制证配置那五个：一直摆在页面上，不关的话每次进页面都要渲染五遍科目表
  for (const key of ['output_tax_subject_id', 'input_credit_subject_id',
    'output_debit_subject_id', 'input_fallback_subject_id', 'output_fallback_subject_id']) {
    const m = VIEW.match(new RegExp(`<el-select v-model="config\\.${key}"[^>]*>`))
    assert.ok(m, `配置项 ${key} 的下拉没找到`)
    assert.ok(m[0].includes(':persistent="false"'), `配置项 ${key} 的下拉没关 persistent`)
  }
})

console.log('== 有问题的发票给「重新解析」 ==')

t('重新解析按钮只在有错误的那张上出现', () => {
  assert.ok(VIEW.includes('v-if="row.errors?.length"'), '重新解析按钮没有按错误数显示')
  assert.ok(VIEW.includes('>重新解析<'), '找不到「重新解析」按钮')
})

t('重新解析按钮挨着「忽略这张」', () => {
  const ignored = VIEW.indexOf('>忽略这张<')
  const reparse = VIEW.indexOf('>重新解析<')
  assert.ok(ignored > 0 && reparse > 0, '两个按钮都要在')
  assert.ok(reparse > ignored, '「重新解析」应该在「忽略这张」右边')
  assert.ok(reparse - ignored < 900, '两个按钮离得远，不像一组')
})

console.log('== 「这张」那个残骸标签去掉了 ==')

t('不再有 label="这张"', () => {
  assert.doesNotMatch(VIEW, /label="这张"/)
})

t('按钮组自己占位，不跟别项挤在一起', () => {
  assert.ok(VIEW.includes('class="row-actions"'), '按钮组没有单独的类名')
  assert.ok(VIEW.includes('.row-actions'), '按钮组没有样式')
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
