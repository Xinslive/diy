/**
 * 壳层的骨架把页面「藏没了」，页面里量自己的尺寸就量成了 0。
 *
 * **现象**（2026-10-28 用户报，三件事一个根）：
 *   1. 凭证详情页的「会计科目」列不显示科目名（摘要列也空）；
 *   2. 表格错位 —— 金额数字跟表头的竖线对不齐，合计行跟分录行也对不齐；
 *   3. 时好时坏：从列表首次进入看不到，点「前一张/后一张」有时又正常。
 *
 * **机制**：
 * 壳层（MainLayout）在路由数据没到齐的时候摆一个页面级骨架，
 * 路由页让位 —— 原来让位用的是 ``v-show``，那**就是 display:none**：
 * 整棵子树没有布局盒。
 *
 * 而页面里有三处**量自己尺寸**的地方：
 *   · 凭证表的 syncGridWidth() 量 clientWidth → 量到 0 → ``--vg-grid-width: 0px``
 *     → 表头与合计行塌成 0 宽，列宽退回各列最小值 → **竖线与分录行对不齐**；
 *   · 摘要格的 autosizeSum() 量 scrollHeight → 量到 0 → ``height: 0px``
 *     → **摘要看不见**；
 *   · 科目格的 autosize() 量 scrollHeight → 量到 0 → ``height: 0px``
 *     → **科目名看不见**。
 *
 * 「时好时坏」就是**赛跑**：量尺寸发生在骨架撤下之前还是之后，
 * 决定这一页是好的还是坏的。
 *
 * 本文件钉住两件事：
 *   1. 路由页**不许**用 display:none 藏（v-show 就是它）；
 *   2. 三处量尺寸的地方，量到 0 时**不许**把 0 写进样式 ——
 *      「没有盒子」不是「内容是空的」，这两件事必须分开。
 *
 * 测试的路子是本仓的老规矩：**从真实组件源码里抠出那段实现来跑** ——
 * 被测的是页面上真在用的那份代码，不是另写一份等价物。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const LAYOUT_SRC = fs.readFileSync(
  new URL('../src/layouts/MainLayout.vue', import.meta.url), 'utf8')
const MAIN_CSS = fs.readFileSync(
  new URL('../src/styles/main.css', import.meta.url), 'utf8')
const VOUCHER_SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const PICKER_SRC = fs.readFileSync(
  new URL('../src/components/SubjectPicker.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 抠出一段实现，并自检抠到的是它（抠空了就让断言失败，别让测试空转）。 */
function block(src, start, end, what) {
  const a = src.indexOf(start)
  const b = src.indexOf(end)
  assert.ok(a !== -1, `源码里找不到标记 ${start}（${what}）`)
  assert.ok(b > a, `源码里找不到结束标记 ${end}（${what}）`)
  return src.slice(a, b)
}

/** 抠出一个函数（从函数头到它在行首的那个右花括号）。 */
function funcBlock(src, header, what) {
  const a = src.indexOf(header)
  assert.ok(a !== -1, `源码里找不到 ${header}（${what}）`)
  const b = src.indexOf('\n}\n', a)
  assert.ok(b > a, `源码里找不到 ${what} 的结尾`)
  return src.slice(a, b + 3)
}

/** 取一段 CSS 规则块的内容。 */
function cssBlock(text, selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')
  const m = text.match(re)
  assert.ok(m, '样式表里没找到 ' + selector + ' 这段')
  return m[1]
}

/** 一个假的分录滚动区：只有 clientWidth 与那只会写样式的父盒子。 */
function fakeScrollBox(clientWidth) {
  const written = {}
  const wrap = {
    style: {
      setProperty(name, value) { written[name] = value },
    },
  }
  return { written, box: { clientWidth, parentElement: wrap } }
}

/** 一个假的输入框：scrollHeight 就是「内容有多高」。 */
function fakeInput(scrollHeight) {
  return { style: { height: '', overflowY: '' }, scrollHeight }
}

// --------------------------------------------------------------------- 抠实现

const SYNC_BLOCK = funcBlock(
  VOUCHER_SRC, 'function syncGridWidth() {', '凭证表的列宽测量')
const SUM_BLOCK = funcBlock(
  VOUCHER_SRC, 'function autosizeSum(el, measured) {', '摘要格的高度自适应')
const PICKER_BLOCK = funcBlock(
  PICKER_SRC, 'function autosize() {', '科目格的高度自适应')

/** 跑真实的 syncGridWidth。 */
function syncGridWidth(entryScroll) {
  const factory = new Function('entryScroll', `${SYNC_BLOCK}\nreturn syncGridWidth`)
  return factory(entryScroll)
}

/** 跑真实的 autosizeSum。 */
function autosizeSum() {
  const factory = new Function(
    'getComputedStyle', 'sumArmed',
    `${SUM_BLOCK}\nreturn autosizeSum`)
  return factory(() => ({ lineHeight: '21px' }), false)
}

/** 跑真实的 autosize（SubjectPicker）。 */
function pickerAutosize(inputEl) {
  const factory = new Function(
    'inputEl', 'perFrame', 'getComputedStyle', 'wraps',
    `${PICKER_BLOCK}\nreturn autosize`)
  return factory(inputEl, (_key, fn) => fn(), () => ({ lineHeight: '22px' }), { value: false })
}

console.log('== 一、路由页不许被 display:none 藏起来 ==')

t('路由页不是用 v-show 让位的（v-show 就是 display:none）', () => {
  const m = LAYOUT_SRC.match(/<div class="route-page"[^>]*>/)
  assert.ok(m, 'MainLayout.vue 里找不到路由页那个 div —— 结构改了？')
  assert.ok(!/v-show/.test(m[0]),
    '路由页还在用 v-show —— 那就是 display:none，页面里量到的尺寸全是 0')
})

t('路由页让位时保留布局盒（visibility:hidden，不是 display:none）', () => {
  const pending = cssBlock(MAIN_CSS, '.route-page.is-pending')
  assert.ok(/visibility:\s*hidden/.test(pending),
    '路由页让位时要 visibility:hidden —— 藏起来但**留着盒子**，量得到宽高')
  assert.ok(!/display:\s*none/.test(pending),
    '路由页让位时不能 display:none —— 那会把整棵子树的布局盒一起收走')
  assert.ok(/position:\s*absolute/.test(pending),
    '路由页让位时应当绝对定位（离开正常流），骨架才有地方摆，不把页面顶下去')
})

t('路由页把高度链接回去（height:100%）', () => {
  const rp = cssBlock(MAIN_CSS, '.route-page')
  assert.ok(/height:\s*100%/.test(rp),
    '路由页缺了 height:100% —— .voucher-page / .print-page 那两页是 height:100% 的整页，' +
    '中间少一层，百分比高度就落到 auto 上，整页不再占满内容区')
})

console.log('')
console.log('== 二、量到 0 不等于「内容是空的」 ==')

t('凭证表列宽：页面没有盒子时，不把 0 写进 --vg-grid-width', () => {
  const { written, box } = fakeScrollBox(0)
  syncGridWidth({ value: box })()
  assert.equal(written['--vg-grid-width'], undefined,
    '把 0 写成了 --vg-grid-width —— 表头与合计行会塌成 0 宽，竖线跟分录行对不齐')
})

t('凭证表列宽：页面有盒子时，照常量出真实宽度', () => {
  const { written, box } = fakeScrollBox(900)
  syncGridWidth({ value: box })()
  assert.equal(written['--vg-grid-width'], '800px',
    '真实宽度算错了 —— 900 扣掉左右两条动作留白各 50')
})

t('摘要格：没有盒子时，不把高度写成 0（那会让摘要永远看不见）', () => {
  const el = fakeInput(0)
  autosizeSum()(el)
  assert.notEqual(el.style.height, '0px',
    '摘要格被写成 0 高 —— 摘要就永远看不见了')
})

t('摘要格：内容有一行时，照常量出高度', () => {
  const el = fakeInput(30)
  autosizeSum()(el)
  assert.equal(el.style.height, '30px')
})

t('科目格：没有盒子时，不把高度写成 0（那会让科目名永远看不见）', () => {
  const el = fakeInput(0)
  pickerAutosize({ value: el })()
  assert.notEqual(el.style.height, '0px',
    '科目格被写成 0 高 —— 会计科目那一列就永远是空的')
})

t('科目格：内容有一行时，照常量出高度', () => {
  const el = fakeInput(30)
  pickerAutosize({ value: el })()
  assert.equal(el.style.height, '30px')
})

console.log('')
console.log('== 三、返回按钮说清楚去哪儿 ==')

t('凭证页的按钮写「凭证列表」，不是「← 返回」', () => {
  assert.ok(/<el-button @click="backToList">凭证列表<\/el-button>/.test(VOUCHER_SRC),
    '凭证详情页的返回按钮文案没改成「凭证列表」')
  assert.ok(!/<el-button @click="backToList">← 返回<\/el-button>/.test(VOUCHER_SRC),
    '「← 返回」的文案还留着 —— 说的是「往回走」，不是「去哪儿」')
})

/* ------------------------------------------------------------------ 跑 */
for (const [name, fn] of tests) {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}
console.log('')
console.log(`  ${pass} 通过 / ${fail} 失败`)
if (fail) process.exit(1)
