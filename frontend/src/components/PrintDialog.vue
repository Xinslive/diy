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
              <el-option
                v-for="t in tplList" :key="t.id" :value="t.id"
                :label="`${t.name}（${paperOf(t)}${t.is_default ? ' · 默认' : ''}）`"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="纸型">
            <el-select v-model="paper" style="width: 100%" @change="onPaperChange">
              <el-option v-for="p in papers" :key="p.key" :value="p.key" :label="paperLabel(p.key)" />
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
        </el-form>

        <!-- 说明沉到栏底：设置项只有三行，撑不满 60vh 高的预览区，
             留一大片空在下面会让人以为左侧没加载出来。 -->
        <div class="note">
          <p class="note-tip">纸型与方向只对本次打印生效，不会改动模板。</p>
          <p v-if="isVoucher" class="note-sub">A4 每页 2 张凭证，A5 每页 1 张。</p>
          <p v-if="dirty" class="note-warn">纸型已与所选模板不同，按当前选择打印。</p>
        </div>
      </div>

      <div class="stage-wrap">
        <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon />
        <PrintPreview v-else :html="html" :paper="paper" :orientation="orientation" :papers="papers" />
      </div>
    </div>

    <template #footer>
      <span class="status">
        <el-tag v-if="loading" size="small" type="info" effect="plain">生成预览中…</el-tag>
      </span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="printing" @click="doPrint">
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
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Printer } from '@element-plus/icons-vue'
import PrintPreview from '@/components/PrintPreview.vue'
import { loadPrintHtml, openPrintWindow, printWindow, templateMeta, templates as loadTemplates } from '@/api/print'
import {
  DEFAULT_ORIENTATION, normalizePaper, paperLabel as labelPaper, pickPapers,
} from '@/utils/paper'

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
  title: { type: String, default: '打印' },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const templateId = ref(null)
const defaultTemplates = ref({})
const paper = ref('A4')
const orientation = ref('portrait')
const papers = ref([])
const tplList = ref([])
const html = ref('')
const error = ref('')
const loading = ref(false)
const printing = ref(false)

/** 选中模板自带的那份纸型，用来判断用户是不是临时改过。 */
const templatePaper = ref('')

/**
 * report.html 的两个 id 是反的：它的 template_id 指**报表**模板，
 * 版式模板走 print_template_id。其余四个接口都是 template_id 直接指版式。
 */
const templateKey = computed(() => (props.scope === 'REPORT' ? 'print_template_id' : 'template_id'))

function paperLabel(key) {
  return labelPaper(key, isVoucher.value)
}

const isVoucher = computed(() => props.scope === 'VOUCHER')
const dirty = computed(() => !!templatePaper.value && templatePaper.value !== paper.value)

/**
 * 模板的纸型以 content 为准：paper 列只是保存时顺手冗余的一份拷贝。
 * 落到界面上再收敛一次：模板是老的、纸型早就不是 A4/A5 时，兜回 A4，
 * 否则下拉框会停在一个选项列表里根本不存在的值上。
 */
function paperOf(t) {
  return normalizePaper(t?.content?.paper || t?.paper)
}
function orientationOf(t) {
  return t?.content?.orientation || 'portrait'
}

function buildParams() {
  const p = { ...props.params }
  if (templateId.value !== null) p[templateKey.value] = templateId.value
  if (paper.value) p.paper = paper.value
  if (orientation.value) p.orientation = orientation.value
  return p
}

async function init() {
  error.value = ''
  html.value = ''
  templateId.value = null
  try {
    // meta 是公开字典，缓存一份即可，不必每次开弹窗都拉。
    if (!papers.value.length) {
      const meta = await templateMeta(props.bookId)
      // 后端仍下发全部纸型，这里只留系统对外提供的两种。
      papers.value = pickPapers(meta?.papers || [])
      defaultTemplates.value = meta?.defaults || {}
    }
    tplList.value = (await loadTemplates(props.bookId)) || []
  } catch (e) {
    error.value = e?.response?.data?.detail || '打印模板加载失败'
    return
  }
  const list = tplList.value.filter((t) => t.scope === props.scope)
  const builtin = { id: 0, name: '内置默认', content: defaultTemplates.value[props.scope] || {} }
  tplList.value = [builtin, ...list]
  // 设计器传入的 0 明确指内置版式；其它入口仍优先使用账套默认模板。
  const def = tplList.value.find((t) => t.id === props.initialTemplateId) ||
    list.find((t) => t.is_default) || list[0] || builtin
  if (def) {
    // **必须等选项先渲染出来再赋值**。el-select 是靠 modelValue 去已注册的
    // 选项里反查 label 的；选项还没挂上就赋值，收起框里只能显示空。
    // 数据是异步回来的，这里天然会踩到，晚一拍 nextTick 就够了。
    await nextTick()
    templateId.value = def.id
    templatePaper.value = paperOf(def)
    paper.value = templatePaper.value
    orientation.value = orientationOf(def)
  }
  await refreshPreview()
}

function onTemplateChange() {
  const t = tplList.value.find((x) => x.id === templateId.value)
  if (!t) return
  templatePaper.value = paperOf(t)
  paper.value = templatePaper.value
  orientation.value = orientationOf(t)
  refreshPreview()
}

/** 换纸型就把方向带到该纸型的惯用值，否则 A5 沿用 A4 的纵向，横向小册子排不出来。 */
function onPaperChange() {
  orientation.value = DEFAULT_ORIENTATION[paper.value] || 'portrait'
  refreshPreview()
}

let token = 0
async function refreshPreview() {
  if (!props.path) return
  const mine = ++token
  loading.value = true
  const doc = await loadPrintHtml(props.path, buildParams())
  if (mine !== token) return                 // 过期响应直接丢弃，避免旧结果盖掉新的
  if (doc) {
    html.value = doc
    error.value = ''
  } else {
    error.value = '打印内容生成失败，请调整范围或模板后重试'
  }
  loading.value = false
}

async function doPrint() {
  printing.value = true
  try {
    // 重新取一次再打：拿到的字符串同时写进预览和打印窗口，
    // 「看到的就是打出来的」才不是一句空话。
    const doc = await loadPrintHtml(props.path, buildParams())
    if (!doc) {
      ElMessage.error('打印内容生成失败')
      return
    }
    html.value = doc
    error.value = ''
    printWindow(openPrintWindow(doc))
    visible.value = false
  } finally {
    printing.value = false
  }
}

watch(() => props.modelValue, (v) => { if (v) init() })
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
.note-tip { color: #909399; }
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