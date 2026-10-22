/**
 * 「用户与授权」页的入参约束（src/views/system/UserAdmin.vue，**线上那份源码**）。
 *
 * 为什么要有这条：后端的口令与姓名长度口径已经统一了
 * （backend/app/api/system.py：口令 ≥ 8 位且两类字符、姓名 ≤ 64 与列宽一致），
 * 而**前端是最后一道能给出人话的地方** —— 超长时后端回的是 422，
 * 原样弹给管理员并不能告诉她该改成什么样。
 *
 * 所以这里钉住的是**两端口径对得上**这件事：三个常量必须与后端一致
 * （用户名 64、姓名 64、口令 8），输入框必须真的挂着 maxlength，
 * 且**重置口令那一条也必须用同一套判据**（过去建号查 6 位、重置查 6 位、
 * 后端却是两套不同的下限 —— 「同一个属性三套下限」正是审计 P2-1 说的）。
 *
 * 只看源码就下断言：这些事说的都是「界面上有没有这个东西」，
 * 真跑起来点一遍只能证明点得动，证明不了它没被换成一个别的条件。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/system/UserAdmin.vue', import.meta.url), 'utf8')

/** 从源码里取一个常量的值。 */
const constValue = (name) => {
  const m = VIEW.match(new RegExp(`const ${name} = (\\d+)`))
  assert.ok(m, `源码里没有 ${name} 这个常量`)
  return Number(m[1])
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 入参约束与后端对齐 ==')

t('用户名长度上限是 64（与 sys_user.username 的列宽一致）', () => {
  assert.equal(constValue('USERNAME_MAX'), 64)
})

t('姓名长度上限是 64（与 sys_user.real_name 的列宽一致）', () => {
  assert.equal(constValue('REAL_NAME_MAX'), 64)
})

t('口令下限是 8 位（与后端 PASSWORD_MIN_LENGTH 一致）', () => {
  assert.equal(constValue('PASSWORD_MIN'), 8)
})

t('两个输入框都挂着 maxlength —— 不然超长要到提交时才发现', () => {
  assert.match(VIEW, /v-model="userForm\.username"[^>]*:maxlength="USERNAME_MAX"/)
  assert.match(VIEW, /v-model="userForm\.real_name"[^>]*:maxlength="REAL_NAME_MAX"/)
})

t('建号与重置口令用的是同一个判据', () => {
  // 建号
  assert.match(VIEW, /function saveUser\(\)[\s\S]*?passwordProblem\(userForm\.password\)/)
  // 重置
  assert.match(VIEW, /function savePassword\(\)[\s\S]*?passwordProblem\(newPassword\.value\)/)
})

t('用户名形状校验在（换行/空格会被拒）', () => {
  assert.match(VIEW, /function usernameProblem\(username\)/)
  assert.match(VIEW, /USERNAME_BAD\.test\(username\)/)
})

t('不再有「至少 6 位」这种旧文案 —— 它与后端已经不是一个口径了', () => {
  assert.doesNotMatch(VIEW, /至少 6 位/)
})

t('删除失败时把后端的话原样透出来（最后一个管理员那句自带处置动作）', () => {
  assert.match(VIEW, /async function remove\(row\)[\s\S]*?e\?\.response\?\.data\?\.detail/)
})

console.log('')
console.log('== 账套授权一列只写账套名，不写单位名 ==')
//
// 用户 2026-10-22 批注：「不显示单位名」。单位名是建账套时填的抬头，
// 授权表里认的是**账套** —— 全站顶上的账套选择框也早就只写账套名。

t('账套标签不再拼公司名', () => {
  const code = VIEW.replace(/<!--[\s\S]*?-->/g, '')   // 注释里可以提这件事
  assert.ok(!/company_name/.test(code), '账套标签里还在拼单位名')
})

t('账套标签只剩账套名与角色', () => {
  assert.match(VIEW, /bookLabel\(b\.book_id\)\s*\}\}\s*·\s*\{\{ ROLE_CN\[b\.role\] \}\}/)
})

t('认不出账套时退回 id —— 那才是唯一能区分的东西', () => {
  assert.match(VIEW, /return b \? b\.name : `#\$\{id\}`/)
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
if (fail) process.exit(1)
