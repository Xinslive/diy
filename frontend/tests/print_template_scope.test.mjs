/**
 * 打印模板的三层归属（src/utils/printTemplate.js，**线上那份源码**）。
 *
 *  照应的是后端 `app/api/printing.py` 的三个口径：
 *   - `_template()` 取默认模板的 ORDER BY（user_id IS NULL, book_id IS NULL, id）
 *   - `_clear_other_defaults()` 的分组键 (user_id, book_id, scope)
 *   - `_require_global_template_write` / `_fork_private` 的可改性
 *
 *  最后一组是源码级断言：盯住「设计器真的用了这份门禁」，不然 util 写得再对，
 *  页面照样按 store.canWrite 一把梭，用户点了才吃后端 403。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const U = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/printTemplate.js'))

const VIEW = fs.readFileSync(new URL('../src/views/print/PrintTemplate.vue', import.meta.url), 'utf8')
const DIALOG = fs.readFileSync(new URL('../src/components/PrintDialog.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

// 三层：系统基线（两个归属都空）/ 账套模板 / 我的私有副本。
const BASELINE = { id: 1, name: '凭证纸专用', book_id: null, is_baseline: true, is_private: false, is_default: true }
const OTHER_BASE = { id: 2, name: '基础模板', book_id: null, is_baseline: true, is_private: false, is_default: false }
const BOOK_TPL = { id: 3, name: '本账套模板', book_id: 59, is_baseline: false, is_private: false, is_default: true }
const MINE = { id: 4, name: '凭证纸专用', book_id: null, is_baseline: false, is_private: true, is_default: false }

const OWNER = { role: 'OWNER', isAdmin: false }
const ACCOUNTANT = { role: 'ACCOUNTANT', isAdmin: false }
const ADMIN = { role: 'ACCOUNTANT', isAdmin: true }
const VIEWER = { role: 'VIEWER', isAdmin: false }

console.log('== 生效的默认：私有 → 账套 → 基线 ==')

t('只有基线标着默认时，取它', () => {
  assert.equal(U.effectiveDefaultId([BASELINE, OTHER_BASE]), 1)
})

t('账套默认压过基线默认', () => {
  assert.equal(U.effectiveDefaultId([BASELINE, BOOK_TPL]), 3)
})

t('我的私有副本压过账套默认', () => {
  const mine = { ...MINE, is_default: true }
  assert.equal(U.effectiveDefaultId([BASELINE, BOOK_TPL, mine]), mine.id)
})

t('私有副本没标默认时不顶掉账套默认', () => {
  // 私有副本只是「我改的那份」，它是不是默认是另一回事 ——
  // 否则「改基线」这个动作会顺手把默认也改了，用户没要求过。
  assert.equal(U.effectiveDefaultId([BASELINE, BOOK_TPL, MINE]), 3)
})

t('一个都没有时返回 null', () => {
  assert.equal(U.effectiveDefaultId([OTHER_BASE]), null)
  assert.equal(U.effectiveDefaultId([]), null)
})

console.log('== 列表标记 ==')

t('只标「默认」，私有副本不加任何标记', () => {
  // 写时复制是实现细节，不往界面上贴「（我的）」：用户改两下、存一下，看到的
  // 仍然是那份他熟悉的模板名。机制没变（见 canEditTemplate），只是不必每次都
  // 提醒一遍「这份只有你看得见」。
  const mine = { ...MINE, is_default: true }
  assert.deepEqual(U.templateTags(mine, mine.id), ['默认'])
  assert.deepEqual(U.templateTags(MINE, 1), [])
  assert.deepEqual(U.templateTags(BOOK_TPL, 4), [])
  assert.deepEqual(U.templateTags(BOOK_TPL, 3), ['默认'])
})

t('私有副本只是不标标记，归属与可改性都没变', () => {
  // 防止「不标了」被顺手做成「私有副本当看不见」：仍然可改，且仍是生效的默认。
  assert.equal(U.canEditTemplate(MINE, ACCOUNTANT), true)
  assert.equal(U.effectiveDefaultId([{ ...MINE, is_default: true }, BASELINE]), MINE.id)
})

t('别人的默认不是我的默认时，我不标', () => {
  // 基线那份在别人账套里也是默认；列表给我的默认是账套模板，就别乱标。
  assert.deepEqual(U.templateTags(BASELINE, 3), [])
})

console.log('== 可改性：改哪一层落到哪一层 ==')

t('系统基线：会计就能改（改动落成他自己的私有副本）', () => {
  assert.equal(U.canEditTemplate(BASELINE, ACCOUNTANT), true)
  assert.equal(U.canEditTemplate(BASELINE, OWNER), true)
})

t('系统基线：只读账号不行', () => {
  assert.equal(U.canEditTemplate(BASELINE, VIEWER), false)
})

t('账套模板：要 OWNER 或管理员，会计不行', () => {
  assert.equal(U.canEditTemplate(BOOK_TPL, OWNER), true)
  assert.equal(U.canEditTemplate(BOOK_TPL, ADMIN), true)
  assert.equal(U.canEditTemplate(BOOK_TPL, ACCOUNTANT), false)
})

t('私有副本：主人可改，管理员也不例外（列表里看得见它就说明是主人）', () => {
  assert.equal(U.canEditTemplate(MINE, ACCOUNTANT), true)
})

t('没有选中模板时一律不能改', () => {
  assert.equal(U.canEditTemplate(null, OWNER), false)
})

console.log('== 可删性 ==')

t('系统基线不能删', () => {
  assert.equal(U.canDeleteTemplate(BASELINE, OWNER), false)
  assert.equal(U.canDeleteTemplate(BASELINE, ADMIN), false)
})

t('私有副本能删，删了就回到基线', () => {
  assert.equal(U.canDeleteTemplate(MINE, ACCOUNTANT), true)
})

t('账套模板仍按 OWNER 那一套', () => {
  assert.equal(U.canDeleteTemplate(BOOK_TPL, OWNER), true)
  assert.equal(U.canDeleteTemplate(BOOK_TPL, ACCOUNTANT), false)
})

console.log('== 新建：新建出来的是账套模板，所以要 OWNER ==')

t('会计不能新建（后端会拒，别让用户点了才知道）', () => {
  assert.equal(U.canCreateTemplate(ACCOUNTANT), false)
  assert.equal(U.canCreateTemplate(OWNER), true)
  assert.equal(U.canCreateTemplate(ADMIN), true)
  assert.equal(U.canCreateTemplate(VIEWER), false)
})

console.log('== 内置版式那一项：凭证范围有基线就不摆 ==')

t('凭证范围一份保存模板都没有时才保留', () => {
  assert.equal(U.showBuiltinTemplate('VOUCHER', []), true)
  assert.equal(U.showBuiltinTemplate('VOUCHER', [BASELINE]), false)
})

t('其它范围保留（那里它可能是唯一选项）', () => {
  assert.equal(U.showBuiltinTemplate('LEDGER', []), true)
})

console.log('== 新建即私有：canCreatePrivateTemplate ==')

t('会计能新建个人版式（不用 OWNER：只有改动者一个人看得见）', () => {
  assert.equal(U.canCreatePrivateTemplate(ACCOUNTANT), true)
})

t('OWNER/管理员当然也行', () => {
  assert.equal(U.canCreatePrivateTemplate(OWNER), true)
  assert.equal(U.canCreatePrivateTemplate({ role: 'ACCOUNTANT', isAdmin: true }), true)
})

t('只读账号两条路都不行', () => {
  assert.equal(U.canCreatePrivateTemplate(VIEWER), false)
  assert.equal(U.canCreateTemplate(VIEWER), false)
})

t('角色认不出来一律当拒绝（fail-closed，不是黑名单判断）', () => {
  assert.equal(U.canCreatePrivateTemplate({}), false)
  assert.equal(U.canCreatePrivateTemplate({ role: 'ghost' }), false)
  assert.equal(U.canCreateTemplate({}), false)
})

t('两条路的差别**只有** OWNER 那一条，会计仍是分水岭', () => {
  // 写清这道门到底放宽了多少：OWNER 两边都真，会计只有私有那条。
  // 哪天 canCreatePrivateTemplate 顺手写成了 canCreateTemplate 的复制品，
  // 这个断言会先发现。
  assert.equal(U.canCreateTemplate(OWNER), true)
  assert.equal(U.canCreateTemplate(ACCOUNTANT), false)
  assert.equal(U.canCreatePrivateTemplate(OWNER), true)
  assert.equal(U.canCreatePrivateTemplate(ACCOUNTANT), true)
})

console.log('== 页面确实走了这份门禁 ==')

t('设计器用了可改/可删/可新建三个判断，不再一把梭 store.canWrite', () => {
  assert.match(VIEW, /canEditTemplate/)
  assert.match(VIEW, /canDeleteTemplate/)
  assert.match(VIEW, /canCreateTemplate/)
  assert.match(VIEW, /templateTags/)
})

/**
 * 「更多」菜单里的**菜单项本身**。只取 <el-dropdown-item> 那几行：整个
 * <el-dropdown-menu> 块里有一段解释这两项为什么撤掉的注释，断言打到注释上
 * 就成了「注释里不许提这个词」，那显然不是想钉的东西。
 */
const menuItems = () => VIEW.match(/<el-dropdown-item[\s\S]*?<\/el-dropdown-item>/g)?.join('\n') || ''

t('「更多」里只剩重命名/设默认/删除 —— 复制一份与新建个人版式已撤掉', () => {
  // 撤掉的是「拿屏幕上的东西再生成一份模板」那两条：顶栏「新建」已经覆盖了
  // 其中真需要留的那件，三选一反而让人先想该点哪个。这里把撤掉钉住，免得
  // 下次整理菜单时又被当成「漏了功能」加回来。
  const items = menuItems()
  assert.equal(items.match(/<el-dropdown-item/g).length, 3, `菜单项不该多于三个：${items}`)
  assert.doesNotMatch(items, /复制一份/)
  assert.doesNotMatch(items, /新建个人版式/)
  assert.doesNotMatch(VIEW, /command="copy"|command="create-private"/)
  assert.doesNotMatch(VIEW, /doCopy/, 'doCopy 已无入口，留在文件里就是死代码')
})

t('撤掉入口后，图标与分派都没留下悬空引用', () => {
  // 这类残留不会报错，只会让 import 与 onMore 各挂一个用不到的东西。
  const imports = VIEW.slice(VIEW.indexOf("from '@element-plus/icons-vue'") - 400,
    VIEW.indexOf("from '@element-plus/icons-vue'"))
  for (const icon of ['CopyDocument', 'Lock']) {
    assert.doesNotMatch(imports, new RegExp(`\\b${icon},`), `${icon} 已无使用，应从 import 去掉`)
    assert.doesNotMatch(VIEW, new RegExp(`<${icon} `), `${icon} 已无使用，应从模板去掉`)
  }
})

t('顶栏「新建」建的是账套模板，不发 private', () => {
  // 后端 TemplateIn.private 默认 False（fail-closed），不传就是账套模板。
  // 这里显式钉住「不发」：一旦把 private 传成 true，顶栏那个（OWNER 门禁、
  // 语义是给同事用）就会变成只有自己看得见，恰好与它的含义相反。
  const fn = VIEW.slice(VIEW.indexOf('async function doCreate()'))
  const body = fn.slice(0, fn.indexOf('\nasync function') === -1 ? 900 : fn.indexOf('\nasync function'))
  assert.doesNotMatch(body, /private:/, '顶栏「新建」不该再发 private')
  assert.doesNotMatch(VIEW, /canCreatePrivateTemplate/)
})

t('打印弹窗的标记与设计器共用一份 templateTags', () => {
  assert.match(DIALOG, /templateTags/)
  assert.match(DIALOG, /effectiveDefaultId|sharedEffectiveDefault/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
// 必须 process.exit 而不是 process.exitCode：vite 的 middlewareMode server
// 还挂着事件循环，只设 exitCode 的话 node --test 会一直等下去。
process.exit(fail ? 1 : 0)
