/**
 * 科目弹窗的共用提交校验（`composables/useSubjectForm.js`）。
 *
 * 抽出来的原因：这两条校验以前在新增弹窗和编辑弹窗里各写一份，逐字相同。
 * 重复已经开始产生代价 —— 编辑那个补了 `catch`，新增那个没补，同一个失败
 * 在两个弹窗里是两种体验。
 *
 * 这里钉三件事：
 *   ① 两个弹窗的**空值校验**必须给出同一句话（漂移过一次就会再漂）；
 *   ② **只读账套**的文案两边不同，而且必须由调用方传 —— 这是「新增」和
 *      「编辑」的分界，藏在校验里就会有人抽着抽着又变成同一句；
 *   ③ 还有一个纯源码断言：两个弹窗都**必须真的调它**，而不是各留一份
 *      本地副本 —— 否则抽了个寂寞，下次照样漂。
 *
 * 注意 `bad !== true` 这个写法：校验通过返回 `true`（真值），
 * 不通过返回**文案**（也是真值）。调用点必须比 `!== true` 而不是 `!bad`，
 * 否则「校验通过」会被当成「校验失败」反过来弹一句「true」。
 * 第 4 条就是盯这个的。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const { validateSubjectForm } = await import(
  new URL('../src/composables/useSubjectForm.js', import.meta.url).href)

const writable = { canWrite: true }
const readonly = { canWrite: false }
const ok = { code: '1002', name: '银行存款' }

let pass = 0, fail = 0
const tests = []
const t = (name, fn) => tests.push([name, fn])

t('正常输入通过（返回 true，不是文案）', () => {
  assert.equal(validateSubjectForm(writable, ok, '只读'), true)
})

t('编码为空 → 编码那句', () => {
  assert.equal(validateSubjectForm(writable, { code: '  ', name: 'x' }, '只读'), '科目编码不能为空')
})

t('名称为空 → 名称那句', () => {
  assert.equal(validateSubjectForm(writable, { code: '1002', name: '' }, '只读'), '科目名称不能为空')
})

t('编码和名称都空 → 先报编码（顺序固定，别让人看到两套提示顺序）', () => {
  assert.equal(validateSubjectForm(writable, { code: '', name: '' }, '只读'), '科目编码不能为空')
})

t('只读账套 → 回调用方给的文案', () => {
  assert.equal(validateSubjectForm(readonly, ok, '不能新增科目'), '不能新增科目')
})

t('只读时不再往下报空值（权限优先于格式）', () => {
  assert.equal(validateSubjectForm(readonly, { code: '', name: '' }, '只读文案'), '只读文案')
})

t('表单是 undefined 也不炸（返回编码那句而不是抛 TypeError）', () => {
  assert.equal(validateSubjectForm(writable, undefined, '只读'), '科目编码不能为空')
})

t('store 是 undefined 视为只读（fail-closed，不放行）', () => {
  assert.equal(validateSubjectForm(undefined, ok, '不能写'), '不能写')
})

// ------------------------------------------------------------------
// 两个弹窗都必须在用这份共用校验
// ------------------------------------------------------------------

for (const [file, readonlyText] of [
  ['components/SubjectFormDialog.vue', '这个账套是只读的，不能新增科目'],
  ['components/SubjectEditDialog.vue', '这个账套是只读的，不能编辑科目'],
]) {
  const src = fs.readFileSync(path.join(ROOT, 'src', file), 'utf8')

  t(`${file} 导入了共用校验`, () => {
    assert.match(src, /import\s*\{[^}]*validateSubjectForm[^}]*\}\s*from\s*['"]@\/composables\/useSubjectForm['"]/,
      '没导入共用校验 —— 两边又要各写一份了')
  })

  t(`${file} 用的是 bad !== true 而不是 !bad`, () => {
    assert.match(src, /if\s*\(bad\s*!==\s*true\)\s*return\s+ElMessage\.warning\(bad\)/,
      '校验通过时返回的是 true，`!bad` 会把「通过」当成「失败」弹一句 true')
  })

  t(`${file} 传了自己的只读文案`, () => {
    assert.ok(src.includes(`'${readonlyText}'`),
      `只读文案应该是「${readonlyText}」——新增和编辑本来就是两回事`)
  })
  t(`${file} 不再自己写一遍空值校验`, () => {
    assert.ok(!/科目编码不能为空|科目名称不能为空/.test(src),
      '本地还留着一份空值校验，会和共用那份漂')
  })
}

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
