<template>
  <div class="pt">
    <!-- ── 顶部工具条 ────────────────────────────────────────────
         只有真正每天都要用的动作留在明面上（换范围、换模板、刷新、打印、保存），
         重命名/设为默认/删除这类低频动作收进「更多」，工具条因此不会被一排按钮撑满。 -->
    <header class="pt-bar">
      <div class="bar-l">
        <span class="pt-title">打印模板</span>
        <el-select v-model="scope" class="sel" @change="onScopeChange">
          <el-option v-for="(label, key) in SCOPES" :key="key" :label="label" :value="key" />
        </el-select>
        <el-select v-model="templateId" class="sel sel-tpl" @change="loadTemplate">
          <el-option v-if="showBuiltin" label="（内置默认）" :value="0" />
          <el-option v-for="t in listOfScope" :key="t.id" :label="tplLabel(t)" :value="t.id" />
        </el-select>
        <el-button :disabled="!canCreate" @click="doCreate"><el-icon><Plus /></el-icon>&nbsp;新建</el-button>
        <el-dropdown trigger="click" @command="onMore">
          <el-button title="更多操作"><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown>
            <!-- 每项的可用性各按各的判据（utils/printTemplate.js，与后端同一套）：
                 改「系统基线」或「我的私有副本」只要能写这个账套就行（改动只对我
                 一人可见），改「账套模板」要 OWNER，基线不能删。统一按
                 store.canWrite 一把梭的后果是：会计点的每一下都要吃一个后端 403。 -->
            <el-dropdown-menu>
              <el-dropdown-item command="rename" :disabled="!canEditSelected">
                <el-icon><EditPen /></el-icon>重命名
              </el-dropdown-item>
              <!-- 这里曾有两项：「复制一份」与「新建个人版式（只有你可见）」，
                   已按需求撤掉。两条都是「拿屏幕上的东西再生成一份模板」，而顶栏
                   的「新建」已经覆盖了这三件事里唯一还需要留的那件（建一份给同事
                   用的账套模板）—— 菜单里三选一反而让人先想「该点哪个」。
                   会计要的「只有我可见的版式」没少：选中系统基线直接保存即可，
                   后端写时复制成私有副本（见 utils/printTemplate.js 的说明）。 -->
              <el-dropdown-item command="default" :disabled="!canEditSelected" divided>
                <el-icon><Star /></el-icon>{{ isCurrentDefault ? '取消默认' : '设为默认' }}
              </el-dropdown-item>
              <el-dropdown-item command="delete" :disabled="!canDeleteSelected" divided>
                <el-icon><Delete /></el-icon>删除
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>

      <div class="bar-r">
        <el-tag v-if="previewing" size="small" type="info" effect="plain">渲染中…</el-tag>
        <el-tag v-else-if="dirty" size="small" type="warning" effect="plain">未保存</el-tag>

        <el-tooltip
          :content="guideMode
            ? '正在调整页边距，再次点击退出'
            : '打开后可在预览纸的四条边上直接拖动页边距'"
          placement="bottom"
        >
          <el-button :type="guideMode ? 'primary' : ''" :disabled="!canEditDraft" @click="toggleGuides">
            <el-icon><Crop /></el-icon>&nbsp;边距
          </el-button>
        </el-tooltip>

        <el-tooltip content="重新渲染预览" placement="bottom">
          <el-button @click="refreshPreview"><el-icon><Refresh /></el-icon></el-button>
        </el-tooltip>
        <el-button @click="doPrint"><el-icon><Printer /></el-icon>&nbsp;打印</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canEditSelected || !templateId" @click="save">
          保存
        </el-button>
      </div>
    </header>

    <!-- 只读提示：放在工具条下面一条，不占版面，也不挡着纸 -->
    <div v-if="!store.canWrite" class="ro-note">
      <el-icon><InfoFilled /></el-icon>
      当前账套是只读权限，可以预览和打印，但不能保存模板。
    </div>

    <div class="pt-body">
      <!-- ── 左栏检查器 ──────────────────────────────────────────
           默认展开；窗口窄到预览会被挤扁时自动收起。
           这是「预览不被挤小」的兜底 —— 不是靠给预览一个更宽的栅格，
           而是保证两侧永远不会同时占着宽度。 -->
      <aside v-show="railOpen" ref="rail" class="rail">
        <TemplateInspector
          v-model:active-tab="tab"
          :tpl="tpl"
          :is-voucher="isVoucher"
          :selected-id="selectedId"
          :can-write="canEditDraft"
          :paper-options="paperOptions"
          @change="onEdit"
          @select="selectedId = $event"
          @add-element="addElement"
        />
      </aside>

      <button class="rail-toggle" :class="{ closed: !railOpen }" :title="railOpen ? '收起检查器' : '展开检查器'"
              :aria-label="railOpen ? '收起检查器' : '展开检查器'" @click="toggleRail">
        <el-icon><DArrowLeft v-if="railOpen" /><DArrowRight v-else /></el-icon>
      </button>

      <!-- ── 预览台 ──────────────────────────────────────────────
           这一栏是页面的主角，占掉除检查器外的全部宽度。 -->
      <section class="stage-col">
        <div class="stage-opts">
          <template v-if="scope === 'DETAIL_LEDGER'">
            <label>科目</label>
            <el-select v-model="previewSubjectId" size="small" class="opt" @change="onPreviewSubjectChange">
              <el-option v-for="o in subjectOptions" :key="o.id" :label="o.label" :value="o.id" />
            </el-select>
            <template v-if="previewRangeSubjects.length">
              <label>科目起</label>
              <el-select v-model="previewFromSubjectId" size="small" class="opt" @change="onPreviewFromSubjectChange">
                <el-option v-for="o in rangeSubjectOptions" :key="o.id" :label="o.label" :value="o.id" />
              </el-select>
              <label>止</label>
              <el-select v-model="previewToSubjectId" size="small" class="opt" @change="onPreviewToSubjectChange">
                <el-option v-for="o in rangeSubjectOptions" :key="o.id" :label="o.label" :value="o.id" />
              </el-select>
            </template>
          </template>
          <template v-else-if="scope === 'REPORT'">
            <label>报表</label>
            <el-select v-model="previewReportId" size="small" class="opt opt-w" @change="refreshPreview">
              <el-option v-for="r in reports" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </template>

          <span class="spacer" />
          <span class="hint">
            <template v-if="isVoucher && canEditDraft">
              直接在纸上拖动控件即可改版式 · 双击文字就地编辑
            </template>
            <template v-else-if="isVoucher">当前模板预览</template>
            <template v-else>
              点纸上的标题、页眉、表、页脚可直接定位到对应设置
            </template>
          </span>
        </div>

        <div v-if="previewError" class="stage-err">
          <el-alert type="warning" :closable="false" show-icon :title="previewError" />
        </div>

        <div class="stage-host-wrap">
          <PrintStage
            :key="scope + ':' + templateId"
            ref="stage"
            :html="previewHtml"
            :paper="PAPER"
            :orientation="tpl.orientation"
            :papers="meta.papers || []"
            :margins="tpl.margins"
            :slots="isVoucher ? voucherSlots : 1"
            :editing="canEditDraft || !isVoucher"
            :error="previewError"
            :footer-format="tpl.footer?.format"
          >
            <template #tools>
              <div v-if="isVoucher && voucherSlots > 1 && canEditDraft" class="slot-switch" aria-label="当前编辑的凭证槽位">
                <button v-for="n in voucherSlots" :key="n" type="button"
                        :class="{ on: activeSlot === n - 1 }" :aria-pressed="activeSlot === n - 1"
                        @click="activeSlot = n - 1">第 {{ n }} 张</button>
              </div>
            </template>
            <template #default="{ page, content, slots: slotRects, hotspots: spots, scale: stageScale }">
              <!-- 凭证：元素框直接压在真预览上，能拖能缩 -->
              <VoucherCanvasLayer
                v-if="isVoucher && canEditDraft"
                :elements="tpl.elements"
                :columns="tpl.columns"
                :slot-rects="slotRects"
                :slots="voucherSlots"
                :active-slot="activeSlot"
                :selected-id="selectedId"
                :can-write="canEditDraft"
                :scale="stageScale"
                @change="onEdit"
                @select="selectElement"
              />

              <!-- 页边距：拖纸的四条边 -->
              <PaperGuides v-if="guideMode" :page="page" :content="content"
                           :margins="tpl.margins" :can-write="canEditDraft" :scale="stageScale"
                           @change="onEdit" />

              <!-- 非凭证：在真预览上量出来的可点区块 -->
              <template v-else-if="!isVoucher && spots?.length">
                <button v-for="h in spots" :key="h.key" type="button" class="hotspot"
                        :class="[h.key, { on: hotKey === h.key }]"
                        :style="{ left: h.x + 'px', top: h.y + 'px', width: h.w + 'px', height: h.h + 'px' }"
                        :title="HOT_CN[h.key] || ''" @click="focusHotspot(h.key)" />
              </template>
            </template>
          </PrintStage>
        </div>

        <div class="save-bar" :class="{ dirty }">
          <el-button @click="loadTemplate(templateId)" :disabled="!dirty">放弃改动</el-button>
          <span class="save-tip">{{ saveHint }}</span>
          <span class="spacer" />
          <el-button type="primary" :loading="saving" :disabled="!canEditSelected || !templateId || !dirty" @click="save">
            保存模板
          </el-button>
        </div>
      </section>
    </div>

    <PrintDialog v-model="printVisible" :book-id="store.bookId" :path="printPath"
                 :scope="scope" :params="printParams" :initial-template-id="templateId" :title="printTitle" />
  </div>
</template>

<script setup>
/**
 * 打印模板设计器。
 *
 * 这个页面重做过一次，方向只有一条：**预览是主角，编辑发生在预览上**。
 *
 * 上一版是「版式 | 预览 | 列」三栏并排，栅格写死 300px + 1fr + 520px。
 * 实际测出来中栏只剩 154px —— A4 的纸被压成邮票，两侧各占着固定宽度，
 * 谁也不肯让。用户要判断版式，版式却恰好是唯一看不清的那部分。
 *
 * 现在改成：预览吃掉除检查器外的全部宽度，检查器可以收起；
 * 凭证的控件框、页边距，都直接画在**真实渲染出来的那张纸**上 ——
 * 拖到哪里，打印出来就在哪里，不再有「示意图」和「实物」两套坐标。
 */
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Crop, DArrowLeft, DArrowRight, Delete, EditPen,
  InfoFilled, MoreFilled, Plus, Printer, Refresh, Star,
} from '@element-plus/icons-vue'
import PrintStage from '@/components/print/PrintStage.vue'
import VoucherCanvasLayer from '@/components/print/VoucherCanvasLayer.vue'
import PaperGuides from '@/components/print/PaperGuides.vue'
import TemplateInspector from '@/components/print/TemplateInspector.vue'
import PrintDialog from '@/components/PrintDialog.vue'
import * as printApi from '@/api/print'
import * as reportApi from '@/api/report'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { pickPapers, slotsPerPage } from '@/utils/paper'
import {
  effectiveDefaultId as sharedEffectiveDefault, showBuiltinTemplate,
  templateTags, canEditTemplate, canDeleteTemplate, canCreateTemplate,
} from '@/utils/printTemplate'

const clone = (o) => JSON.parse(JSON.stringify(o))

const store = useSessionStore()

/** 骨架：首屏渲染发生在模板数据回来之前，模板里到处是 tpl.margins.top 这类深层访问，
 *  必须给一份完整默认值，否则 setup 阶段就会抛 "reading 'top'"。 */
/** 纸张恒为 A4（见 utils/paper.js）。画布要一个具体的值来算纸面尺寸。 */
const PAPER = 'A4'

const EMPTY_TPL = {
  paper: PAPER,
  orientation: 'portrait',
  margins: { top: 10, right: 10, bottom: 10, left: 10 },
  title: { text: '', size: 16, bold: true, align: 'center' },
  subtitle: { text: '', size: 11, align: 'center' },
  font: { family: '宋体', size: 10 },
  show: { period: true, voucher_no: true, date: true, attachments: true, remark: true, page_no: true },
  meta: { company: true, book: true, period: true, accountant: true, checker: true, cashier: true,
          voucher_no: true, date: true, attachments: true, remark: true },
  columns: [{ key: 'summary', label: '摘要', width: 40, align: 'left', indent: 0 }],
  footer: { text: '', show_page_no: true, format: '第 {page} 页　共 {total} 页' },
  body: { repeat_header: true, border: true },
  voucher: { rows_per_page: 5, split_number: true, fill_rows: true,
             split_format: '{voucher_no} - {page}/{pages}' },
  elements: [],
}

const meta = ref({ papers: [], defaults: {} })
const scope = ref('VOUCHER')
const templateId = ref(0)
const list = ref([])
const tpl = ref(clone(EMPTY_TPL))

/** 检查器栏是否展开。展开时才占宽度，所以预览永远拿得到主体。 */
const railOpen = ref(true)
/** 边距拖拽模式。非凭证范围它就是唯一的直接编辑手段。 */
const guideMode = ref(false)
const stage = ref(null)
function toggleGuides() {
  guideMode.value = !guideMode.value
  if (guideMode.value) stage.value?.resetScroll()
}
/** 当前编辑第几个凭证槽位（一页两张时才分得出）。 */
const activeSlot = ref(0)
/** 画布上选中的凭证元素。 */
const selectedId = ref(null)
const inspectorTab = ref('page')

const subjects = ref([])
const reports = ref([])
const saving = ref(false)

const previewSubjectId = ref(null)
const previewFromSubjectId = ref(null)
const previewToSubjectId = ref(null)
const previewReportId = ref(null)
const previewHtml = ref('')
const previewError = ref('')
const previewing = ref(false)
const printVisible = ref(false)
let previewToken = 0

const SCOPES = {
  VOUCHER: '记账凭证', LEDGER: '总账', DETAIL_LEDGER: '明细账',
  BALANCE: '科目余额表', REPORT: '报表',
}
const HOT_CN = {
  title: '主标题', subtitle: '副标题', meta: '页眉信息',
  table: '数据表格', footer: '页脚',
}
/** 纸型只给系统对外提供的那一种，与打印弹窗共用同一份口径。 */
const paperOptions = computed(() => pickPapers(meta.value.papers || []))

const listOfScope = computed(() => list.value.filter((t) => t.scope === scope.value))

/** 生效的默认模板（口径见 utils/printTemplate.js，与打印弹窗共用一份）。 */
const effectiveDefaultId = computed(() => sharedEffectiveDefault(listOfScope.value))
/** 内置版式只在没有任何已保存模板时才是唯一选项 —— 凭证范围已有基线模板。 */
const showBuiltin = computed(() => showBuiltinTemplate(scope.value, listOfScope.value))
/** 当前这份模板。列表里找不到它时是 null（内置版式，或刚被删掉）。 */
const selectedTpl = computed(() => listOfScope.value.find((t) => t.id === templateId.value) || null)
/** 身份。判「能不能改哪一层」要用角色，光有 canWrite 分不出 OWNER 与会计。 */
const whoami = computed(() => ({ role: store.currentBook?.role || '', isAdmin: !!store.isAdmin }))
/** 改 / 删 / 新建各按各的判据（口径在 utils/printTemplate.js，与后端一致）。 */
const canEditSelected = computed(() => canEditTemplate(selectedTpl.value, whoami.value))
const canDeleteSelected = computed(() => canDeleteTemplate(selectedTpl.value, whoami.value))
const canCreate = computed(() => canCreateTemplate(whoami.value))
/**
 * 检查器/画布能不能编辑 —— 与「能不能存」分开两件事。
 *
 * 选的是「（内置默认）」时 selectedTpl 为 null（它是后端的兜底版式，不是某一行
 * 模板记录），canEditSelected 因此是 false。但这一档恰恰是最需要随手调的时候：
 * 总账/明细账/余额表/报表这四个范围眼下没有已保存模板，内置版式是**唯一**选项，
 * 照 canEditSelected 一刀切下去，这四个范围的检查器会整片变只读 —— 而实际上
 * 这份草稿随便调，只是存不进去（要存得先「新建」，见 saveHint）。
 *
 * 所以编辑看 canEditDraft，写库看 canEditSelected。
 */
const canEditDraft = computed(() => (templateId.value ? canEditSelected.value : store.canWrite))
/** 菜单那一句跟着 is_default 走 —— 它切换的就是这个标记本身，
    即便当前这份被同范围的另一份盖着，清掉它仍然是个有效动作。 */
const isCurrentDefault = computed(() => !!selectedTpl.value?.is_default)
/** 下拉里给生效的那一份加后缀。不加的话「默认」这件事在页面上完全不见：
    既不知道现在这份是不是默认，也不知道切过去的那份是不是。 */
const tplLabel = (t) => {
  // 只标「默认」（口径见 utils/printTemplate.js::templateTags）；私有副本不标。
  const tags = templateTags(t, effectiveDefaultId.value)
  return tags.length ? `${t.name}（${tags.join(' · ')}）` : t.name
}
const isVoucher = computed(() => scope.value === 'VOUCHER')
// 一页几张凭证读模板自己的 slots_per_page（缺省 2，与后端一致）。
// 画布按它切槽位、检查器按它算毫米 —— 两边必须读同一个字段，否则用户会看到
// 「检查器说一页一张、画布上却并排放两张」。
const voucherSlots = computed(() => slotsPerPage(tpl.value))
watch(voucherSlots, (slots) => { activeSlot.value = Math.min(activeSlot.value, slots - 1) })
const tab = computed({
  get: () => inspectorTab.value,
  set: (v) => { inspectorTab.value = v },
})

/** 预览期间跟顶栏走。顶栏已经有一个期间选择器，页面里再放一个只会选出两份不一样的期间。 */
const previewPeriodId = computed(() =>
  store.periodId || store.currentPeriod?.id || store.periods[store.periods.length - 1]?.id || null)
const fromPeriodId = ref(null)

/** 当前草稿是否与已保存内容不同 —— 用来提示「未保存」，免得以为已经存进去了。 */
const savedSnapshot = ref('')
const dirty = computed(() => !!templateId.value && JSON.stringify(tpl.value) !== savedSnapshot.value)

const saveHint = computed(() => {
  if (!templateId.value) return '内置默认版式，改动需要先新建模板才能保存'
  if (!store.canWrite) return '只读账套，改动无法保存'
  if (!canEditSelected.value) return '账套共用的模板，只有账套所有者或管理员能改'
  // 改基线时后端会写时复制成私有副本，但那是实现细节：底部这行只回答
  // 「我现在能不能存、存了会怎样」，而答案对两种模板都是一样的 —— 能存。
  // 原来这里对基线另说一句「保存后会变成只有你可见的个人版式」，
  // 每次改内置模板都要先被提醒一遍，用户并没有因此多做任何事。
  if (dirty.value) return '有未保存的改动'
  return '已是最新'
})

// ------------------------------------------------------------------ 编辑动作
/**
 * 编辑器里任何改动都走这里：防抖刷一次预览。
 *
 * 拖拽是每帧都在改坐标的，不防抖的话一次拖动能打出去几十个请求；
 * 完全不等又会让拖动看着发木。200ms 是这两者之间的平衡点。
 */
let previewTimer = null
function onEdit() {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, 200)
}

/** 新增元素：id 必须唯一，且父组件持有数组，所以在这里生成。 */
let seq = 0
function addElement(kind) {
  if (!canEditDraft.value) return
  const id = 'el-' + Date.now().toString(36) + '-' + (seq++)
  const base = { id, x: 10, y: 10, w: 40, h: 6, font_size: 9, bold: false,
                 align: 'left', visible: true, border: true }
  const el = { ...base, type: 'text', bind: 'custom', text: '新文本' }
  if (kind === 'table') Object.assign(el, { type: 'table', bind: 'entries', text: '', x: 3, y: 24, w: 94, h: 55 })
  if (kind === 'line') {
    Object.assign(el, { type: 'line', bind: 'custom', text: '', x: 5, y: 50, w: 90, h: 1,
                        line_width: 0.3, line_style: 'solid' })
  }
  if (kind === 'rectangle') {
    Object.assign(el, { type: 'rectangle', bind: 'custom', text: '', x: 20, y: 30, w: 50, h: 25,
                        line_width: 0.3, line_style: 'solid' })
  }
  if (!Array.isArray(tpl.value.elements)) tpl.value.elements = []
  tpl.value.elements.push(el)
  selectedId.value = id
  inspectorTab.value = 'elements'
  onEdit()
}

function selectElement(id) {
  selectedId.value = id
  if (id) inspectorTab.value = 'elements'
}

/** 在真预览上点了某个区块 → 切到对应页签并把那个控件滚到眼前。 */
const hotKey = ref('')
function focusHotspot(key) {
  hotKey.value = key
  railOpen.value = true
  inspectorTab.value = 'page'
  nextTick(() => {
    const el = document.querySelector(`.ti [data-field="${key}"]`)
    if (el) {
      // 只滚动检查器；scrollIntoView 会把外层隐藏溢出的区域也滚走，连纸面都偏移。
      const inspector = el.closest('.ti')
      if (inspector) {
        const target = el.getBoundingClientRect()
        const viewport = inspector.getBoundingClientRect()
        inspector.scrollTo({
          top: inspector.scrollTop + target.top - viewport.top - (viewport.height - target.height) / 2,
          behavior: 'smooth',
        })
      }
      el.querySelector('input, textarea')?.focus({ preventScroll: true })
    }
  })
}

/** 删除控件后清掉悬空选中态，避免左栏显示旧属性而画布已经没有对应框。 */
watch(
  () => (tpl.value.elements || []).map((el) => el.id),
  (ids) => {
    if (selectedId.value && !ids.includes(selectedId.value)) selectedId.value = null
  },
)

// ------------------------------------------------------------------ 数据
async function loadTemplates() {
  list.value = await printApi.templates(store.bookId)
}

async function onScopeChange() {
  // 换范围等于换一套版式模型：凭证有元素层，账簿报表没有。
  // 停在「元素」页签上只会看到一片空白，所以主动收回收面板页。
  if (!isVoucher.value) inspectorTab.value = 'page'
  selectedId.value = null
  activeSlot.value = 0
  guideMode.value = false
  const hit = listOfScope.value.find((t) => t.is_default) || listOfScope.value[0]
  templateId.value = hit?.id || 0
  await loadTemplate(templateId.value)
}

async function loadTemplate(id) {
  clearTimeout(previewTimer)
  const base = id
    ? (await printApi.templateDetail(store.bookId, id)).content
    : clone(meta.value.defaults[scope.value] || EMPTY_TPL)
  tpl.value = clone({ ...clone(EMPTY_TPL), ...base })
  selectedId.value = null
  activeSlot.value = 0
  normalize()
  migrateVoucherVisibility()
  // 等列宽归一化完再取快照，否则「刚打开就显示未保存」是假警报
  savedSnapshot.value = JSON.stringify(tpl.value)
}

/**
 * 凭证的显隐统一交给画布元素的 visible 开关（后端 _voucher_element_html 只认它）。
 * 老模板把开关存在 meta 里，载入时折进 visible，再把 meta 全部置 true，
 * 免得留下一个界面上已经关不掉的「隐藏」。
 */
function migrateVoucherVisibility() {
  if (!isVoucher.value) return
  if (!Array.isArray(tpl.value.elements)) tpl.value.elements = []
  const meta = tpl.value.meta || {}
  const show = tpl.value.show || {}
  for (const el of tpl.value.elements) {
    const bind = el.bind
    const enabled = meta[bind] !== false &&
      (!['period', 'voucher_no', 'date', 'attachments', 'remark'].includes(bind) || show[bind] !== false) &&
      (bind !== 'footer' || show.page_no !== false)
    el.visible = el.visible !== false && enabled
  }
  for (const key of Object.keys(meta)) meta[key] = true
  for (const key of Object.keys(show)) show[key] = true
}

/** 把本地改动对齐到「可提交」的形态：列宽归一化、meta/show 补全 */
const round = (n) => Math.round(n * 10000) / 10000
function normalize() {
  const cols = tpl.value.columns || []
  const total = cols.reduce((s, c) => s + (Number(c.width) || 0), 0) || 1
  cols.forEach((c) => { c.width = round((Number(c.width) || 0) / total * 100) })
  if (!Array.isArray(tpl.value.elements)) tpl.value.elements = []
  if (!tpl.value.voucher) tpl.value.voucher = { rows_per_page: 5, split_number: true, fill_rows: true }
  if (!tpl.value.meta) tpl.value.meta = {}
  if (!tpl.value.show) tpl.value.show = {}
  schedulePreview()
}

function schedulePreview(delay = 250) {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, delay)
}

async function loadPreviewData() {
  // **年度必带**：科目表每个年度各有一份（后端迁移 0032）
  const tree = await subjectApi.subjectTree(
    store.bookId, { include_inactive: true, year: store.currentYear })
  const flatten = (nodes, parentId = null) => nodes.flatMap((node) => {
    const item = { ...node, parent_id: parentId }
    return [item, ...flatten(node.children || [], node.id)]
  })
  subjects.value = flatten(tree)
  if (!previewSubjectId.value || !subjects.value.some((s) => s.id === previewSubjectId.value)) {
    previewSubjectId.value = subjects.value.find((s) => s.is_leaf && s.active)?.id || subjects.value[0]?.id || null
  }
  resetPreviewSubjectRange()
  // 必须带 book_id：/api/reports/templates 挂着 get_my_book，而它把 book_id
  // 声明成必填的 query 参数（端点签名上那个 Optional[int] 是骗人的，依赖照样
  // 强制要求）。漏掉它就是 422 Field required，整个预览面板跟着挂。
  reports.value = await reportApi.templates(store.bookId)
  if (!previewReportId.value && reports.value.length) previewReportId.value = reports.value[0].id
}

function subjectLabel(s) {
  const indent = '　'.repeat(Math.max(0, s.level - 1))
  const kind = s.children?.length ? '（综合）' : ''
  const inactive = s.active ? '' : '（停用）'
  return `${indent}${s.code} ${s.name}${kind}${inactive}`
}

function resetPreviewSubjectRange() {
  previewFromSubjectId.value = previewRangeSubjects.value[0]?.id ?? null
  previewToSubjectId.value = previewRangeSubjects.value.at(-1)?.id ?? null
}

const selectedPreviewSubject = computed(() => subjects.value.find((s) => s.id === previewSubjectId.value))
const previewRangeSubjects = computed(() => selectedPreviewSubject.value?.children || [])

/**
 * 下拉选项的 label 提前算好，别留在模板里。
 *
 * 以前 `v-for="s in subjects" :label="subjectLabel(s)"` —— 每渲染一次，
 * 每个选项就现拼一遍全角缩进（''.repeat）。这张页面渲染得并不少（拖一下纸
 * 预览就重渲染一次），科目一多就是几百次字符串拼接 × 每帧。
 *
 * 起止两个下拉更该合：它们是**同一批**子科目，label 各写了一份一模一样的
 * 模板串，算两遍。现在共用 rangeSubjectOptions 这一份。
 */
const subjectOptions = computed(() => subjects.value.map((s) => ({ id: s.id, label: subjectLabel(s) })))
const rangeSubjectOptions = computed(
  () => previewRangeSubjects.value.map((s) => ({ id: s.id, label: `${s.code} ${s.name}` }))
)

function onPreviewSubjectChange() {
  resetPreviewSubjectRange()
  refreshPreview()
}

function onPreviewFromSubjectChange() {
  const start = previewRangeSubjects.value.findIndex((s) => s.id === previewFromSubjectId.value)
  const end = previewRangeSubjects.value.findIndex((s) => s.id === previewToSubjectId.value)
  if (start > end) previewToSubjectId.value = previewFromSubjectId.value
  refreshPreview()
}

function onPreviewToSubjectChange() {
  const start = previewRangeSubjects.value.findIndex((s) => s.id === previewFromSubjectId.value)
  const end = previewRangeSubjects.value.findIndex((s) => s.id === previewToSubjectId.value)
  if (end < start) previewFromSubjectId.value = previewToSubjectId.value
  refreshPreview()
}

/**
 * 预览请求体：带上**当前未保存的草稿**。
 *
 * 这是「拖一下纸就变」的全部秘密 —— 预览不吃已保存的模板（那要先点保存，
 * 还得刷新），而是把编辑器里这份 JSON 直接发给后端渲染一遍。
 */
function previewPayload() {
  return {
    scope: scope.value,
    content: tpl.value,
    period_id: previewPeriodId.value,
    from_period_id: fromPeriodId.value || undefined,
    subject_id: previewSubjectId.value || undefined,
    from_subject_id: previewRangeSubjects.value.length ? previewFromSubjectId.value : undefined,
    to_subject_id: previewRangeSubjects.value.length ? previewToSubjectId.value : undefined,
    report_template_id: previewReportId.value || undefined,
  }
}

async function refreshPreview() {
  if (!store.bookId) return
  const token = ++previewToken
  previewing.value = true
  try {
    const html = await printApi.previewHtml(store.bookId, previewPayload())
    if (token !== previewToken) return              // 过期响应直接丢弃，避免旧结果盖掉新的
    previewHtml.value = html
    previewError.value = ''
  } catch (e) {
    if (token !== previewToken) return
    const detail = e.response?.data?.detail
    // 草稿处于半成品状态（列全删了、科目没选）是正常编辑过程，
    // 就地显示原因即可，不该弹一个红色报错打断。
    previewError.value = typeof detail === 'string' ? detail : '预览加载失败'
  } finally {
    if (token === previewToken) previewing.value = false
  }
}

/**
 * 正式打印：与预览**同一个范围、同一个期间**，只是模板换成已保存的那份。
 *
 * 打印走已保存的版式而不是编辑器里的草稿，是因为打印窗口里的东西可能被人另存为 PDF
 * 发出去 —— 必须对应一条真正存了档的版式。所以这里**只交范围**，
 * print_template_id 交给弹窗：它选的也是模板表里已保存的那些行，草稿压根进不了这条路。
 */
const printTarget = computed(() => {
  const pid = previewPeriodId.value
  const b = store.bookId
  switch (scope.value) {
    case 'LEDGER':
      return { path: `/books/${b}/print/general-ledger.html`,
               params: { period_id: pid, from_period_id: fromPeriodId.value || undefined } }
    case 'DETAIL_LEDGER':
      return { path: `/books/${b}/print/detail-ledger.html`,
               params: { period_id: pid, from_period_id: fromPeriodId.value || undefined,
                 subject_id: previewSubjectId.value,
                 from_subject_id: previewRangeSubjects.value.length ? previewFromSubjectId.value : undefined,
                 to_subject_id: previewRangeSubjects.value.length ? previewToSubjectId.value : undefined } }
    case 'BALANCE':
      return { path: `/books/${b}/print/balance.html`, params: { period_id: pid } }
    case 'REPORT':
      return { path: `/books/${b}/print/report.html`,
               params: { period_id: pid, template_id: previewReportId.value } }
    default:
      return { path: `/books/${b}/print/vouchers.html`, params: { period_id: pid } }
  }
})
const printPath = computed(() => printTarget.value.path)
const printParams = computed(() => printTarget.value.params)
const printTitle = computed(() => `打印${SCOPES[scope.value]}`)

function doPrint() {
  if (dirty.value) ElMessage.info('当前改动尚未保存，打印使用模板已保存的版本')
  printVisible.value = true
}

// ------------------------------------------------------------------ 模板增删改存

/**
 * 写完之后把「选中的模板」对齐到服务端返回的那一份。
 *
 * **id 真的会变**：目标是系统基线时，后端先把基线整份复制成只有我可见的私有
 * 副本，改动落在副本上（见 api/printing.py::update_template），返回的是副本的
 * 新 id。不跟着切的后果是下一次保存又拿旧 id（基线）再复制一份，攒出一串同名
 * 私有副本，而真正在改的那一份没人选中 —— 表现是「改了没生效」。
 */
async function applyWriteResult(res, { relaunch = true } = {}) {
  const forked = !!res.forked
  if (res.id && res.id !== templateId.value) templateId.value = res.id
  await loadTemplates()
  if (relaunch) await loadTemplate(templateId.value)
  return forked
}

async function save() {
  if (!canEditSelected.value || !templateId.value) return
  saving.value = true
  try {
    const res = await printApi.updateTemplate(store.bookId, templateId.value, { content: tpl.value })
    await applyWriteResult(res)
    ElMessage.success('模板已保存')
  } finally {
    saving.value = false
  }
}

/**
 * 改模板名。名字只存在数据库里，跟版式内容无关 ——
 * 改名不该动版式，也不该把「未保存」这个提示点出来（它只比对 tpl）。
 */
async function doRename() {
  if (!canEditSelected.value || !templateId.value) return
  const cur = selectedTpl.value
  if (!cur) return
  let name
  try {
    ;({ value: name } = await ElMessageBox.prompt('模板名称', '重命名打印模板', {
      inputValue: cur.name,
      inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
    }))
  } catch {
    return                       // 点了取消
  }
  const next = name.trim()
  if (next === cur.name) return
  const res = await printApi.updateTemplate(store.bookId, cur.id, { name: next })
  await applyWriteResult(res, { relaunch: false })
  ElMessage.success(`已改名为「${next}」`)
}

/**
 * 顶栏「新建」：把屏幕上这份版式另存为**账套模板**（同账套的同事也看得见），
 * 所以门禁是 OWNER（后端 `_require_global_template_write`）。
 *
 * 这里**不传 `private`**。后端 `TemplateIn.private` 默认 False（fail-closed），
 * 不传就是账套模板 —— 少一个字段就少一处能出错的地方，要私有的那条路不在这里。
 */
async function doCreate() {
  if (!canCreate.value) return
  let name
  try {
    ;({ value: name } = await ElMessageBox.prompt('新模板名称', '新建打印模板', {
      inputValue: `${SCOPES[scope.value]} 模板`,
      inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
    }))
  } catch {
    return
  }
  const t = await printApi.createTemplate(store.bookId, {
    scope: scope.value,
    name: name.trim(),
    content: clone(tpl.value),
    is_default: false,
  })
  await loadTemplates()
  templateId.value = t.id
  await loadTemplate(t.id)
  ElMessage.success('已基于当前版式新建')
}

async function doDelete() {
  if (!canDeleteSelected.value || !templateId.value) return
  try {
    await ElMessageBox.confirm('删除后无法恢复，确定删除该打印模板吗？', '删除模板', { type: 'warning' })
  } catch {
    return
  }
  await printApi.deleteTemplate(store.bookId, templateId.value)
  ElMessage.success('已删除')
  await loadTemplates()
  templateId.value = listOfScope.value[0]?.id || 0
  await loadTemplate(templateId.value)
}

/**
 * 设为默认 / 取消默认。
 *
 * 默认模板是**不指定模板直接打印时**兜底取的那一份（见 api/printing.py::_template），
 * 所以这是一次会影响此后每一次打印的选择 —— 设之前说清楚会发生什么，
 * 而不是让人点完才发现以后所有凭证都换了版式。
 *
 * 只发 is_default，不带 content：改名和设默认都不该顺带把版式重写一遍。
 */
async function doToggleDefault() {
  if (!canEditSelected.value || !templateId.value) return
  const cur = selectedTpl.value
  if (!cur) return
  const next = !cur.is_default
  if (next) {
    try {
      await ElMessageBox.confirm(
        `设为默认后，不指定模板直接打印${SCOPES[scope.value]}时会用这一份版式。`,
        `将「${cur.name}」设为默认模板`, { type: 'warning' })
    } catch {
      return                        // 点了取消
    }
  }
  const res = await printApi.updateTemplate(store.bookId, cur.id, { is_default: next })
  await applyWriteResult(res, { relaunch: false })
  ElMessage.success(`「${cur.name}」${next ? '设为默认模板' : '取消默认'}`)
}

function onMore(cmd) {
  if (cmd === 'rename') doRename()
  else if (cmd === 'default') doToggleDefault()
  else if (cmd === 'delete') doDelete()
}

// ------------------------------------------------------------------ 生命周期
onMounted(async () => {
  meta.value = await printApi.templateMeta(store.bookId)
  await loadTemplates()
  await loadPreviewData()
  templateId.value = listOfScope.value.find((t) => t.is_default)?.id || listOfScope.value[0]?.id || 0
  await loadTemplate(templateId.value)
})

// 顶栏切期间，预览跟着走 —— 期间不再是页面里的独立控件了
watch(() => previewPeriodId.value, () => schedulePreview(0))

watch(() => store.bookId, async () => {
  if (store.bookId) {
    await loadTemplates()
    await loadPreviewData()
    await loadTemplate(templateId.value)
  }
})

/**
 * 窗口窄了就自动收起检查器。
 *
 * 这不是「响应式小屏适配」那种锦上添花 —— 这个页面的判断依据只有一个：
 * 预览必须看得清。两边同时展开而中间只剩一条缝的时候，
 * 自动收起左栏比让用户自己发现「预览怎么这么小」要靠谱得多。
 *
 * 但用户**手动**收过之后就别再自动展开：每 resize 一次就弹回来一次，比不响应还烦。
 */
let userToggled = false
function syncRail() {
  if (window.innerWidth < 1120) railOpen.value = false
  else if (!userToggled) railOpen.value = true
}
function toggleRail() {
  userToggled = true
  railOpen.value = !railOpen.value
}
onMounted(() => { syncRail(); window.addEventListener('resize', syncRail) })
onBeforeUnmount(() => {
  clearTimeout(previewTimer)
  window.removeEventListener('resize', syncRail)
})
</script>

<style scoped>
.pt {
  --rail-w: 320px;
  display: flex; flex-direction: column;
  /* 撑满 .content（父级是 flex:1 + min-height:0，有确定高度，100% 才成立）。
     这个页面不再随文档滚动：预览台自己滚，滚动条留在纸的四周。 */
  height: 100%; min-height: 0;
  background: #fff; overflow: hidden;
}

/* ── 顶部工具条 ───────────────────────────────────────────── */
.pt-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px;
  padding: 10px 16px; border-bottom: 1px solid var(--cwq-hairline, #e8ebf0);
  background: #fff; flex: none;
}
.bar-l, .bar-r { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.bar-r { margin-left: auto; }
.pt-title { font-size: 15px; font-weight: 600; color: var(--cwq-chrome-strong, #1f2733); }
.sel { width: 132px; }
.sel-tpl { width: 190px; }

.ro-note {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 16px; font-size: 13px; color: #92400e;
  background: #fffbeb; border-bottom: 1px solid #fde68a; flex: none;
}

/* ── 主体两栏 ─────────────────────────────────────────────── */
.pt-body { flex: 1; min-height: 0; display: flex; position: relative; }

.rail {
  width: var(--rail-w); flex: none; min-width: 0;
  border-right: 1px solid var(--cwq-hairline, #e8ebf0);
  background: #fbfcfe; overflow: hidden;
}

.rail-toggle {
  position: absolute; left: var(--rail-w); top: 50%; z-index: 20;
  transform: translate(-1px, -50%);
  width: 18px; height: 52px; padding: 0;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--cwq-hairline, #e8ebf0); border-left: none;
  border-radius: 0 8px 8px 0; background: #fff; color: var(--cwq-chrome-text, #5b6472);
  cursor: pointer; box-shadow: 2px 0 8px rgba(15, 23, 42, .06);
}
.rail-toggle.closed { left: 0; }
.rail-toggle:hover { color: var(--cwq-chrome-active, #2563eb); }

/* ── 预览台 ───────────────────────────────────────────────── */
.stage-col { flex: 1; min-width: 0; display: flex; flex-direction: column; }

.stage-opts {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px; flex: none;
  padding: 8px 16px; border-bottom: 1px solid var(--cwq-hairline, #e8ebf0);
  background: #fff;
}
.stage-opts label { font-size: 13px; color: var(--cwq-chrome-text, #5b6472); }
.stage-opts .spacer, .save-bar .spacer { flex: 1; }
.opt { width: 170px; }
.opt-w { width: 190px; }
.hint { font-size: 12px; color: var(--cwq-muted, #909399); }

.stage-host-wrap { flex: 1; min-height: 0; }
.stage-err { padding: 12px 16px 0; }

/* ── 保存条 ───────────────────────────────────────────────── */
.save-bar {
  display: flex; align-items: center; gap: 12px; flex: none;
  padding: 9px 16px; border-top: 1px solid var(--cwq-hairline, #e8ebf0);
  background: #fff;
}
.save-tip { font-size: 12px; color: var(--cwq-muted, #909399); }
.save-bar.dirty .save-tip { color: var(--cwq-warn, #d97706); font-weight: 500; }

/* ── 画布上的零碎控件（VoucherCanvasLayer / PaperGuides 的兄弟样式） ── */
.slot-switch { display: flex; gap: 4px; }
.slot-switch button {
  border: 1px solid var(--cwq-hairline, #e8ebf0); background: rgba(255, 255, 255, .95);
  border-radius: 4px; padding: 2px 9px; font-size: 11px; cursor: pointer;
  color: var(--cwq-chrome-text, #5b6472);
}
.slot-switch button.on { background: var(--cwq-chrome-active, #2563eb); color: #fff; border-color: transparent; }

/* 预览上量出来的可点区块：平时隐形，悬停才浮出虚线 */
.hotspot {
  position: absolute; padding: 0; background: transparent;
  border: 1px solid transparent; border-radius: 3px; cursor: pointer;
}
.hotspot:hover, .hotspot.on {
  border-color: rgba(37, 99, 235, .55); background: rgba(37, 99, 235, .07);
}
.hotspot.on { border-style: dashed; }

@media (max-width: 1120px) {
  .pt { --rail-w: 300px; }
}
</style>
