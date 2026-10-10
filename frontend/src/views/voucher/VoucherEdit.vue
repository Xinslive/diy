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
      <el-button @click="addRow">增加分录</el-button>
      <el-button type="primary" :loading="saving" :disabled="!store.canWrite" @click="save">保存</el-button>
      <el-button v-if="voucherId" :disabled="form.status === 'POSTED'" @click="printVoucher">打印预览</el-button>
    </div>

    <div class="card">
      <div class="voucher-head">
        <div><span class="muted">会计期间</span> <b>{{ store.currentPeriod?.code }}</b></div>
        <div><span class="muted">状态</span>
          <el-tag v-if="form.status === 'POSTED'" type="success" size="small">已记账</el-tag>
          <el-tag v-else type="warning" size="small">未记账</el-tag>
        </div>
        <div v-if="locked" class="tag-bad">当前期间已结账，不可修改</div>
      </div>

      <el-table :data="form.entries" border size="default" class="entries">
        <el-table-column type="index" label="#" width="46" align="center" />
        <el-table-column label="会计科目" min-width="300">
          <template #default="{ row }">
            <el-select v-model="row.subject_id" filterable placeholder="选择科目" style="width: 100%" @change="onSubject(row)">
              <el-option v-for="s in subjectOptions" :key="s.id" :label="`${s.code} ${s.name}`" :value="s.id" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="方向" width="96" align="center">
          <template #default="{ row }">
            <el-radio-group v-model="row.direction" size="small">
              <el-radio-button value="DEBIT">借</el-radio-button>
              <el-radio-button value="CREDIT">贷</el-radio-button>
            </el-radio-group>
          </template>
        </el-table-column>
        <el-table-column label="金额" width="160" align="right">
          <template #default="{ row }">
            <el-input v-model="row.amount" placeholder="0.00" class="money" @keyup.enter="addRow" />
          </template>
        </el-table-column>
        <el-table-column label="摘要" min-width="220">
          <template #default="{ row }"><el-input v-model="row.summary" /></template>
        </el-table-column>
        <el-table-column width="52" align="center">
          <template #default="{ $index }">
            <el-button link type="danger" @click="form.entries.splice($index, 1)">删</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="totals">
        <div>借方合计 <span class="money">{{ fmt(total.debit) }}</span></div>
        <div>贷方合计 <span class="money">{{ fmt(total.credit) }}</span></div>
        <div :class="balanced ? 'tag-ok' : 'tag-bad'">
          {{ balanced ? '借贷平衡 ✓' : `差额 ${fmt(Math.abs(total.debit - total.credit))}` }}
        </div>
      </div>

      <el-input v-model="form.remark" placeholder="备注" class="remark" />
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/voucher'
import * as subjectApi from '@/api/subject'
import { useSessionStore } from '@/stores/session'
import { fmt } from '@/utils/format'

const store = useSessionStore()
const route = useRoute()
const router = useRouter()

const voucherId = computed(() => Number(route.params.id || 0))
const saving = ref(false)
const subjects = ref([])
const printVisible = ref(false)

const form = reactive({
  voucher_no: '', voucher_date: '', status: 'UNPOSTED',
  attachments: 0, remark: '', entries: []
})

const locked = computed(() => !!store.currentPeriod?.locked)

const subjectOptions = computed(() => subjects.value.filter((s) => s.is_leaf))
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
function addRow() {
  // 自动补一行方向相反的分录，符合连续录入的习惯
  const last = form.entries[form.entries.length - 1]
  form.entries.push({
    subject_id: null,
    direction: last ? (last.direction === 'DEBIT' ? 'CREDIT' : 'DEBIT') : 'DEBIT',
    amount: '',
    summary: last ? last.summary : ''
  })
}

async function load() {
  subjects.value = await subjectApi.subjectFlat(store.bookId)
  if (voucherId.value) {
    const v = await api.getVoucher(store.bookId, voucherId.value)
    Object.assign(form, {
      voucher_no: v.voucher_no, voucher_date: v.voucher_date, status: v.status,
      attachments: v.attachments, remark: v.remark,
      entries: v.entries.map((e) => ({
        subject_id: e.subject_id, direction: e.direction, amount: e.amount, summary: e.summary
      }))
    })
  } else {
    form.voucher_date = defaultDate()
    addRow()
    addRow()
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
watch(() => [store.bookId, store.periodId], load)
</script>

<style scoped>
.voucher-head { display: flex; gap: 24px; align-items: center; margin-bottom: 12px; font-size: 13px; }
.entries { margin-bottom: 12px; }
.totals { display: flex; gap: 28px; justify-content: flex-end; font-size: 14px; padding: 6px 0 12px; }
.remark { margin-top: 4px; }
.w-date { width: 90px; } .w-no { width: 80px; } .w-amt { width: 110px; text-align: right; }
.entries :deep(.el-radio-button__inner) { padding: 6px 10px; }
.print-area { padding: 8px; }
.print-title { text-align: center; font-size: 18px; font-weight: 700; letter-spacing: 6px; margin-bottom: 14px; }
.print-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.print-table td { border: 1px solid #dcdfe6; padding: 7px 8px; }
.print-total td { font-weight: 700; background: #fafafa; }
</style>
