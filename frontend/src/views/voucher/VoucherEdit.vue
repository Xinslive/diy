<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-button @click="$router.back()">← 返回</el-button>
      <el-divider direction="vertical" />
      <span class="muted">凭证号</span>
      <el-input v-model="form.voucher_no" style="width: 110px" placeholder="自动生成" />
      <span class="muted">日期</span>
      <el-date-picker v-model="form.voucher_date" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
      <span class="muted">附件</span>
      <el-input-number v-model="form.attachments" :min="0" :max="999" style="width: 100px" />
      <span class="spacer" />
      <!-- 连续录入用：前一张/后一张在同期间内翻，插入/增加直接开一张新的。
           原来那个「增加分录」按钮去掉了 —— 分录靠回车往下加，不用专门去找按钮。 -->
      <el-button-group>
        <el-button :disabled="!prevId" @click="goNeighbour('prev')">前一张</el-button>
        <el-button :disabled="!nextId" @click="goNeighbour('next')">后一张</el-button>
      </el-button-group>
      <el-button :disabled="!canAdd" @click="insertVoucher">插入凭证</el-button>
      <el-button :disabled="!canAdd" @click="addVoucher">增加凭证</el-button>
      <el-button type="primary" :loading="saving" :disabled="!store.canWrite || readonly" @click="save">保存</el-button>
      <el-button v-if="voucherId" :disabled="form.status === 'POSTED'" @click="printVoucher">打印预览</el-button>
    </div>

    <div class="card">
      <div class="voucher-head">
        <div><span class="muted">会计期间</span> <b>{{ voucherPeriod?.code || store.currentPeriod?.code }}</b></div>
        <div><span class="muted">状态</span>
          <el-tag v-if="form.status === 'POSTED'" type="success" size="small">已记账</el-tag>
          <el-tag v-else type="warning" size="small">未记账</el-tag>
        </div>
        <div v-if="locked" class="tag-bad">当前期间已结账，不可修改</div>
        <div v-else-if="posted" class="tag-bad">
          已记账凭证已锁定，不能修改。请在凭证列表点「反记账」后再改。
        </div>
      </div>

      <div class="entry-wrap">
        <!-- 柠檬云那套：表格左边挂着两个动作，一键插行/加行，
             不用先去表格里找「删」再点 -->
        <div class="entry-gutter">
          <el-button class="g-ins" circle size="small" title="在当前行下面插入一行"
                     :disabled="readonly" @click="insertRow">⊕</el-button>
          <el-button class="g-add" circle size="small" title="在最后加一行"
                     :disabled="readonly" @click="addRow">＋</el-button>
        </div>

        <el-table
          :data="form.entries"
          border
          size="default"
          class="entries"
          :row-class-name="rowClass"
        >
          <el-table-column type="index" label="序号" width="58" align="center" />
          <el-table-column label="摘要" min-width="170">
            <template #default="{ row }">
              <el-input
                v-model="row.summary"
                :disabled="readonly"
                placeholder="摘要"
                @focus="activeRow = row"
                @keyup.enter="addRow"
              />
            </template>
          </el-table-column>
          <el-table-column label="会计科目" min-width="230">
            <template #default="{ row }">
              <el-select
                v-model="row.subject_id"
                :disabled="readonly"
                filterable
                placeholder="选择科目"
                style="width: 100%"
                :ref="(el) => setSubjectRef(el, $index)"
                @focus="activeRow = row"
                @change="onSubject(row)"
              >
                <el-option v-for="s in subjectOptions" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
              </el-select>
            </template>
          </el-table-column>
          <!-- 借、贷是两个独立的列，不是「选方向」的单选钮：
               往哪边填数就记哪边，少一次切状态，录入速度差很多。 -->
          <el-table-column label="借方金额" min-width="292">
            <template #default="{ row, $index }">
              <AmountGrid
                :model-value="debitOf(row)"
                :readonly="readonly"
                :show-head="$index === 0"
                @update:model-value="(v) => setDebit(row, v)"
                @focus="activeRow = row"
                @enter="addRow"
              />
            </template>
          </el-table-column>
          <el-table-column label="贷方金额" min-width="292">
            <template #default="{ row, $index }">
              <AmountGrid
                :model-value="creditOf(row)"
                :readonly="readonly"
                :show-head="$index === 0"
                @update:model-value="(v) => setCredit(row, v)"
                @focus="activeRow = row"
                @enter="addRow"
              />
            </template>
          </el-table-column>
          <el-table-column width="46" align="center">
            <template #default="{ $index }">
              <el-button
                link
                type="danger"
                class="row-del"
                :disabled="readonly || form.entries.length <= 1"
                title="删除这一行"
                @click="form.entries.splice($index, 1)"
              >⊗</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="totals">
        <div class="sum-cn">合计：{{ amountToChinese(total.debit) }}</div>
        <div>借方合计 <span class="money">{{ fmt(total.debit) }}</span></div>
        <div>贷方合计 <span class="money">{{ fmt(total.credit) }}</span></div>
        <div :class="balanced ? 'tag-ok' : 'tag-bad'">
          {{ balanced ? '借贷平衡 ✓' : `差额 ${fmt(Math.abs(total.debit - total.credit))}` }}
        </div>
      </div>

      <el-input v-model="form.remark" :disabled="readonly" placeholder="备注" class="remark" />
    </div>

    <!-- 打印预览 -->
    <el-dialog v-model="printVisible" title="记账凭证（打印预览）" width="820px">
      <div class="print-area">
        <div class="print-title">记 账 凭 证</div>
        <table class="print-table">
          <tbody>
            <tr v-for="(e, i) in form.entries" :key="i">
              <td class="w-date">{{ i === 0 ? form.voucher_date : '' }}</td>
              <td class="w-no">{{ i === 0 ? form.voucher_no : '' }}</td>
              <td>{{ e.summary }}</td>
              <td>{{ subjectText(e.subject_id) }}</td>
              <td class="w-amt">{{ e.direction === 'DEBIT' ? fmt(num(e.amount)) : '' }}</td>
              <td class="w-amt">{{ e.direction === 'CREDIT' ? fmt(num(e.amount)) : '' }}</td>
            </tr>
            <tr class="print-total">
              <td colspan="4">合计</td>
              <td class="w-amt">{{ fmt(total.debit) }}</td>
              <td class="w-amt">{{ fmt(total.credit) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #footer>
        <el-button @click="printVisible = false">关闭</el-button>
        <el-button type="primary" class="no-print" onclick="window.print()">打印</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/voucher'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'
import { amountToChinese } from '@/utils/money'
import AmountGrid from '@/components/AmountGrid.vue'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

const voucherId = computed(() => Number(route.params.id || 0))
const saving = ref(false)
const subjects = ref([])
const printVisible = ref(false)
// 同期间内的前一张 / 后一张，由后端按「录入顺序」算好
const prevId = ref(null)
const nextId = ref(null)
// 当前正在录入的那一行，整行淡绿底：连续录入时眼睛得有个落点
const activeRow = ref(null)
// 这张凭证自己的会计期间。顶栏那个期间是「现在正在记哪个月」，
// 打开一张旧凭证时两者根本不是一回事，页眉写错期间很容易误导人。
const voucherPeriod = ref(null)

const form = reactive({
  voucher_no: '', voucher_date: '', status: 'UNPOSTED',
  attachments: 0, remark: '', entries: []
})

const locked = computed(() => !!store.currentPeriod?.locked)
const posted = computed(() => form.status === 'POSTED')
// 只读：期间已结账，或凭证本身已记账
const readonly = computed(() => locked.value || posted.value)

// 能不能开新凭证：只读账套不行，期间已结账也不行
const canAdd = computed(() => store.canWrite && !locked.value)

// 正常只列末级科目。但已选中的那个必须**一定**在列表里：
// 导入或历史数据可能把分录挂在上级科目上，这时若下拉里找不到它，
// el-select 只会显示一个裸 id（956），谁也不知道那是什么科目。
const subjectOptions = computed(() => {
  const picked = new Set(form.entries.map((e) => e.subject_id).filter(Boolean))
  const extra = subjects.value.filter((s) => picked.has(s.id) && !s.is_leaf)
  return [...extra, ...subjects.value.filter((s) => s.is_leaf)]
})

// ---- 借方/贷方是两个独立格子，数据层还是一个 direction + amount ----
// 往借方填数就是借方，往贷方填数就是贷方；清空则只清自己那一侧。
const debitOf = (row) => (row.direction === 'DEBIT' ? row.amount : '')
const creditOf = (row) => (row.direction === 'CREDIT' ? row.amount : '')
function setDebit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'DEBIT') row.amount = ''
    return
  }
  row.direction = 'DEBIT'
  row.amount = v
}
function setCredit(row, v) {
  if (v === '' || v === null || v === undefined) {
    if (row.direction === 'CREDIT') row.amount = ''
    return
  }
  row.direction = 'CREDIT'
  row.amount = v
}
function rowClass({ row }) {
  return row === activeRow.value ? 'row-active' : ''
}
const num = (v) => (v === '' || v === null || v === undefined ? 0 : Number(v))
const total = computed(() => {
  let debit = 0, credit = 0
  for (const e of form.entries) {
    if (e.direction === 'DEBIT') debit += num(e.amount)
    else credit += num(e.amount)
  }
  return { debit, credit }
})
const balanced = computed(() => total.value.debit === total.value.credit && total.value.debit > 0)

function subjectText(id) {
  const s = subjects.value.find((x) => x.id === id)
  return s ? `${s.code} ${s.name}` : ''
}
function onSubject(row) {
  if (!row.summary) {
    const n = form.entries.filter((e) => e.summary).length
    if (n === 0) {
      const first = form.entries.find((e) => e.subject_id)
      if (first) first.summary = ''
    }
  }
}
// 每行的科目下拉要能拿到实例，回车加完行直接聚焦过去，不用鼠标去找
const subjectRefs = new Map()
function setSubjectRef(el, idx) {
  if (el) subjectRefs.set(idx, el)
  else subjectRefs.delete(idx)
}

/** 在当前行下面插一行：先切回借方，录入的人接着往下写。 */
function insertRow() {
  const at = Math.max(0, form.entries.indexOf(activeRow.value)) + 1
  const last = form.entries[at - 1]
  form.entries.splice(at, 0, {
    subject_id: null,
    direction: 'DEBIT',
    amount: '',
    summary: last ? last.summary : ''
  })
  nextTick(() => subjectRefs.get(at)?.focus?.())
}

function addRow() {
  // 自动补一行方向相反的分录，符合连续录入的习惯
  const last = form.entries[form.entries.length - 1]
  form.entries.push({
    subject_id: null,
    direction: last ? (last.direction === 'DEBIT' ? 'CREDIT' : 'DEBIT') : 'DEBIT',
    amount: '',
    summary: last ? last.summary : ''
  })
  activeRow.value = form.entries[form.entries.length - 1]
  if (readonly.value) return
  nextTick(() => subjectRefs.get(form.entries.length - 1)?.focus?.())
}

/** 前一张 / 后一张：只在同一会计期间内翻，跳到最前/最后一张时按钮自己禁用。 */
function goNeighbour(which) {
  const id = which === 'prev' ? prevId.value : nextId.value
  if (id) router.push(`/vouchers/${id}`)
}

/** 插入凭证：接着当前这张开一张新的，日期沿用当前凭证的日期。
 *  只跳到空白页，不预先落库 —— 空凭证一旦存进去就会占号、还会漏在列表里。 */
function insertVoucher() {
  router.push({ name: 'voucher-new', query: { date: form.voucher_date } })
}

/** 增加凭证：不受当前凭证日期影响，按所在期间的默认日期另起一张。 */
function addVoucher() {
  router.push({ name: 'voucher-new' })
}

async function loadNeighbour() {
  if (!voucherId.value) {
    prevId.value = null
    nextId.value = null
    return
  }
  const r = await api.neighbour(store.bookId, voucherId.value)
  prevId.value = r.prev_id
  nextId.value = r.next_id
}

async function load() {
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  subjectRefs.clear()
  if (voucherId.value) {
    const v = await api.getVoucher(store.bookId, voucherId.value)
    voucherPeriod.value = store.periods.find((p) => p.id === v.period_id) || null
    Object.assign(form, {
      voucher_no: v.voucher_no, voucher_date: v.voucher_date, status: v.status,
      attachments: v.attachments, remark: v.remark,
      entries: v.entries.map((e) => ({
        subject_id: e.subject_id, direction: e.direction, amount: e.amount, summary: e.summary
      }))
    })
    await loadNeighbour()
  } else {
    // 「插入凭证」会把当前凭证的日期带过来
    form.voucher_date = route.query.date || defaultDate()
    voucherPeriod.value = null   // 新凭证还没保存，期间以保存时按日期落库为准
    addRow()
    addRow()
    prevId.value = null
    nextId.value = null
  }
}

function defaultDate() {
  const p = store.currentPeriod
  if (!p) return new Date().toISOString().slice(0, 10)
  const day = new Date().getDate()
  const d = String(Math.min(day, 28)).padStart(2, '0')
  return `${p.year}-${String(p.month).padStart(2, '0')}-${d}`
}

async function save() {
  if (readonly.value) {
    return ElMessage.warning(posted.value ? '已记账凭证不能修改，请先反记账' : '当前期间已结账，不能修改凭证')
  }
  const entries = form.entries
    .filter((e) => e.subject_id && num(e.amount))
    .map((e) => ({
      subject_id: e.subject_id,
      direction: e.direction,
      amount: String(num(e.amount)),
      summary: e.summary || ''
    }))
  if (!entries.length) return ElMessage.warning('请至少填写一条分录')
  if (!balanced.value) return ElMessage.error('借贷不平衡，无法保存')

  const payload = {
    voucher_date: form.voucher_date, entries, remark: form.remark,
    attachments: form.attachments, voucher_no: form.voucher_no || null
  }
  saving.value = true
  try {
    if (voucherId.value) await api.updateVoucher(store.bookId, voucherId.value, payload)
    else await api.createVoucher(store.bookId, payload)
    ElMessage.success('已保存')
    router.push('/vouchers')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  if (!store.periodId) return
  await load()
})
// 前一张/后一张只是换 route.params，组件不会重新挂载 —— 必须盯着 id
watch(() => [store.bookId, store.periodId, route.params.id], load)
</script>

<style scoped>
.voucher-head { display: flex; gap: 24px; align-items: center; margin-bottom: 12px; font-size: 13px; }

/* 表格 + 左侧动作栏，整体跟柠檬云一样：左边窄窄一条放 ⊕ / ＋ */
.entry-wrap { display: flex; align-items: stretch; gap: 8px; margin-bottom: 12px; }
.entry-gutter {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding-top: 46px;
}
.entry-gutter .el-button + .el-button { margin-left: 0; }
.g-ins { color: #fff; background: #67c23a; border-color: #67c23a; font-weight: 700; }
.g-ins:hover { background: #85ce61; border-color: #85ce61; color: #fff; }
.g-add { color: #fff; background: #409eff; border-color: #409eff; font-weight: 700; }
.g-add:hover { background: #66b1ff; border-color: #66b1ff; color: #fff; }

.entries { flex: 1; min-width: 0; }

/* 当前录入行：淡绿底，眼睛跟着手走 */
.entries :deep(.el-table__row.row-active > td.el-table__cell) {
  background: #f0f9eb;
}
.row-del { font-size: 15px; font-weight: 700; line-height: 1; }

.totals { display: flex; gap: 28px; justify-content: flex-end; align-items: baseline; font-size: 14px; padding: 6px 0 12px; }
.sum-cn { font-weight: 700; font-size: 15px; }
.remark { margin-top: 4px; }
.w-date { width: 90px; } .w-no { width: 80px; } .w-amt { width: 110px; text-align: right; }
.entries :deep(.el-radio-button__inner) { padding: 6px 10px; }
.print-area { padding: 8px; }
.print-title { text-align: center; font-size: 18px; font-weight: 700; letter-spacing: 6px; margin-bottom: 14px; }
.print-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.print-table td { border: 1px solid #dcdfe6; padding: 7px 8px; }
.print-total td { font-weight: 700; background: #fafafa; }
</style>
