/**
 * 纸型只留 A4；「每页几张凭证」独立成模板字段（src/utils/paper.js，**线上那份源码**）。
 *
 *  照应的是后端 `app/printing/template.py::validate_template` 与 `api/printing.py`：
 *   - paper 一律归一成 A4（老模板的 A5/A3 只在 validate 时被接住）
 *   - voucher_slots_per_page() 只读 voucher.slots_per_page，不再从纸型推
 *   - 打印端点不再收 paper 参数（_with_orientation 只剩方向一个维度）
 *
 *  这一组盯的是**两边不会各写一份**：纸型收敛成一个值之后，任何地方再从
 *  纸型推「一页几张」，推出来的答案就永远是 2，而用户要的「A4 一页一张」
 *  正好是被这一条掐死的。缺省值也必须与后端一致，否则画布说两张、预览说一张。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const U = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/paper.js'))

const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), 'utf8')
const INSPECTOR = read('../src/components/print/TemplateInspector.vue')
const DIALOG = read('../src/components/PrintDialog.vue')
const VIEW = read('../src/views/print/PrintTemplate.vue')
const STAGE = read('../src/components/print/PrintStage.vue')

/**
 * 去掉注释再断言「代码里没有某个写法」。
 *
 * 不去注释的话，下面每一条都可能被**解释这个坑的注释**本身命中 —— 那条注释
 * 恰好写着被删掉的老代码长什么样，于是「已经修好了」和「还没修」长得一模一样，
 * 测试从此只会一直绿。这类假绿比没有测试更坏。
 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const CODE = {
  INSPECTOR: stripComments(INSPECTOR),
  STAGE: stripComments(STAGE),
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 只剩 A4 ==')

t('允许选择的纸型只有 A4', () => {
  assert.deepEqual(U.PAPER_KEYS, ['A4'])
})

t('后端下发的纸型被收敛到只剩 A4', () => {
  const down = [{ key: 'A4' }, { key: 'A5' }, { key: 'B5' }, { key: 'A3' },
                { key: 'CONTINUOUS' }, { key: 'A4_LANDSCAPE' }]
  assert.deepEqual(U.pickPapers(down).map(p => p.key), ['A4'])
})

t('老模板的纸型兜回 A4', () => {
  for (const old of ['A5', 'B5', 'A3', 'A4_LANDSCAPE', undefined, null, '泰坦纸']) {
    assert.equal(U.normalizePaper(old), 'A4', String(old))
  }
})

t('纸型文案不再夹带「每页 N 张」', () => {
  // 「A4（每页 2 张凭证）」这种文案在只有一个纸型之后是纯误导：真正决定
  // 一页几张的是模板里的 slots_per_page，跟纸型没关系。
  assert.equal(U.paperLabel('A4'), 'A4')
  assert.ok(!/每页/.test(U.paperLabel('A4', true)), '还在说每页几张')
})

console.log('== 一页几张凭证 = 模板自己说 ==')

t('从模板的 voucher.slots_per_page 读', () => {
  assert.equal(U.slotsPerPage({ voucher: { slots_per_page: 1 } }), 1)
  assert.equal(U.slotsPerPage({ voucher: { slots_per_page: 2 } }), 2)
})

t('缺省值与后端 validate_template 一致（= 2）', () => {
  // 后端对没有该字段的模板也补 2（A4 的旧含义就是一页两张）。双方取不同的缺省值，画布上就会出现两张、预览一张。
  for (const tpl of [{}, { voucher: {} }, { voucher: null }, null, undefined,
                     { paper: 'A5' }, { voucher: { slots_per_page: 0 } },
                     { voucher: { slots_per_page: 9 } }]) {
    assert.equal(U.slotsPerPage(tpl), 2, JSON.stringify(tpl))
  }
})

t('只接受 1 和 2', () => {
  assert.deepEqual(U.SLOTS_OPTIONS, [1, 2])
})

console.log('== 界面上真的去掉了纸型选择 ==')

t('检查器不再有纸型下拉框', () => {
  assert.ok(!/纸型[\s\S]{0,120}<el-select/.test(INSPECTOR),
            '「纸型」那一行下面还是 el-select')
  assert.ok(!INSPECTOR.includes('onPaperChange'), '还留着 onPaperChange')
})

t('检查器提供「每页凭证数」选择', () => {
  assert.ok(INSPECTOR.includes('每页凭证数'), '没有这个选项')
  assert.ok(INSPECTOR.includes('SLOTS_OPTIONS'), '不是用共享的可选值')
  // 只能写到 voucher.slots_per_page：渲染层只读这一个字段。
  assert.ok(/slots_per_page/.test(INSPECTOR))
  assert.ok(/function setSlots[\s\S]{0,320}slots_per_page/.test(INSPECTOR),
            '改了但没写到 voucher.slots_per_page')
})

t('每页几张不在账簿报表的检查器里出现', () => {
  // 账簿报表说「一页几张凭证」是不可会的：空闲里出现一个选项、依然要人去猜它有什么用。
  const sec = INSPECTOR.match(/<section v-if="isVoucher" class="sec">[\s\S]*?<\/section>/g) || []
  assert.ok(sec.every(b => !b.includes('每页凭证数')),
            '该项应该只在 isVoucher 的分支里出现')
})

t('打印弹窗里没有纸型下拉框', () => {
  assert.ok(!/<el-form-item label="纸型"/.test(DIALOG), '弹窗仍有纸型下拉')
  assert.ok(!DIALOG.includes('onPaperChange'), '还留着 onPaperChange')
  assert.ok(!/p\.paper = /.test(DIALOG), '请求里还在带 paper 参数')
})

t('弹窗说明文案不再提 A5', () => {
  assert.ok(!/A5/.test(DIALOG), '弹窗里还剩 A5 的文案')
})

t('画布从模板读每页几张', () => {
  assert.ok(/slotsPerPage\(tpl\.value\)/.test(VIEW),
            '画布还在用 TWO_UP_PAPERS 推')
  assert.ok(!/TWO_UP_PAPERS/.test(VIEW))
})

t('全局只有一份纸型口径', () => {
  const files = { INSPECTOR, DIALOG, VIEW }
  for (const [name, src] of Object.entries(files)) {
    assert.ok(!/DEFAULT_ORIENTATION/.test(src), name + ' 还引用了 DEFAULT_ORIENTATION')
  }
})

console.log('== 每页一张 = 两张里去掉一张，不是把剩下那张抻开 ==')

t('画布槽位不按张数重算（去掉那个撑满整页的特例）', () => {
  const STAGE_CODE = CODE.STAGE, INSPECTOR_CODE = CODE.INSPECTOR
  // 元素坐标是槽位内的百分比。槽位一旦跟着张数变大，标题、表格、页脚会一起被
  // 纵向抻开，而用户一个坐标都没改过 —— 看着就是「模板严重变形」。
  // 槽位恒按两张算，放几张只决定返回前几个格子。
  assert.ok(!/slots\s*<=\s*1\)[^\n]*return\s*\[\s*c\s*\]/.test(STAGE_CODE),
            'PrintStage 还有「一张就占满内容区」的特例')
  assert.ok(!/if\s*\(slots\.value\s*===\s*1\)/.test(INSPECTOR_CODE),
            'TemplateInspector 的 slotMm 还按张数改尺寸')
  assert.ok(!/gap\s*=\s*slots\.value\s*==/.test(INSPECTOR_CODE),
            '槽位之间的缝不该跟着张数变')
  assert.ok(/cells\.slice\(0,/.test(STAGE_CODE), '槽位应当是「按两张算、再截前几个」')
})

t('排布方向只看横竖，不看放几张', () => {
  assert.ok(!/slots\s*==\s*2[^\n]*orientation[^\n]*landscape/.test(STAGE),
            '横排与否不该由张数决定')
})

console.log('\n通过 ' + pass + ' / 失败 ' + fail)
process.exit(fail ? 1 : 0)
