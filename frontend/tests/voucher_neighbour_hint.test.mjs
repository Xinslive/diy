/**
 * 「前一张/后一张」上不再挂位置提示气泡（src/views/voucher/VoucherEdit.vue）。
 *
 * 2026-10-24 用户在 acct.xins.live 的凭证页上把鼠标停在「前一张/后一张」上，
 * 弹出一句「全部凭证里的第 1634 / 2147 张」，当场打回：**不要显示这个**。
 *
 * 为什么该删：那句话解释的是按钮**本来就在干的事** —— 能不能点（:disabled）
 * 已经把「有没有前一张/后一张」说完了，多这一句只是噪音。跟「凭证页不摆说明条」
 * 那条规矩（插入凭证顺位、科目候选面板的层级说明）是一回事，前后被打回两次。
 *
 * 断言都只看**源码**：这些事说的都是「界面上还有没有这个东西」，跑起来点一遍
 * 只能证明点得动，证明不了它没被换成一个别的形状的提示。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/**
 * 去掉注释，只留**会被渲染/执行**的那部分。
 *
 * 源码里到处是解释「为什么」的注释（这一页的注释密度很高），其中就会**引用**
 * 被删掉的那句话 —— 拿带注释的源码去断言「这句话不在了」，断言的是注释而不是
 * 界面，下次有人把说明补进注释里反而会平白炸一条。
 */
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')            // 模板里的 HTML 注释
    .replace(/\/\*[\s\S]*?\*\//g, '')            // 块注释
    .replace(/^[ \t]*\/\/.*$/gm, '')             // 整行行注释
}

/** 工具条那一段：从 `toolbar no-print` 到卡片区之前。 */
const toolbar = () => stripComments(VIEW).slice(
  VIEW.indexOf('<div class="toolbar no-print">'), VIEW.indexOf('<div class="card">'))

test('「前一张/后一张」外面没有套气泡', () => {
  const bar = toolbar()
  const at = bar.indexOf('前一张')
  assert.ok(at !== -1, '工具条上找不到「前一张」')
  const before = bar.slice(0, at)
  const opens = (before.match(/<el-tooltip/g) || []).length
  const closes = (before.match(/<\/el-tooltip>/g) || []).length
  assert.equal(opens, closes,
    '「前一张/后一张」被一个 <el-tooltip> 包着 —— 位置提示气泡又回来了')
})

test('源码里没有位置提示那句话', () => {
  const code = stripComments(VIEW)
  assert.doesNotMatch(code, /里的第/, '位置提示文案还在')
  assert.doesNotMatch(code, /neighbourScope/, 'neighbourScope 这个状态还在')
})

test('翻页按钮本身还在，且仍按有没有邻票变灰', () => {
  const bar = toolbar()
  assert.match(bar, /<el-button :disabled="!prevId" @click="goNeighbour\('prev'\)">前一张<\/el-button>/)
  assert.match(bar, /<el-button :disabled="!nextId" @click="goNeighbour\('next'\)">后一张<\/el-button>/)
})

test('邻票信息照旧往后端要（只是不再往页面上摆）', () => {
  // 删的只是页面上的那行字，翻页本身要的数据一张都不能少。
  const code = stripComments(VIEW)
  assert.match(code, /prevId\.value = r\.prev_id/)
  assert.match(code, /nextId\.value = r\.next_id/)
})
