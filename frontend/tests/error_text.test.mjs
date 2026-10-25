/**
 * ``utils/errorText.js`` 的**行为级**用例。
 *
 * 为什么单独一个文件：这个 helper 承接着全站「后端那句话怎么显示给人看」
 * 的**唯一一份实现**（§4.5）。而它的存在理由是一条**具体**的缺陷：
 * 「近期二次验证」闸门回的 403 是**结构化**的（``{code, message}``），
 * 直接把 ``detail`` 丢给 ElMessage 会渲染成 ``[object Object]``。
 *
 * 所以这里钉的不是「源码里写了什么」，而是**真的拿一个错误进去，
 * 断言出来的话与后端那句逐字相同** —— 源码形态的断言钉不住这件事
 * （它可以在实现变形之后照样绿）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { errorText } = await vite.ssrLoadModule('/src/utils/errorText.js')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

/** 造一个 axios 形状的错误：``{response: {data: {detail}}}``。 */
function err(detail) {
  return { response: { data: { detail } } }
}

console.log('== 纯字符串 detail：原样透出 ==')

t('字符串 detail **逐字**返回', () => {
  const msg = '只有系统管理员可以访问'
  assert.equal(errorText(err(msg)), msg)
})

t('空字符串不算数 → 走兜底', () => {
  assert.equal(errorText(err(''), '操作失败'), '操作失败')
})

console.log('== 结构化 detail：取 message，不吐 [object Object] ==')

t('结构化 detail 取 message', () => {
  const out = errorText(err({ code: 'MFA_REAUTH_REQUIRED', message: '该操作需要近期完成邮箱二次验证' }))
  assert.equal(out, '该操作需要近期完成邮箱二次验证')
})

t('结构化 detail **绝不会**渲染成 [object Object]', () => {
  const out = errorText(err({ code: 'MFA_REAUTH_REQUIRED', message: '需要二次验证' }))
  assert.ok(!out.includes('[object'), '把对象直接甩到界面上了：' + out)
})

t('结构化 detail 只有 code 时退回 code（总比空着强）', () => {
  assert.equal(errorText(err({ code: 'SOME_CODE' })), 'SOME_CODE')
})

t('结构化 detail 的 msg 字段也认', () => {
  assert.equal(errorText(err({ msg: '出了点问题' })), '出了点问题')
})

console.log('== 数组 detail（FastAPI 的 422） ==')

t('数组 detail 取每条的 msg，用「；」连起来', () => {
  const out = errorText(err([{ msg: '用户名不能为空' }, { msg: '密码太短' }]))
  assert.equal(out, '用户名不能为空；密码太短')
})

t('数组里没有 msg 的条目被跳过', () => {
  const out = errorText(err([{ msg: '第一处' }, { loc: ['body', 'x'] }]))
  assert.equal(out, '第一处')
})

console.log('== 什么都没有的时候 ==')

t('连 response 都没有 → 兜底文案', () => {
  assert.equal(errorText(undefined, '加载失败'), '加载失败')
  assert.equal(errorText({}, '加载失败'), '加载失败')
})

t('有 error.message 时用它（axios 的网络错误）', () => {
  assert.equal(errorText({ message: 'Network Error' }, '加载失败'), 'Network Error')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
