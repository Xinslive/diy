/**
 * 「用户与授权」页的入参约束（src/views/system/UserAdmin.vue，**线上那份源码**）。
 *
 * 为什么要有这条：后端的密码与姓名长度口径已经统一了
 * （backend/app/core/security.py：密码 ≥ 6 位、姓名 ≤ 64 与列宽一致），
 * 而**前端是最后一道能给出人话的地方** —— 超长时后端回的是 422，
 * 原样弹给管理员并不能告诉她该改成什么样。
 *
 * 所以这里钉住的是**两端口径对得上**这件事：三个常量必须与后端一致
 * （账号 64、姓名 64、密码 6），输入框必须真的挂着 maxlength，
 * 且**重置密码那一条也必须用同一套判据**（过去建号查 6 位、重置查 6 位、
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

t('账号长度上限是 64（与 sys_user.username 的列宽一致）', () => {
  assert.equal(constValue('USERNAME_MAX'), 64)
})

t('姓名长度上限是 64（与 sys_user.real_name 的列宽一致）', () => {
  assert.equal(constValue('REAL_NAME_MAX'), 64)
})

t('密码下限是 6 位（与后端 PASSWORD_MIN_LENGTH 一致）', () => {
  assert.equal(constValue('PASSWORD_MIN'), 6)
})

t('两个输入框都挂着 maxlength —— 不然超长要到提交时才发现', () => {
  assert.match(VIEW, /v-model="userForm\.username"[^>]*:maxlength="USERNAME_MAX"/)
  assert.match(VIEW, /v-model="userForm\.real_name"[^>]*:maxlength="REAL_NAME_MAX"/)
})

t('建号与重置密码用的是同一个判据', () => {
  // 建号
  assert.match(VIEW, /function saveUser\(\)[\s\S]*?passwordProblem\(userForm\.password\)/)
  // 重置
  assert.match(VIEW, /function savePassword\(\)[\s\S]*?passwordProblem\(newPassword\.value\)/)
})

t('用户名形状校验在（换行/空格会被拒）', () => {
  assert.match(VIEW, /function usernameProblem\(username\)/)
  assert.match(VIEW, /USERNAME_BAD\.test\(username\)/)
})

t('密码位数不写死在文案里 —— 提示一律跟着常量走', () => {
  // 写死的「至少 8 位」这类文案与真正的下限**分处两地**，改一次就要改两处，
  // 而漏改的那一处不会报错：它只是在界面上悄悄变成一句假话。
  // 下限从 8 改成 6 之后，这条钉的就是「别再写死数字」。
  assert.doesNotMatch(VIEW, /至少 \d+ 位/)
  assert.match(VIEW, /至少 \$\{PASSWORD_MIN\} 位/)
})

t('删除失败时把后端的话原样透出来（最后一个管理员那句自带处置动作）', () => {
  // **这里断言的是「用了共享取值的那一份」**，不是「直接读 data.detail」。
  //
  // 直接读 ``e.response.data.detail`` 在 detail 是**结构化**时会渲染成
  // ``[object Object]`` —— 而「近期二次验证」闸门回的 403 恰恰是结构化的
  // （{code, message}）。所以取值统一走 utils/errorText.js（§4.5：
  // 同一个量只允许一个权威实现）。
  //
  // 「原样透出」这条**意图**不再由这里的源码形态守着，改由
  // tests/error_text.test.mjs **行为级**钉住（真的拿一个 detail 进去，
  // 断言出来的话与后端那句逐字相同）。
  assert.match(VIEW, /async function remove\(row\)[\s\S]*?errorText\(e,/)
  assert.doesNotMatch(VIEW, /async function remove\(row\)[\s\S]*?e\?\.response\?\.data\?\.detail/)
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
