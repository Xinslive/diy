/**
 * 个人资料页（改自己的姓名、改自己的密码、管自己的 MCP 密钥）的前端一半。
 *
 * 这一页的门面很短，但**每一处都有个具体的错法**，所以逐条钉住：
 *
 *   1. **入口在账号菜单里**，不在侧栏 —— 它不属于任何一个模块。放在侧栏里
 *      会让「这一页属于哪个模块」变得说不清。
 *   2. **标签页刷新一次之后还在**。标签的 key 是从菜单派生的，而这一页
 *      不在菜单里 —— 不单独登记的话，F5 之后标签就没了（页面还开着，
 *      「关掉这一页」的把手没了）。
 *   3. **改完密码必须清本地会话并回登录页**：服务端把该账号所有在用令牌
 *      都作废了（含这一次），留着令牌不管的话，下一个请求撞 401 才会
 *      被动发现 —— 那时人已经站在登录页上，却不知道自己为什么被踢出去。
 *   4. **改完姓名要写回 store**：顶栏那个名字是 store 里的副本，不写回
 *      就是「明明改了，界面没动」。
 *   5. **MCP 密钥的明文必须显示出来**：它只在签发那一次响应里回来，之后
 *      谁也拿不回来 —— 只弹一句「生成成功」等于生成了一枚谁也用不了的密钥。
 *
 * 判定逻辑尽量跑**真实源码**（vite 的 ssrLoadModule），不复制逻辑 ——
 * 复制出来的等价物错了测试照样是绿的。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createPinia, setActivePinia } from 'pinia'
import { source, storage } from './plugin_test_support.mjs'

storage()
const TABS_KEY = 'cwq_tabs'
const setUser = (id) => localStorage.setItem('cwq_user', JSON.stringify({ id, username: `u${id}` }))

const { useTabsStore } = await source('/src/stores/tabs.js')

const read = (p) => fs.readFileSync(new URL(p, import.meta.url), 'utf8')
const ROUTER = read('../src/router/index.js')
const LAYOUT = read('../src/layouts/MainLayout.vue')
const VIEW = read('../src/views/ProfileView.vue')
const API = read('../src/api/auth.js')
const MCP_API = read('../src/api/mcp.js')

/**
 * 去掉注释，只留**会被渲染/执行**的那部分。
 *
 * 这一页的注释密度很高（到处在解释「为什么」），而断言「界面上没有某个词」
 * 时必须先剥掉注释 —— 注释里写着「管理面那个『吊销』」不等于界面上有它。
 * 不剥的话，下面那条断言会被**自己写的说明**绊倒。
 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^\s*\/\/.*$/gm, '')

/** 界面上的字（剥掉注释）与接口层的字。 */
const SHOWN = stripComments(VIEW)
const MCP_CODE = stripComments(MCP_API)

/** 重新载入页面：新建一个 pinia，store 才会真去读一次存储 */
const reload = () => { setActivePinia(createPinia()); return useTabsStore() }

test('路由存在，标题是「个人资料」', () => {
  assert.match(ROUTER, /name: 'profile'/, '没有 /profile 这条路由')
  assert.match(ROUTER, /path: 'profile'[^\n]*meta: \{ title: '个人资料' \}/,
    '个人资料页的标题不是「个人资料」—— 标签栏上会写着别的东西')
})

test('个人资料页不要求有账套（管理员自己未必有账套）', () => {
  const line = ROUTER.match(/name: 'profile'[^\n]*/)[0]
  assert.doesNotMatch(line, /requiresBook/,
    '标了 requiresBook —— 没有账套的人（包括管理员）就进不去这一页')
})

test('个人资料页不要求管理员（人人可改自己的）', () => {
  const line = ROUTER.match(/name: 'profile'[^\n]*/)[0]
  assert.doesNotMatch(line, /adminOnly/,
    '标了 adminOnly —— 普通用户改不了自己的名字，这页就白做了')
})

test('入口在右上角的账号菜单里，且排在「退出登录」上面', () => {
  assert.match(LAYOUT, /<el-dropdown-item command="profile">个人资料<\/el-dropdown-item>/,
    '账号菜单里没有「个人资料」这一项')
  const profileAt = LAYOUT.indexOf('command="profile"')
  const logoutAt = LAYOUT.indexOf('command="logout"')
  assert.ok(profileAt > 0 && logoutAt > profileAt,
    '「个人资料」要排在「退出登录」上面 —— 那是对「我要走了」的直觉顺序')
})

test('点账号菜单的「个人资料」是跳到 profile 路由，不是手写路径', () => {
  assert.match(LAYOUT, /router\.push\(\{ name: 'profile' \}\)/,
    '跳的不是路由名 —— 路由改名时这里会变成 404')
})

test('个人资料页不在侧栏里（它不属于任何一个模块）', () => {
  const nav = read('../src/router/nav.js')
  assert.doesNotMatch(nav, /name: 'profile'/,
    '「个人资料」进了菜单 —— 它会挤掉一个侧栏位置，而它本来就不属于任何模块')
})

test('开一个个人资料标签，刷新一次之后它还在', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  setActivePinia(createPinia())
  const s = useTabsStore()
  // 路由对象照真实的样子给：**带 meta**。标签标题是从路由 meta 借的
  // （``this.titles[key] || route.meta?.title``）—— 这一页不在菜单里，
  // titles 里没有它，标题只能从 meta 借。
  s.open({ name: 'profile', path: '/profile', fullPath: '/profile',
           meta: { title: '个人资料' } })
  assert.equal(s.opened.length, 1)
  assert.equal(s.opened[0].title, '个人资料', '标签标题没借到 —— 标签栏上会写着 profile')

  // 重新载入（换 pinia = 重新读一次 localStorage）
  const again = reload()
  assert.equal(again.opened.length, 1,
    '刷新之后个人资料的标签没了 —— 页面还开着，但「关掉这一页」的把手没了')
  assert.equal(again.opened[0].key, 'profile')
  assert.equal(again.opened[0].title, '个人资料',
    '刷新之后标签标题空了 —— 标题是从路由 meta 借的，借不到就只剩 key')
})

test('菜单里没有的 key 仍然会被丢掉（这条别被 EXTRA_KEYS 带歪）', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  setActivePinia(createPinia())
  const s = useTabsStore()
  s.open({ name: 'profile', path: '/profile', fullPath: '/profile',
           meta: { title: '个人资料' } })
  s.open({ name: '早就不存在的页面', path: '/nope', fullPath: '/nope' })
  const again = reload()
  assert.equal(again.opened.length, 1, '认不出的 key 没有被丢掉 —— 菜单改名的标签会赖着不走')
  assert.equal(again.opened[0].key, 'profile')
})

test('这一页不摆说明条（页头、字段提示、底部提示一律没有）', () => {
  // 用户 2026-10-23 定的口径：这一页开着就只该看见两张卡片。
  //
  // **为什么值得钉住**：说明条是最容易「又加回来」的东西 —— 下一个维护者
  // 觉得「这里应该说清楚」，就顺手加一句；加着加着，页面就被说明淹了。
  // 断言写在这里，才有人在想加之前先看见它。
  assert.doesNotMatch(VIEW, /class="muted form-hint"/,
    '字段下面又有提示条了 —— 要说的就做进 placeholder，或者在动作发生的那一刻说')
  assert.doesNotMatch(VIEW, /<el-alert/,
    '底部又有说明条了 —— 常驻的说明只会变成背景噪音')
  assert.doesNotMatch(VIEW, /class="page-head"/,
    '又摆起页头与那句说明了 —— 卡片自带标题，页面叫什么标签栏上已经写着')
  assert.doesNotMatch(VIEW, /class="hint"/,
    '卡片页眉又有提示了 —— 卡片自己带着标题就够')
})

test('改姓名调的是 PATCH /auth/me', () => {
  assert.match(API, /http\.patch\('\/auth\/me'/,
    '改姓名走的不是 PATCH /auth/me')
  assert.match(VIEW, /updateProfile\(\{ real_name: name \}\)/,
    '页面没有把姓名发给后端')
})

test('改密码调的是 POST /auth/me/password，且带上了当前密码', () => {
  assert.match(API, /http\.post\('\/auth\/me\/password'/, '改密码走的接口不对')
  assert.match(VIEW, /current_password: pwd\.current_password/,
    '当前密码没发过去 —— 服务端验的就是它')
  assert.match(VIEW, /new_password: pwd\.new_password/, '新密码没发过去')
})

test('改完密码要清本地会话并回登录页', () => {
  assert.match(VIEW, /store\.logout\(\{ remote: false \}\)/,
    '改完密码没有清本地令牌 —— 令牌已经作废了，留着只会让人撞 401')
  assert.match(VIEW, /router\.push\('\/login'\)/, '改完密码没有回登录页')
})

test('改完密码**不**再发一条登出留痕（令牌已作废，那条只会 401）', () => {
  assert.doesNotMatch(VIEW, /authApi\.logout\(/,
    '发了一条登出留痕 —— 令牌此刻已经作废，那条请求注定失败')
})

test('改完姓名要写回 store（顶栏那个名字就是它）', () => {
  assert.match(VIEW, /store\.applyProfile\(/,
    '改完姓名没写回 store —— 顶栏要等下一次刷新才变，而人刚刚才改的')
  const session = read('../src/stores/session.js')
  assert.match(session, /applyProfile\(data\)/, 'store 里没有 applyProfile 这个动作')
  assert.match(session, /localStorage\.setItem\('cwq_user'/,
    'applyProfile 没有写回 localStorage —— 刷新之后名字又变回去了')
})

// --------------------------------------------------------------------- MCP 密钥

console.log('== MCP 密钥：本人自己管自己那份 ==')

test('这一页有「MCP 密钥」这一块，以及「生成密钥」的入口', () => {
  assert.match(VIEW, /<span class="title">MCP 密钥<\/span>/,
    '个人资料页上没有「MCP 密钥」这一块 —— 本人没有地方管自己的密钥')
  assert.match(VIEW, /生成密钥/,
    '没有「生成密钥」的入口 —— 本人只能看着管理员签发')
})

test('列自己的密钥走 GET /mcp-tokens', () => {
  assert.match(MCP_API, /http\.get\('\/mcp-tokens'/,
    '列表走的不是 GET /mcp-tokens')
  assert.match(VIEW, /mcpApi\.listMyTokens\(\)/,
    '页面没有去拉自己的密钥列表')
})

test('生成走 POST /mcp-tokens，**而且必须带上当前密码**', () => {
  assert.match(MCP_API, /http\.post\('\/mcp-tokens'/, '生成走的接口不对')
  assert.match(VIEW, /current_password: mcpForm\.current_password/,
    '当前密码没发过去 —— 服务端验的就是它')
})

test('请求里**没有「改谁」这个字段** —— 目标永远是本人', () => {
  // 授权判据与操作对象是同一个东西（令牌对应的那个人），所以不存在
  // 「改到别人头上」的可能。传一个 user_id 上去反而多出一道能出错的关卡。
  assert.doesNotMatch(MCP_API, /user_id/,
    '接口里出现了 user_id —— 这一路只该动本人名下的密钥')
  assert.doesNotMatch(VIEW, /user_id/,
    '页面往上传了 user_id —— 同上')
})

test('**明文必须显示出来** —— 只弹一句「生成成功」等于白生成', () => {
  // MCP 密钥永不过期，明文只在签发那一次响应里回来，之后谁也拿不回来
  // （服务器上只存摘要）。所以界面必须**当场把它显示出来**，让人抄走。
  assert.match(VIEW, /plainToken\.value = data\.token/,
    '返回的明文没有被显示出来 —— 这一枚密钥谁也用不了')
  assert.match(VIEW, /plainVisible\.value = true/,
    '没有把明文弹出来')
  assert.match(VIEW, /plain-token|plain-input/,
    '明文那一块没有专门的展示位')
})

test('删除一枚走 DELETE /mcp-tokens/{id}，全部删除走 DELETE /mcp-tokens', () => {
  assert.match(MCP_API, /http\.delete\(`\/mcp-tokens\/\$\{id\}`\)/,
    '删除一枚走的接口不对')
  assert.match(MCP_API, /http\.delete\('\/mcp-tokens'\)/,
    '全部删除走的接口不对')
  assert.match(VIEW, /mcpApi\.deleteMyToken\(row\.id\)/,
    '页面没有按 id 删除')
  assert.match(VIEW, /mcpApi\.deleteAllMyTokens\(\)/,
    '页面没有「全部删除」这条路')
})

test('界面上用的是「删除」，不是「吊销」', () => {
  // 本人这一侧的处置就是**删掉记录**。界面上还写着「吊销」会让人以为
  // 后面还留着一行旧记录 —— 而服务端已经把那一行删了。
  assert.match(SHOWN, /全部删除/, '没有「全部删除」这个动作')
  assert.match(SHOWN, />删除</, '没有「删除」这个动作')
  assert.doesNotMatch(SHOWN, /吊销/, '界面上还在说「吊销」—— 那一侧的动作是「删除」')
  assert.doesNotMatch(MCP_CODE, /吊销/, '接口层还在说「吊销」')
  assert.doesNotMatch(MCP_CODE, /revoke/i,
    '接口名里还有 revoke —— 本人这一侧的两个动词都是 delete')
})

test('删除前必须先问一句（删错了就是当场断线，而且找不回来）', () => {
  assert.match(VIEW, /ElMessageBox\.confirm\(/,
    '删除没有确认框 —— 密钥永不过期，删错了当场断线')
})

test('列表上要看得见「这枚密钥能读哪些账套」', () => {
  // 「权限跟着用户走」这件事必须**看得见**：密钥不带自己的账套范围，
  // 能读什么完全由本人的授权决定 —— 本人得能一眼确认这一点。
  assert.match(VIEW, /row\.books\.length/,
    '列表上没有显示可读账套的数量')
  assert.match(VIEW, /bookNames/,
    '账套名没有展开的地方')
})

test('这一页仍然不摆说明条（MCP 那一块也不许摆）', () => {
  assert.doesNotMatch(VIEW, /<el-alert/,
    '又摆了一条常驻说明 —— 说明只在动作发生的那一刻说')
  assert.doesNotMatch(VIEW, /class="muted form-hint"/,
    '又出现了常驻的字段提示条')
})
