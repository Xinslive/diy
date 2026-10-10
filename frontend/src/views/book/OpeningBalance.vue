<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">期初余额</h2>
        <div class="muted">
          {{ store.currentBook?.name }} · 启用期间 {{ data.period }}
          <template v-if="!data.editable">（{{ data.reason }}）</template>
        </div>
      </div>
      <div class="head-actions">
        <el-button @click="downloadTpl">下载空白模板</el-button>
        <el-button @click="exportXlsx">导出当前</el-button>
        <el-upload :show-file-list="false" accept=".xlsx" :before-upload="onFile">
          <el-button :loading="parsing">从 Excel 导入</el-button>
        </el-upload>
        <el-button type="primary" :loading="saving" :disabled="!data.editable || !dirty || !balanced"
                   @click="save">保存期初余额</el-button>
      </div>
    </div>

    <el-alert v-if="!data.editable" type="warning" :closable="false" class="mb">
      {{ data.reason }}。如需调整，先在「期末处理」反结账，或直接改第一期间的凭证。
    </el-alert>

    <div class="card mb" :class="balanced ? 'ok' : 'bad'">
      <div class="check">
        <span class="tag">{{ balanced ? '✓ 借贷平衡' : '✗ 借贷不平' }}</span>
        <span>借方合计 <b class="money">{{ money(dr) }}</b></span>
        <span>贷方合计 <b class="money">{{ money(cr) }}</b></span>
        <span v-if="!balanced" class="diff">差额 <b class="money">{{ money(dr - cr) }}</b></span>
        <span class="muted">共 {{ filledCount }} 个科目有余额</span>
      </div>
    </div>

    <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width:240px" class="mb" />

    <div v-for="cat in CATEGORIES" :key="cat.key" class="card mb" v-show="rowsOf(cat.key).length">
      <div class="cat-title">{{ cat.label }}</div>
      <table class="grid">
        <thead>
          <tr>
            <th class="code">科目编码</th>
            <th>科目名称</th>
            <th class="dir">方向</th>
            <th class="amt">期初余额</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in rowsOf(cat.key)" :key="s.id">
            <td class="code" :style="{ paddingLeft: 8 + (s.level - 1) * 16 + 'px' }">{{ s.code }}</td>
            <td>{{ s.name }}</td>
            <td class="dir">
              <template v-if="s.is_leaf">
                <el-tag
                  :type="sides[s.id] === '借' ? 'primary' : 'warning'"
                  size="small"
                  class="flip"
                  :title="flipHint(s)"
                  @click="flip(s)"
                >{{ sides[s.id] }}</el-tag>
              </template>
              <span v-else class="muted">{{ s.natural_side }}</span>
              <el-tooltip v-if="reverse(s)" content="与科目默认方向相反" placement="top">
                <span class="rev">反</span>
              </el-tooltip>
            </td>
            <td class="amt">
              <el-input
                v-model="amounts[s.id]"
                :disabled="!data.editable || !s.is_leaf"
                size="small"
                class="money"
                align="right"
                @blur="dirty = true"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'
import { downloadFile } from '@/api/print'

const store = useSessionStore()
const router = useRouter()
const data = ref({ period: '', editable: false, reason: '', subjects: [] })
const amounts = reactive({})
/** 每行期初的「实际方位」。科目属性只是通常方向，允许逐行翻转 —— 应收账款出现
 *  贷方余额、应交税费为负都是真实业务，强按属性纠正会把余额方向洗掉。 */
const sides = reactive({})
const dirty = ref(false)
const saving = ref(false)
const parsing = ref(false)
const keyword = ref('')

const CATEGORIES = [
  { key: 'ASSET', label: '资产' },
  { key: 'LIABILITY', label: '负债' },
  { key: 'EQUITY', label: '所有者权益' },
  { key: 'PROFIT', label: '损益' }
]

const money = (v) => (Number(v || 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2 })

const filledCount = computed(
  () => data.value.subjects.filter((s) => s.is_leaf && Number(amounts[s.id] || 0) !== 0).length
)

/** 一行的带符号金额：借正贷负，与后端存储口径一致。 */
function signedOf(s) {
  const v = Math.abs(Number(amounts[s.id] || 0))
  if (!v) return 0
  return sides[s.id] === '贷' ? -v : v
}

const reverse = (s) => s.is_leaf && sides[s.id] !== s.natural_side
const flipHint = (s) => `点击切换为${sides[s.id] === '借' ? '贷' : '借'}（科目默认 ${s.natural_side}）`

function flip(s) {
  if (!data.value.editable || !s.is_leaf) return
  sides[s.id] = sides[s.id] === '借' ? '贷' : '借'
  dirty.value = true
}

/** 借贷合计：两边加起来必须是 0。 */
const totals = computed(() => {
  let dr = 0, cr = 0
  for (const s of data.value.subjects) {
    if (!s.is_leaf) continue
    const signed = signedOf(s)
    if (signed > 0) dr += signed; else if (signed < 0) cr += -signed
  }
  return { dr, cr }
})
const dr = computed(() => totals.value.dr)
const cr = computed(() => totals.value.cr)
const balanced = computed(() => Math.abs(dr.value - cr.value) < 0.005)

function rowsOf(cat) {
  const kw = keyword.value.trim().toLowerCase()
  return data.value.subjects.filter(
    (s) => s.category === cat && (!kw || s.code.includes(kw) || s.name.toLowerCase().includes(kw))
  )
}

async function load() {
  if (!store.bookId) return router.push('/books')
  data.value = await api.getOpenings(store.bookId)
  for (const k of Object.keys(amounts)) delete amounts[k]
  for (const k of Object.keys(sides)) delete sides[k]
  for (const s of data.value.subjects) {
    // side 来自库里实际存的方位；没录过时退回科目自然方向
    sides[s.id] = Number(s.signed) !== 0 ? s.side : s.natural_side
    amounts[s.id] = s.is_leaf ? s.amount : ''
  }
  dirty.value = false
}

async function save() {
  const rows = data.value.subjects
    .filter((s) => s.is_leaf && Number(amounts[s.id] || 0) !== 0)
    .map((s) => ({ subject_id: s.id, amount: String(signedOf(s)) }))
  if (!balanced.value) {
    return ElMessage.error(`借贷不平：差额 ${money(dr.value - cr.value)}，不能保存`)
  }
  saving.value = true
  try {
    await api.setOpenings(store.bookId, rows)
    ElMessage.success(`已保存 ${rows.length} 条期初余额`)
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onFile(file) {
  parsing.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const res = await api.parseOpenings(store.bookId, fd)
    const ok = res.rows.filter((r) => !r.error)
    for (const r of ok) {
      if (!r.subject_id) continue
      // 方向列在文件里已经定过了（缺省才由后端按科目属性推），这里照搬，
      // 不能只取绝对值 —— 那样反方向余额会被无声地洗成正的
      const signed = Number(r.signed ?? 0)
      sides[r.subject_id] = r.display_side || (signed < 0 ? '贷' : '借')
      amounts[r.subject_id] = String(Math.abs(signed || Number(r.amount || 0)))
    }
    dirty.value = true
    if (res.balanced) ElMessage.success(`已读入 ${ok.length} 条，借贷平衡`)
    else ElMessage.warning(`已读入 ${ok.length} 条，但借贷不平：差 ${money(res.total_debit - res.total_credit)}`)
    if (res.errors.length) ElMessage.warning(res.errors.slice(0, 3).join('；'))
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '解析失败')
  } finally {
    parsing.value = false
  }
  return false
}

function exportXlsx() {
  downloadFile(`/books/${store.bookId}/openings.xlsx`, {}, '期初余额.xlsx')
}
function downloadTpl() {
  // 模板就是「导出当前」——科目已经在库里，导出来填金额即可
  exportXlsx()
}

watch(() => store.bookId, load)
onMounted(load)
</script>

<style scoped>
.page-title { margin: 0 0 2px; font-size: 18px; }
.head-actions { display: flex; gap: 8px; align-items: center; }
.mb { margin-bottom: 12px; }
.check { display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
.check .tag { font-weight: 700; }
.check b { font-size: 15px; }
.diff, .bad b { color: var(--cwq-danger); }
.ok .tag { color: var(--cwq-ok); }
.cat-title { font-weight: 700; margin-bottom: 8px; }
.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
.grid th, .grid td { border-bottom: 1px solid var(--cwq-border); padding: 5px 8px; }
.grid th { background: #f7f8fa; font-weight: 600; text-align: left; }
.grid .code { width: 130px; color: var(--cwq-muted); }
.grid .dir { width: 88px; text-align: center; }
.grid .dir .flip { cursor: pointer; user-select: none; }
.grid .dir .rev { margin-left: 4px; font-size: 11px; color: var(--cwq-danger); }
.grid .amt { width: 220px; }
</style>
