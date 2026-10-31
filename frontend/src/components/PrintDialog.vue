<template>
  <el-dialog
    v-model="visible" :title="title" width="min(1120px, 92vw)" top="5vh"
    :close-on-click-modal="false" destroy-on-close append-to-body
    class="print-dialog"
  >
    <div class="body">
      <!-- 左：这次打印怎么打。右：打出来长什么样。 -->
      <div class="side">
        <el-form label-position="top" size="default">
          <el-form-item label="打印模板">
            <el-select v-model="templateId" style="width: 100%" placeholder="使用默认模板"
                       @change="onTemplateChange">
              <!-- 模板名带上纸型：模板表里可能存了好几份版式，光看名字分不出
                   哪份是给这台打印机准备的。这里不用自定义插槽 —— el-option 一旦
                   给了插槽，收起时显示的也是插槽内容，两处排版就得各写一遍，
                   改一处忘一处就出现「下拉里带纸型、收起后不带」。 -->
              <!-- 标记与设计器同一份口径（utils/printTemplate.js::templateTags）：
                   只标「默认」= 不指定模板直接打印真正会用的那一份；私有副本不标。
                   两处各写一份就会出现「弹窗里说默认是 A、设计器里说默认是 B」。 -->
              <el-option
                v-for="t in tplList" :key="t.id" :value="t.id"
                :label="`${t.name}（${templateTags(t, defId).join(' · ') || 'A4'}）`"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="方向">
            <!-- 两个按钮平分整列：窄栏里并排的两个小按钮看着像没对齐，
                 等宽铺满才和上面的下拉框左右边界一致。 -->
            <el-radio-group v-model="orientation" class="dir-group" @change="refreshPreview">
              <el-radio-button value="portrait">纵向</el-radio-button>
              <el-radio-button value="landscape">横向</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <!-- 打印顺序（用户 2026-10-30 批注）。
               **这里是两件不同的事，所以是两个开关**：
               「打印顺序优化」管**怎么配对**（一页两张时上下两格错开配对，
               裁开两半后两堆各自成序）；「倒序打印」管**出纸的先后**
               （整叠倒着印）。合成一个开关，就得在旁边解释「这一项在两种
               模板上分别是什么意思」，解释不清就等于没写。

               「打印顺序优化」只在**一页两张**时出现 —— 一页一张时每张纸上
               只有一张凭证，没有「配对」这回事，摆出来只会让人猜它有什么用。

               默认值分开：错位配对默认开（批注 2「默认开启打印顺序优化」），
               倒序打印默认关（批注 3：他的打印机出纸顺序本来就是对的）。

               它是**这一次打印**的设置（跟方向一样），所以住在弹窗里，
               不进模板 —— 模板只管版式，不管这叠纸按什么顺序出。 -->
          <el-form-item v-if="isVoucher && isTwoUp" label="打印顺序优化">
            <el-switch v-model="interleaveOn" @change="refreshPreview" />
          </el-form-item>

          <el-form-item v-if="isVoucher" label="倒序打印">
            <el-switch v-model="reverseOn" @change="refreshPreview" />
          </el-form-item>

          <!-- 标准原票与凭证连续拼成半页队列，长票整页前置；只在两张模板上显示。 -->
          <el-form-item v-if="isVoucher && isTwoUp" label="发票打印">
            <el-switch v-model="invoiceOn" @change="refreshPreview" />
          </el-form-item>

          <!-- 打印日期只有账簿（总账 / 明细账）页脚里那一格要它。
               以前它是后端自动取当天 —— 补记账、归档重打时纸上要写的是
               补账那天，不是出纸那天，所以这里必须能改（用户 2026-10-22 批注 3）。 -->
          <el-form-item v-if="isLedgerBook" label="打印日期">
            <el-input v-model="printDate" placeholder="留空则用当天日期" clearable
                      @change="refreshPreview" />
          </el-form-item>

          <!-- 打印范围与「不打印无发生额科目」是**这一次打印**的设置，
               所以住在弹窗里（用户 2026-10-22 批注 1、2）：页面工具栏上
               原来那个「打印范围」下拉已经删掉 —— 同一件事只留一处。
               模板设计器那边不传 printRanges（那里选的是版式，科目由
               设计器自己的预览科目下拉决定），所以这里不会出现第二份。 -->
          <el-form-item v-if="printRanges.length" label="打印范围">
            <el-select v-model="rangeValue" style="width: 100%">
              <el-option v-for="r in printRanges" :key="r.value"
                         :label="r.label" :value="r.value" />
            </el-select>
          </el-form-item>

          <el-form-item v-if="printRanges.length" label="不打印无发生额科目">
            <el-switch v-model="skipEmptyValue" />
          </el-form-item>
        </el-form>

        <!-- 说明沉到栏底：设置项只有几行，撑不满预览区高，
             留一大片空在下面会让人以为左侧没加载出来。
             原来这里还有一句讲「方向只对本次打印生效、纸张一律 A4」的话 ——
             用户 2026-10-22 批注：去掉。方向与纸型在上面的控件里一眼就
             看得见，再补一句解释只是把左栏压得满满当当。
             于是这里只剩凭证页真需要的那一句（别的范围连这条线都不画，
             空一条虚线看着像没加载完）。 -->
        <div v-if="isVoucher" class="note">
          <p class="note-sub">一页放几张凭证由所选模板决定，在模板设计器里改。</p>
        </div>
      </div>

      <div class="stage-wrap">
        <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon />
        <PrintPreview v-else :html="html" :paper="PAPER" :orientation="orientation" :papers="papers" />
      </div>
    </div>

    <template #footer>
      <span class="status">
        <el-tag v-if="loading" size="small" type="info" effect="plain">生成预览中…</el-tag>
      </span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="printing" :disabled="loading || !!error || !html" @click="doPrint">
        <el-icon><Printer /></el-icon>&nbsp;打印
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 打印前的统一入口：选模板、选纸型、看预览，然后才打印。
 *
 * **为什么不直接 print()**：点一下就出纸，用户没机会确认纸型和版式，
 * 而打印是**不可撤销**的 —— 装订错误的一叠凭证要一张张撕下来重打。
 * 预览不是多余步骤，它就是这笔不可逆操作前的最后一道确认。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Printer } from '@element-plus/icons-vue'
import PrintPreview from '@/components/PrintPreview.vue'
import { loadPrintHtml, openPrintWindow, printWindow, templateMeta, templates as loadTemplates } from '@/api/print'
import { pickPapers, slotsPerPage } from '@/utils/paper'
import { PRINT_ORDER_DEFAULTS, printOrderParam } from '@/utils/printOrder'
import {
  effectiveDefaultId as sharedEffectiveDefault, showBuiltinTemplate, templateTags,
} from '@/utils/printTemplate'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 从模板设计器打开时，优先预览正在编辑的已保存模板。 */
  initialTemplateId: { type: Number, default: null },
  bookId: { type: [Number, String], default: null },
  /** 打印接口路径，如 /books/12/print/vouchers.html。范围参数由 params 带。 */
  path: { type: String, default: '' },
  /** 模板范围：VOUCHER / LEDGER / DETAIL_LEDGER / BALANCE / REPORT。 */
  scope: { type: String, default: 'VOUCHER' },
  /** 除模板/纸型之外的打印范围参数（期间、凭证、科目…）。 */
  params: { type: Object, default: () => ({}) },
  /**
   * 打印范围的可选项（总账 / 明细账那份「本册 / 仅当前科目」）。
   *
   * **传了才显示**「打印范围」与「不打印无发生额科目」两个控件 —— 显隐由
   * 调用方决定，而不是这里按 scope 猜：凭证、余额表、报表没有「成册」这回事，
   * 模板设计器那边选的科目由它自己的预览科目下拉决定。
   */
  printRanges: { type: Array, default: () => [] },
  /**
   * 当前打印范围（``v-model:range``）。
   *
   * 状态**放在父页面**：页面上那个「导出 Excel」按钮要的是同一份范围，
   * 放在弹窗里的话，导出的口径就得另问弹窗一次。
   */
  range: { type: String, default: '' },
  /** 「不打印无发生额科目」（``v-model:skip-empty``），默认打开。 */
  skipEmpty: { type: Boolean, default: true },
  title: { type: String, default: '打印' },
})
const emit = defineEmits(['update:modelValue', 'update:range', 'update:skipEmpty'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const templateId = ref(null)
/** 生效的默认模板 id —— 标签上「· 默认」标的就是它，不是 is_default 直接取。 */
const defId = ref(null)
const defaultTemplates = ref({})
/** 纸张恒为 A4（见 utils/paper.js），预览和请求都要一个具体的值。 */
const PAPER = 'A4'
const orientation = ref('portrait')
const papers = ref([])
const tplList = ref([])
const html = ref('')
const error = ref('')
const loading = ref(false)
const printing = ref(false)

/**
 * report.html 的两个 id 是反的：它的 template_id 指**报表**模板，
 * 版式模板走 print_template_id。其余四个接口都是 template_id 直接指版式。
 */
const templateKey = computed(() => (props.scope === 'REPORT' ? 'print_template_id' : 'template_id'))

const isVoucher = computed(() => props.scope === 'VOUCHER')
/** 账簿（总账 / 明细账）：页脚里有「打印日期」那一格。 */
const isLedgerBook = computed(() =>
  props.scope === 'LEDGER' || props.scope === 'DETAIL_LEDGER')
/** 打印日期。留空 = 用当天（后端兜底）；填了 = 就按填的印。 */
const printDate = ref('')

/**
 * 打印顺序的两个开关（用户 2026-10-30 批注）。
 *
 * * ``interleaveOn``：**怎么配对** —— 一页两张时每张纸的上下两格错开配对
 *   （1-6 号印成 1+4 / 2+5 / 3+6），裁开两半后两堆各自成序。一页一张时
 *   没有配对这回事，这一项不露出来。
 * * ``reverseOn``：**出纸的先后** —— 整叠倒着印。打印机有的把先印的那张压在
 *   最上面，有的把最后印的那张压在最上面，倒过来印才能对上。
 *
 * 两者独立：错位配对管每张纸上放哪两张，倒序管这几张纸按什么先后出。
 */
const interleaveOn = ref(PRINT_ORDER_DEFAULTS.interleave)  // 错位配对，默认开
const reverseOn = ref(PRINT_ORDER_DEFAULTS.reverse)        // 整叠倒着印，默认关
/** 打开后将对应发票原件加入打印队列。 */
const invoiceOn = ref(false)

/** 当前模板一页放几张凭证 —— 决定「打印顺序优化」这一项露不露。 */
const slotsOfTemplate = computed(() => {
  const t = tplList.value.find((x) => x.id === templateId.value)
  return slotsPerPage(t?.content)
})
const isTwoUp = computed(() => slotsOfTemplate.value >= 2)

/**
 * 打印范围与「不打印无发生额科目」都是**父页面**的状态（父页面还要拿它们
 * 拼导出参数），这里只做一层双向绑定。
 *
 * 父页面没给范围时退回第一档 —— 那一档是「本册」，与两页的默认值一致，
 * 不会出现「弹窗里显示本册、请求却按仅当前科目发」这种对不上的情况。
 */
const rangeValue = computed({
  get: () => (props.printRanges.some((r) => r.value === props.range)
    ? props.range
    : (props.printRanges[0]?.value ?? '')),
  set: (v) => emit('update:range', v),
})
const skipEmptyValue = computed({
  get: () => props.skipEmpty,
  set: (v) => emit('update:skipEmpty', v),
})

function orientationOf(t) {
  return t?.content?.orientation || 'portrait'
}

function buildParams() {
  const p = { ...props.params }
  if (templateId.value !== null) p[templateKey.value] = templateId.value
  if (orientation.value) p.orientation = orientation.value
  if (isLedgerBook.value && printDate.value) p.print_date = printDate.value
  // 打印顺序只对凭证有意义（别的范围没有「这叠纸按什么顺序出」这件事）。
  if (isVoucher.value) {
    p.print_order = printOrderParam(slotsOfTemplate.value, { interleave: interleaveOn.value })
    p.reverse = reverseOn.value
    // 「发票打印」只在一页两张时才有（开关也是这么露出来的），所以参数
    // 也跟着这个前提走 —— 换到一页一张的模板，发票页就不该再出现。
    if (isTwoUp.value) p.print_invoices = invoiceOn.value
  }
  return p
}

async function init() {
  error.value = ''
  html.value = ''
  templateId.value = null
  printDate.value = ''
  try {
    // meta 是公开字典，缓存一份即可，不必每次开弹窗都拉。
    if (!papers.value.length) {
      const meta = await templateMeta(props.bookId)
      // 后端 PAPERS 仍保留老纸型（渲染层要认它们），这里只留对外提供的那一种。
      papers.value = pickPapers(meta?.papers || [])
      defaultTemplates.value = meta?.defaults || {}
    }
    tplList.value = (await loadTemplates(props.bookId)) || []
  } catch (e) {
    error.value = e?.response?.data?.detail || '打印模板加载失败'
    return
  }
  const list = tplList.value.filter((t) => t.scope === props.scope)
  defId.value = sharedEffectiveDefault(list)
  const builtin = { id: 0, name: '内置默认', content: defaultTemplates.value[props.scope] || {} }
  // 「内置默认」只在没有已保存模板时才是唯一选项，凭证范围已有基线模板（口径见
  // utils/printTemplate.js，与设计器同一份判断）。留着它，列表里就出现第三种
  // 版式说法 —— 用户打印前要先分清「基线模板」和「内置」到底是什么关系。
  tplList.value = showBuiltinTemplate(props.scope, list) ? [builtin, ...list] : list
  // 设计器传入的 0 明确指内置版式；其它入口仍优先使用账套默认模板。
  // 注意「默认」按生效的那一份算，不是按 is_default 字段直接取 —— 同范围里
  // 我的私有副本、本账套模板、全局基线可能都带着默认标记，只有排最前的那份
  // 会被真正印出来（后端 _template() 的 ORDER BY）。
  const def = tplList.value.find((t) => t.id === props.initialTemplateId) ||
    list.find((t) => t.id === defId.value) || list[0] || builtin
  if (def) {
    // **必须等选项先渲染出来再赋值**。el-select 是靠 modelValue 去已注册的
    // 选项里反查 label 的；选项还没挂上就赋值，收起框里只能显示空。
    // 数据是异步回来的，这里天然会踩到，晚一拍 nextTick 就够了。
    await nextTick()
    templateId.value = def.id
    orientation.value = orientationOf(def)
  }
  await refreshPreview()
}

function onTemplateChange() {
  const t = tplList.value.find((x) => x.id === templateId.value)
  if (!t) return
  orientation.value = orientationOf(t)
  refreshPreview()
}

let token = 0
let previewController
function stopPreview() {
  ++token
  previewController?.abort()
  loading.value = false
}
async function refreshPreview() {
  if (!props.path || !visible.value) return
  previewController?.abort()
  previewController = new AbortController()
  const mine = ++token
  loading.value = true
  const doc = await loadPrintHtml(props.path, buildParams(), { signal: previewController.signal })
  if (mine !== token) return                 // 过期响应直接丢弃，避免旧结果盖掉新的
  if (doc) {
    html.value = doc
    error.value = ''
  } else {
    error.value = '打印内容生成失败，请调整范围或模板后重试'
  }
  loading.value = false
}

function doPrint() {
  if (loading.value || error.value || !html.value) return
  printing.value = true
  try {
    // 直接打印已确认的预览，避免同一批原票再次下载、解码。
    const w = openPrintWindow(html.value)
    if (!w) return
    printWindow(w)
    visible.value = false
  } finally {
    printing.value = false
  }
}

watch(() => props.modelValue, (v) => { if (v) init(); else stopPreview() })
onBeforeUnmount(stopPreview)

// 父页面改了参数（打印范围、不打印无发生额科目）就重出预览 ——
// 预览与打印必须是同一份内容：用户看到的范围就是印出来的范围。
// 范围这两个控件的状态在父页面（导出 Excel 也要用），所以这里等
// props.params 变回来再刷新，而不是自己再算一遍参数。
watch(() => props.params, () => { if (visible.value) refreshPreview() }, { deep: true })
</script>

<style scoped>
/* min-height 只兜一个下限，真正的上限给 max-height：预览区跟着窗口走，
   但不让它在超高屏上长到需要滚动才能看完。 */
.body {
  display: flex; gap: 16px; height: 62vh; min-height: 380px; max-height: 680px;
}
.side {
  width: 268px; flex: none; overflow: auto; padding-right: 6px;
  display: flex; flex-direction: column;
}
.side :deep(.el-form-item) { margin-bottom: 14px; }
/* 说明沉底，填掉设置项与预览区之间那片空白 */
.note { margin-top: auto; padding-top: 12px; border-top: 1px dashed #e4e7ed; }
.note p { margin: 0; font-size: 12px; line-height: 1.7; }
.note-sub { color: #c0c4cc; }
.note-warn { color: #e6a23c; }

.dir-group { display: flex; width: 100%; }
.dir-group :deep(.el-radio-button) { flex: 1; }

/**
 * Element Plus 的 radio-button 左边线是拿 box-shadow 画的，而它给
 * :first-child 写了 ``box-shadow: none !important``（默认那条线是给已选中的
 * 按钮用的，为的是压住中间接缝）。按整列等分之后，第一个按钮左边那半条线
 * 就没人画了 —— 看起来像按钮缺了一道边框，右上/右下却还有圆角。
 *
 * 所以这里四条边都自己画，不再赌它内部怎么实现。圆角仍由它自己的
 * :first-child/:last-child 规则给，不重复定义。
 */
.dir-group :deep(.el-radio-button__inner) {
  width: 100%;
  outline: none;
  border: 1px solid var(--el-border-color);
  box-shadow: none;
}
/* 相邻两条边各画各的会叠成 2px，往回压 1px 合成一条线 */
.dir-group :deep(.el-radio-button:not(:first-child) .el-radio-button__inner) { margin-left: -1px; }
.dir-group :deep(.el-radio-button.is-active .el-radio-button__inner) {
  border-color: var(--el-color-primary);
  background-color: var(--el-color-primary);
  color: var(--el-color-white);
  box-shadow: none;
}

.stage-wrap {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  border: 1px solid #e4e7ed; border-radius: 4px; overflow: hidden;
}
.status { float: left; margin-right: 12px; }
</style>

<style>
/* 弹窗主体默认有上下 padding 和最小高度，这里交给 .body 自己撑满，
   否则预览区会被挤出一条内部滚动条。 */
.print-dialog .el-dialog__body { padding: 16px; }
</style>
