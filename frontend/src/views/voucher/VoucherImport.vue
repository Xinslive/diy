<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span class="title">凭证导入</span>
          <span class="hint">先解析校验、逐张预览，确认无误后再写入账套。任何一张出错都会整体回滚。</span>
        </div>
      </template>

      <el-steps :active="step" align-center finish-status="success" class="mb">
        <el-step title="上传文件" />
        <el-step title="校验预览" />
        <el-step title="确认入库" />
      </el-steps>

      <div v-if="step === 0">
        <el-alert type="info" :closable="false" show-icon class="mb">
          <template #title>标准表头</template>
          <div class="mt-xs">
            日期、凭证号（可空）、摘要、科目编码、科目名称、借方金额、贷方金额、附单据数、备注。<br />
            一张凭证占多行时，第二行起<b>日期与凭证号留空</b>即视为接上一张。<br />
            用友导出的<b>序时账</b>（.xls）也能直接导：那种表每条分录都重复日期与凭证号数，
            同样的日期与凭证号会自动并成同一张凭证。
          </div>
        </el-alert>

        <el-upload
          drag :auto-upload="false" :show-file-list="false" accept=".xls,.xlsx,.xlsm"
          :on-change="onFile" class="mb"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">把 Excel 拖到此处，或<em>点击选择文件</em></div>
          <template #tip>
            <div class="el-upload__tip">支持 .xls / .xlsx，用友导出的序时账可直接导入；表头别名容错（日期/制单日期/voucher_date 均可）</div>
          </template>
        </el-upload>

        <div class="row">
          <el-button type="primary" :disabled="!file" :loading="uploading" @click="upload">
            <el-icon><Upload /></el-icon>&nbsp;上传并解析
          </el-button>
          <el-button @click="downloadTemplate">
            <el-icon><Download /></el-icon>&nbsp;下载导入模板
          </el-button>
        </div>
      </div>

      <div v-else-if="step === 1">
        <el-alert
          :type="result.error_count ? 'error' : 'success'" :closable="false" show-icon class="mb"
          :title="`共解析 ${result.total} 张凭证：${result.ok_count} 张通过，${result.error_count} 张有问题`"
        />

        <el-alert
          v-if="Object.keys(result.unknown_subjects || {}).length"
          type="warning" :closable="false" show-icon class="mb"
          :title="`有 ${Object.keys(result.unknown_subjects).length} 个科目在本账套不存在：${Object.keys(result.unknown_subjects).join('、')}`"
        />
        <el-alert
          v-if="(result.unknown_headers || []).length"
          type="info" :closable="false" show-icon class="mb"
          :title="`忽略了无法识别的列：${result.unknown_headers.join('、')}`"
        />
        <!-- 用友的序时账表尾固定带「合计」行（借贷两行、金额是全表发生额合计）。
             它没有日期、没有凭证号也没有科目，不是分录，已经被跳过 ——
             但必须说出来：不说的话用户会以为表里本来就没这行数据，
             而那行合计往往就是全账最大的一笔数。 -->
        <el-alert
          v-if="result.footer_rows"
          type="info" :closable="false" show-icon class="mb"
          :title="`已跳过表尾 ${result.footer_rows} 行「合计」，它们不是分录，不会进入账上`"
        />

        <!-- 被丢弃的行必须显眼。原来只有表格里一个「有问题」标签，鼠标悬停
             才看得到原因 —— 而审计记录的那个场景正是「用户什么都不知道」。
             丢行数直接顶到提示条上，不靠悬停。 -->
        <el-alert
          v-if="droppedLines.length"
          type="error" :closable="false" show-icon class="mb"
          :title="`有 ${droppedLines.length} 行数据没能生成分录，导入后不会出现在账上`"
        >
          <ul class="drop-list">
            <li v-for="d in droppedLines" :key="d.voucherKey + '-' + d.line">{{ d.text }}</li>
          </ul>
        </el-alert>

        <el-table :data="result.vouchers" size="small" border max-height="460"
                  :row-class-name="({ row }) => (row.ok ? '' : 'row-bad')">
          <el-table-column prop="line" label="行号" width="60" align="center" />
          <el-table-column label="日期" width="100">
            <template #default="{ row }">{{ row.voucher_date_raw || row.voucher_date || '（接上一张）' }}</template>
          </el-table-column>
          <el-table-column label="凭证号" width="90">
            <template #default="{ row }">{{ row.voucher_no || '自动' }}</template>
          </el-table-column>
          <el-table-column label="分录" min-width="330">
            <template #default="{ row }">
              <div v-for="(e, i) in row.entries" :key="i" class="entry">
                <span class="sub">{{ e.subject_code }} {{ e.subject_name }}</span>
                <span class="sum">{{ e.summary }}</span>
                <span class="amt" :class="e.direction === 'DEBIT' ? 'dr' : 'cr'">
                  {{ e.direction === 'DEBIT' ? '借' : '贷' }} {{ e.amount }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="金额" width="130" align="right">
            <template #default="{ row }">{{ row.total_debit }}</template>
          </el-table-column>
          <el-table-column label="校验" width="90" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.ok" type="success" size="small">通过</el-tag>
              <el-tooltip v-else :content="row.errors.join('；')" placement="left">
                <el-tag type="danger" size="small">有问题</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>

        <div class="row mt">
          <el-button type="success" :disabled="result.ok_count === 0" :loading="committing" @click="commit">
            确认导入 {{ result.ok_count }} 张
          </el-button>
          <el-button @click="step = 0">重新选择文件</el-button>
          <el-button text @click="discard">丢弃该批次</el-button>
        </div>
      </div>

      <div v-else>
        <el-result icon="success" :title="`成功导入 ${done} 张凭证`">
          <template #sub-title>
            导入的凭证状态为「未记账」，请到凭证列表确认后再记账。
          </template>
          <template #extra>
            <el-button type="primary" @click="$router.push('/vouchers')">去凭证列表</el-button>
            <el-button @click="reset">继续导入</el-button>
          </template>
        </el-result>
      </div>
    </el-card>

    <el-card shadow="never" class="mt">
      <template #header><span class="title">导入历史</span></template>
      <el-table :data="history" size="small" border>
        <el-table-column prop="filename" label="文件" min-width="180" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total" label="总张数" width="80" align="center" />
        <el-table-column prop="ok_count" label="通过" width="70" align="center" />
        <el-table-column prop="error_count" label="有问题" width="80" align="center" />
        <el-table-column prop="committed_count" label="已入库" width="80" align="center" />
        <el-table-column prop="created_by" label="操作人" width="90" />
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="message" label="备注" min-width="180" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, UploadFilled, Download } from '@element-plus/icons-vue'
import * as voucherApi from '@/api/voucher'
import { downloadFile } from '@/api/print'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const step = ref(0)
const file = ref(null)
const result = ref({ vouchers: [], total: 0, ok_count: 0, error_count: 0, footer_rows: 0 })
const done = ref(0)
const uploading = ref(false)
const committing = ref(false)
const history = ref([])

const statusText = (s) => ({ PENDING: '待确认', COMMITTED: '已入库', DISCARDED: '已丢弃' }[s] || s)
const statusType = (s) => ({ PENDING: 'warning', COMMITTED: 'success', DISCARDED: 'info' }[s] || '')

function onFile(f) {
  file.value = f.raw
}

function downloadTemplate() {
  downloadFile(`/books/${store.bookId}/vouchers/import-template.xlsx`, {}, '凭证导入模板.xlsx')
}

async function upload() {
  uploading.value = true
  try {
    result.value = await voucherApi.uploadImport(store.bookId, file.value)
    step.value = 1
  } finally {
    uploading.value = false
  }
}

// 有行被丢弃时，导入按钮要先把「哪些没进去」摆出来再说 ——
// 点一下就入库、事后才发现少记了金额，是最难挽回的一类操作。
const droppedLines = computed(() => {
  const out = []
  for (const v of result.value.vouchers || []) {
    for (const e of v.errors || []) {
      if (/没有生成任何分录|无法解析为金额|百分号|都填了金额|无法判断方向/.test(e)) {
        out.push({
          voucherKey: `${v.voucher_no || '自动'}@${v.line}`,
          line: v.line,
          text: `第 ${v.line} 行（凭证 ${v.voucher_no || '自动'}）：${e}`,
        })
      }
    }
  }
  return out
})

async function commit() {
  const bad = result.value.error_count || 0
  const dropped = droppedLines.value.length
  if (bad || dropped) {
    const detail = [
      bad ? `${bad} 张凭证校验不通过，不会导入` : '',
      dropped ? `${dropped} 行数据被丢弃，不会出现在账上` : '',
    ].filter(Boolean).join('；')
    try {
      await ElMessageBox.confirm(
        `${detail}。仍然只导入通过的 ${result.value.ok_count} 张吗？`,
        '确认部分导入',
        { type: 'warning', confirmButtonText: `只导入 ${result.value.ok_count} 张`, cancelButtonText: '返回检查' }
      )
    } catch {
      return   // 用户点了「返回检查」
    }
  }
  committing.value = true
  try {
    const res = await voucherApi.commitImport(store.bookId, result.value.batch_id)
    done.value = res.created
    step.value = 2
    await loadHistory()
  } finally {
    committing.value = false
  }
}

async function discard() {
  await voucherApi.discardImport(store.bookId, result.value.batch_id)
  ElMessage.success('已丢弃')
  reset()
}

function reset() {
  step.value = 0
  file.value = null
  result.value = { vouchers: [], total: 0, ok_count: 0, error_count: 0 }
  loadHistory()
}

async function loadHistory() {
  history.value = await voucherApi.listImports(store.bookId)
}

onMounted(loadHistory)
</script>

<style scoped>
.row { display: flex; gap: 10px; align-items: center; }
.mt { margin-top: 14px; }
.mb { margin-bottom: 14px; }
.mt-xs { margin-top: 6px; }
.entry { display: flex; gap: 10px; font-size: 12px; line-height: 1.7; }
.entry .sub { width: 190px; }
.entry .sum { flex: 1; color: #666; }
.entry .amt { width: 130px; text-align: right; font-family: ui-monospace, monospace; }
.entry .dr { color: #c0392b; }
.entry .cr { color: #1e8449; }
:deep(.row-bad) { background: #fdf0ef; }
.drop-list { margin: 6px 0 0; padding-left: 20px; font-size: 12px; line-height: 1.8; }
</style>
