<template>
  <el-dialog
    v-model="visible"
    title="从 Excel 导入期初余额"
    :width="dialogWidth"
    :close-on-click-modal="false"
    @closed="reset"
  >
    <!-- 第 1 步：选文件 -->
    <div v-if="step === 'pick'">
      <el-upload
        drag
        :auto-upload="false"
        :show-file-list="false"
        accept=".xls,.xlsx"
        :on-change="onPick"
        class="upload-box"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">把科目余额表拖到此处，或<em>点击选择文件</em></div>
        <template #tip>
          <div class="el-upload__tip">支持用友导出的 .xls 余额表；.xlsx 同样支持</div>
        </template>
      </el-upload>
      <div v-if="file" class="selected-file">
        <el-icon><Document /></el-icon>
        <span>{{ file.name }}</span>
      </div>
    </div>

    <!-- 第 2 步：解析预览。导入**不再是一个动作**，而是一次看得见的检查 ——
         以前传完直接往表格里填，用户不知道读进来的是不是自己那份文件 -->
    <div v-else-if="step === 'preview'">
      <div class="summary-grid">
        <div><span class="muted">文件</span><b class="ellipsis">{{ fileName }}</b></div>
        <div><span class="muted">识别来源</span><b>{{ result.source || '未识别' }}</b></div>
        <div><span class="muted">期间</span><b>{{ result.period_label || '未标注' }}</b></div>
      </div>

      <!-- 期初 / 期末：用友余额表两种数在同一张表里，必须让人自己指认是哪一份 -->
      <div v-if="result.has_closing" class="basis-row">
        <span class="basis-label">取哪一份数</span>
        <el-radio-group v-model="basis" size="small" @change="reparse">
          <el-radio-button label="opening">期初余额</el-radio-button>
          <el-radio-button label="closing">期末余额</el-radio-button>
        </el-radio-group>
        <span v-if="reparsing" class="muted">重新解析中…</span>
      </div>

      <div class="stat-row">
        <div class="stat is-ok"><b>{{ Number(result.matched) || 0 }}</b><span>读入</span></div>
        <div class="stat" :class="{ 'is-warn': result.unmatched }">
          <b>{{ Number(result.unmatched) || 0 }}</b><span>科目不存在</span>
        </div>
        <div class="stat"><b>{{ Number(result.skipped) || 0 }}</b><span>跳过行</span></div>
      </div>

      <el-alert
        v-if="mismatchText"
        type="error"
        :closable="false"
        show-icon
        class="mb"
        title="这份余额表和本账套的科目表对不上"
      >
        {{ mismatchText }}
      </el-alert>

      <el-alert
        v-if="!balanced"
        type="error"
        :closable="false"
        show-icon
        class="mb"
        title="这份余额表借贷不平"
      >
        借方合计 {{ result.total_debit }}，贷方合计 {{ result.total_credit }}，
        差额 <b>{{ money(diff) }}</b>。导入后表格会照填，但<b>保存期初余额时会被后端拒绝</b>，
        请先查这份文件是不是漏了一行。
      </el-alert>
      <el-alert v-else type="success" :closable="false" show-icon class="mb">
        借贷平衡：借方合计 {{ result.total_debit }} = 贷方合计 {{ result.total_credit }}
      </el-alert>

      <el-alert v-if="allErrors.length" type="warning" :closable="false" show-icon class="mb">
        <template #title>跳过 {{ allErrors.length }} 处</template>
        <ul class="error-list">
          <li v-for="(err, i) in shownErrors" :key="i">{{ err }}</li>
        </ul>
        <el-button v-if="allErrors.length > ERROR_PAGE" link type="primary" class="more-btn" @click="moreErrors = !moreErrors">
          {{ moreErrors ? '收起' : `展开其余 ${allErrors.length - ERROR_PAGE} 条` }}
        </el-button>
      </el-alert>

      <el-table :data="previewRows" size="small" border max-height="300" class="preview-table">
        <el-table-column prop="line" label="行号" width="64" align="center" />
        <el-table-column prop="code" label="编码" width="110" />
        <el-table-column prop="name" label="名称" min-width="170" show-overflow-tooltip />
        <el-table-column label="金额" width="130" align="right">
          <template #default="{ row }">{{ row.amount }}</template>
        </el-table-column>
        <el-table-column label="方向" width="60" align="center">
          <template #default="{ row }">{{ sideOf(row) }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="140">
          <template #default="{ row }">
            <span v-if="row.error" class="row-error">{{ row.error }}</span>
            <span v-else class="muted">匹配到科目</span>
          </template>
        </el-table-column>
        <template #empty><div class="muted">没有读到任何科目行</div></template>
      </el-table>
      <div class="table-note muted">
        表里只列前 {{ previewRows.length }} 行；确认后按文件完整读入 {{ Number(result.matched) || 0 }} 条。
      </div>
    </div>

    <!-- 第 3 步：完成。这里必须说清「还没落库」——
         填进页面表格 ≠ 存进库里，差一次「保存期初余额」 -->
    <div v-else class="success-state">
      <el-result icon="success" title="已填入期初余额表格">
        <template #sub-title>
          {{ summaryText }}。<br />
          这些数目前只在页面表格里，<b>回到页面点「保存期初余额」才写进账</b>。
        </template>
      </el-result>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button v-if="step === 'pick'" type="primary" :disabled="!file" :loading="parsing" @click="doParse">
        <el-icon><View /></el-icon>&nbsp;解析并预览
      </el-button>
      <el-button v-if="step === 'preview'" type="primary" :disabled="!canConfirm" :loading="confirming" @click="confirm">
        确认导入
      </el-button>
      <el-button v-if="step === 'done'" type="primary" @click="visible = false">知道了</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, UploadFilled, View } from '@element-plus/icons-vue'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import { balanceDiff, importSummaryText, mismatchHint } from '@/utils/bookInit'
import { moneyText } from '@/utils/format'
import { moneyCents } from '@/utils/amountGrid'

const props = defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue', 'imported'])

const store = useSessionStore()
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/** 一次展开的错误条数。超过这个数要让人点开 —— 一屏几十条红字没人读得下去 */
const ERROR_PAGE = 10

const step = ref('pick')
const file = ref(null)
const fileName = ref('')
const basis = ref('opening')
const parsing = ref(false)
const reparsing = ref(false)
const confirming = ref(false)
const moreErrors = ref(false)
const result = ref({ rows: [], errors: [] })

const PREVIEW_ROWS = 200
const allErrors = computed(() => result.value.errors || [])
const shownErrors = computed(() => moreErrors.value ? allErrors.value : allErrors.value.slice(0, ERROR_PAGE))
const previewRows = computed(() => (result.value.rows || []).slice(0, PREVIEW_ROWS))
const balanced = computed(() => result.value.balanced !== false)
const diff = computed(() => balanceDiff(result.value))
const matched = computed(() => Number(result.value.matched) || 0)
/** 一条都没匹配上时没什么可导入的，按钮就该灰着 */
const canConfirm = computed(() => matched.value > 0)
const summaryText = computed(() => importSummaryText(result.value))
/** 「科目不存在」多到不正常 —— 多半是初始化第 1 步选错了科目表来源 */
const mismatchText = computed(() => mismatchHint(result.value))

/** 方向优先用后端给的 display_side（文件里已经定过的口径），没有才按 direction 推 */
const sideOf = (row) => row.display_side
  || (row.direction === 'CREDIT' ? '贷' : row.direction === 'DEBIT' ? '借' : '—')

/**
 * 金额文字：带千分位、两位小数。
 *
 * 以前是 `(Number(v || 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2 })` ——
 * 两个毛病：
 *   ① **浮点**：18 位整数在浮点里已经不是原来那个数了；
 *   ② `maximumFractionDigits` 没给，默认是 3 —— 于是 1.005 显示成 **"1.005"**，
 *      而期初余额页显示的是 "1.00"：**同一个业务对象，两个界面，两个数**。
 *
 * 现在统一走**整数分**（BigInt）+ utils/format.js 的 moneyText ——
 * 那是期初余额页、凭证页共用的同一份格式规则：定点到分、两位小数、带千分位。
 */
const money = (v) => moneyText(moneyCents(v))

/** 预览步得宽：那几列里除「名称」「说明」都是定宽，低于 780px 就要横向滚动 */
const dialogWidth = 'min(820px, calc(100vw - 32px))'

function onPick(uploadFile) {
  const name = uploadFile.raw?.name || uploadFile.name || ''
  // accept 只是给文件选择框的建议，拖进来的文件不经过它 —— 这里再拦一道，
  // 否则拖一个 .txt 进来，报错来自后端而不是这里，用户看不懂
  if (!/\.xlsx?$/i.test(name)) {
    ElMessage.warning('请选择 .xls 或 .xlsx 余额表')
    return
  }
  file.value = uploadFile.raw
  fileName.value = name
}

/**
 * 传同一个文件重新解析，取 basis 那一份数。结果只进内存，不落库。
 *
 * **年度必须一起带上**（`?year=`）：期初挂在**某一年的科目表**上，
 * 不说清是哪一年，后端只能拿「账套当前在记的那一年」去认科目 ——
 * 那是**另一张表**：解析出来的每一行都指着另一年的科目 id，
 * 页面上的科目对不上，一个都填不进去，表格于是全是 0，
 * 而人看到的只是「导入没生效」（2026-10-31 生产实测）。
 *
 * 这一页是期初余额页的一部分，它跟着顶栏的年度走 —— 这里就取同一个口径。
 */
async function parseFile(b = basis.value) {
  const fd = new FormData()
  fd.append('file', file.value)
  result.value = await api.parseOpenings(store.bookId, fd, b, store.currentYear)
  step.value = 'preview'
}

async function doParse() {
  if (!file.value) return
  parsing.value = true
  try {
    await parseFile()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '解析失败')
  } finally {
    parsing.value = false
  }
}

/**
 * 切换期初 / 期末要**重新解析同一个文件**。
 *
 *  两份数在用友余额表里是不同的列，不是同一份结果的两种显示 ——
 *  拿旧结果换个标签就交出去，等于把一份期末数当成期初数写进账。
 */
async function reparse(b) {
  reparsing.value = true
  moreErrors.value = false
  try {
    await parseFile(b)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '解析失败')
  } finally {
    reparsing.value = false
  }
}

async function confirm() {
  // 借贷不平放行 —— 文件是用户自己的，可能他清楚差在哪（例如差的就是还没录的部分）。
  // 但必须让他知道**保存时会怎样**，否则他填完、看着表格是平的、点保存才吃后端一句拒绝。
  if (!balanced.value) {
    try {
      await ElMessageBox.confirm(
        `这份余额表借贷不平，差额 ${money(diff.value)}。`
        + '导入后页面表格会照填，但点「保存期初余额」时会被后端拒绝。请确认你清楚差额来自哪里。',
        '确认导入一份不平的余额表？',
        { type: 'warning', confirmButtonText: '我清楚，仍然导入', cancelButtonText: '返回检查' }
      )
    } catch {
      return
    }
  }
  confirming.value = true
  try {
    // 只把解析结果交出去，落库仍是页面上的「保存期初余额」那一步
    emit('imported', result.value)
    step.value = 'done'
  } finally {
    confirming.value = false
  }
}

function reset() {
  step.value = 'pick'
  file.value = null
  fileName.value = ''
  basis.value = 'opening'
  moreErrors.value = false
  result.value = { rows: [], errors: [] }
}
</script>

<style scoped>
.mb { margin-bottom: 14px; }
.muted { color: var(--cwq-muted); }
/* 预览那一步内容比一屏高（摘要 + 统计 + 两条告警 + 表格）。不封顶的话，
   底部那排「取消 / 确认导入」会被顶到屏幕外 —— 而那正是用户看完整屏之后
   要按的那个键，按不到等于整步白做。封顶 + 正文自己滚，底栏始终在。 */
:deep(.el-dialog__body) { max-height: calc(100vh - 240px); overflow-y: auto; }
.upload-box { width: 100%; }
/* 拖拽框：Element Plus 默认上下各留 40px、图标 67px，一块空地就有近 200px 高。
   弹窗收窄之后这块空白更扎眼 —— 收到 26px、图标缩到 44px，够放一句「拖到这里」就行 */
.upload-box :deep(.el-upload-dragger) { padding: 26px 16px; border-radius: 8px; }
.upload-box :deep(.el-upload-dragger .el-icon--upload) { margin-bottom: 10px; font-size: 44px; line-height: 40px; }
.selected-file { display: flex; align-items: center; gap: 8px; margin-top: 12px; padding: 10px 12px; color: #334155; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }
/* 三项并排一行：文件 / 来源 / 期间。原本按两列排，第三格「期间」会自己掉到第二行，
   右边空一大块，看着像少了一项 —— 三列各自 ellipsis 才不挤 */
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.summary-grid > div { display: flex; flex-direction: column; gap: 4px; min-width: 0; padding: 9px 12px; border: 1px solid #e5e7eb; background: #fff; }
.summary-grid b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ellipsis { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.basis-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.basis-label { color: var(--cwq-muted); font-size: 13px; }
/* 三个计数并排：读入 / 没匹配上 / 跳过。没匹配上的那格要黄，
   因为它是「你文件里有、这个账里没有」—— 少了它就等于漏了一笔余额 */
.stat-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; }
.stat { display: flex; align-items: baseline; justify-content: center; gap: 6px; padding: 10px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; }
.stat b { font-size: 20px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--cwq-muted); font-size: 12px; }
.stat.is-ok b { color: var(--cwq-ok); }
.stat.is-warn { background: #fdf6ec; border-color: #f5dab1; }
.stat.is-warn b { color: #e6a23c; }
.error-list { max-height: 150px; overflow: auto; margin: 4px 0 0; padding-left: 20px; line-height: 1.7; }
.more-btn { margin-top: 4px; }
.preview-table { margin-top: 8px; }
.row-error { color: var(--cwq-danger); }
.table-note { padding-top: 8px; font-size: 12px; }
.success-state { padding: 16px 0 4px; }
/* 窄屏三列会把「月份：2026.10-2026.10」挤成两行，退回两列 */
@media (max-width: 640px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
