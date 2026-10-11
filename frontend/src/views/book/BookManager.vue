<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">账套管理</h2>
        <div class="muted">
          账套是独立的记账单元，一个账套可以记很多个年度；年度结转只是在同一个账套里往下追加一年，
          不会新开账套
        </div>
      </div>
      <!-- 谁都能建账套，建完自己就是 OWNER；管理员授权管的是别人的账套 -->
      <el-button type="primary" @click="openCreate">新建账套</el-button>
    </div>

    <div class="card" v-loading="loading">
      <el-table :data="store.books" border>
        <el-table-column label="单位名称" prop="company_name" min-width="180" />
        <el-table-column label="账套名称" min-width="170">
          <template #default="{ row }">
            <el-link type="primary" @click="switchTo(row.id, pickedYear(row))">{{ row.name }}</el-link>
            <el-tag v-if="row.id === store.bookId" size="small" type="success" class="ml">当前</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="年度" width="118" align="center">
          <template #default="{ row }">
            <!-- 默认停在最新年度；要看往年就手动切，进去的账就是选中的那一年 -->
            <el-select
              :model-value="pickedYear(row)"
              size="small"
              style="width: 100%"
              @change="(v) => onYearPick(row, v)"
              @click.stop
            >
              <el-option v-for="y in row.years" :key="y.year" :label="`${y.year} 年`" :value="y.year">
                <!-- 同 MainLayout：float 会让「已结完」跟年份文字上下错开 -->
                <span class="year-opt">
                  <span>{{ y.year }} 年</span>
                  <el-tag v-if="y.carried" size="small" type="info">已结完</el-tag>
                </span>
              </el-option>
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="已结账" width="120" align="center">
          <template #default="{ row }">
            <span :class="yearOf(row).carried ? 'ok' : ''">
              {{ yearOf(row).closed }} / {{ yearOf(row).total }} 期
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="yearOf(row).carried ? 'info' : 'success'" size="small">
              {{ yearOf(row).carried ? '已结转' : '使用中' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="switchTo(row.id, pickedYear(row))">进入</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty
        v-if="!loading && !store.books.length"
        description="还没有账套，点右上角「新建账套」自己开一个；也可以请管理员在「用户与授权」里把已有账套授权给你"
      />
    </div>

    <el-dialog v-model="visible" title="新建账套" width="520px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="公司名称" required>
          <el-input v-model="form.company_name" placeholder="如：启明物业管理有限公司" />
        </el-form-item>
        <el-form-item label="账套名称" required>
          <el-input v-model="form.name" placeholder="如：租金账套" />
        </el-form-item>
        <el-form-item label="起始年度" required>
          <el-select v-model="form.start_year" style="width: 100%">
            <el-option v-for="y in yearOptions" :key="y" :label="y + ' 年'" :value="y" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用月份">
          <el-select v-model="form.start_month" style="width: 100%">
            <el-option v-for="m in 12" :key="m" :label="m + ' 月'" :value="m" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert v-if="newBookId" type="success" :closable="false" class="mb">
        <div>账套已创建，下一步录入<strong>期初余额</strong>（上一年的科目余额表）。</div>
        <el-button size="small" type="primary" class="mt" @click="goOpenings(newBookId)">
          去录期初余额
        </el-button>
        <el-button size="small" class="mt" @click="closeCreate">稍后再说</el-button>
      </el-alert>
      <template #footer>
        <el-button @click="closeCreate">关闭</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSubmit" @click="submit">
          创建账套
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑账套" width="560px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="单位名称" required>
          <el-input v-model="editForm.company_name" />
        </el-form-item>
        <el-form-item label="账套名称" required>
          <el-input v-model="editForm.name" />
        </el-form-item>
        <!-- 科目编码设置直接排在编辑表单里，不再另开一层弹窗：
             改的是一个字段而已，为它开第二个窗只是多点一次。
             原来是让用户手填「4-2-2-2」，填错了要等到新增科目接错号才发现。 -->
        <el-form-item label="科目编码">
          <div class="rule-block">
            <div class="rule-row">
              <span class="rule-label">科目级次：</span>
              <el-select v-model="ruleLevel" size="small" style="width: 84px">
                <el-option v-for="n in LEVELS" :key="n" :label="String(n)" :value="n"
                           :disabled="n < minLevel" />
              </el-select>
              <span v-if="minLevel > 2" class="rule-note">
                注：已用到 {{ minLevel }} 级，不能再调小
              </span>
            </div>
            <div class="rule-row">
              <span class="rule-label">编码长度：</span>
              <template v-for="(w, i) in ruleWidths" :key="i">
                <span v-if="i" class="rule-dash">-</span>
                <!-- 一级固定 4 位：1xxx 资产、5xxx 损益，动了整张科目表都认不出 -->
                <el-select :model-value="w" size="small" style="width: 66px" :disabled="i === 0"
                           @update:model-value="setWidth(i, $event)">
                  <el-option v-for="n in widthOptions(i)" :key="n" :label="String(n)" :value="n" />
                </el-select>
              </template>
            </div>
            <div class="rule-sample">示例：{{ ruleSample }}</div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSaveEdit" @click="submitEdit">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as api from '@/api/book'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()
const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const visible = ref(false)
const newBookId = ref(0)

const yearOptions = Array.from({ length: 6 }, (_, i) => new Date().getFullYear() - 1 + i)
const form = reactive({
  company_name: '', name: '', start_year: new Date().getFullYear(), start_month: 1
})

const canSubmit = computed(() => form.company_name.trim() && form.name.trim())

// 每个账套当前选中的年度。默认最新年度，用户可以往前翻。
const picked = reactive({})
const yearsOf = (row) => (row.years || []).slice().sort((a, b) => b.year - a.year)
const pickedYear = (row) => picked[row.id] ?? (yearsOf(row)[0]?.year ?? row.start_year)
const yearOf = (row) =>
  (row.years || []).find((y) => y.year === pickedYear(row)) ||
  { closed: 0, total: row.start_month === 1 ? 12 : 0, carried: false }

function onYearPick(row, y) {
  picked[row.id] = y
  // 当场切过去：用户选年度就是想看那一年，不必再点「进入」
  if (row.id === store.bookId) store.setYear(y)
}

function closeCreate() {
  visible.value = false
  newBookId.value = 0
  // 不清掉的话，下次重挂载又会把「刚建好账套」的弹窗弹出来
  store.setLastCreatedBook(0)
}

function openCreate() {
  newBookId.value = 0
  store.setLastCreatedBook(0)
  form.company_name = ''
  form.name = `${form.start_year} 账套`
  form.start_month = 1
  visible.value = true
}

async function submit() {
  saving.value = true
  try {
    const book = await api.createBook({ ...form })
    newBookId.value = book.id
    // 建第一个账套时 bookId 从 0 变过来，页面会整个重挂载，
    // 这里先把 id 存到 store，挂载后好把提示接回来
    store.setLastCreatedBook(book.id)
    await store.loadBooks()
    ElMessage.success(`账套《${book.company_name} · ${book.name}》已创建，标准科目表已装载`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    saving.value = false
  }
}

// ---- 编辑账套名 / 单位名 ----
const editVisible = ref(false)
const editTarget = ref(null)
const editForm = reactive({ company_name: '', name: '', subject_code_rule: '4-2-2-2' })
const canSaveEdit = computed(() => editForm.company_name.trim() && editForm.name.trim())

// ---- 科目编码设置 ----
const LEVELS = [2, 3, 4, 5, 6]
// 二级往后只给 2/3/4 位。一级固定 4 位；下级再长的话，12 位以后的编码
// 在凭证、科目表、打印抬头里都读不顺眼，实际用起来也记不住。
const WIDTHS = [2, 3, 4]
const usedLevel = ref(2)

// 老账套可能存着别的位数（1 位或 8 位），照实列出来，
// 免得下拉里找不到当前值、打开就显示成空的
const widthOptions = (i) => {
  const w = ruleWidths.value[i]
  return WIDTHS.includes(w) ? WIDTHS : [w, ...WIDTHS].sort((a, b) => a - b)
}

// 存的是「4-2-2-2」这样一个串，界面上是「级次 + 每级几位」。
// 改动先落在数组上，确定保存时再拼回串 —— 界面上没保存的改动不跟去接口。
const ruleWidths = computed({
  get: () => String(editForm.subject_code_rule || '4-2-2-2').split('-').map(Number),
  set: (arr) => { editForm.subject_code_rule = arr.join('-') }
})

// 级次就是「几段」。调大时补一段默认长度，调小时砍掉后面的。
const ruleLevel = computed({
  get: () => ruleWidths.value.length,
  set: (n) => {
    const cur = ruleWidths.value
    ruleWidths.value = Array.from({ length: n }, (_, i) => cur[i] ?? 2)
  }
})
// 示例按「每个下级都是本级的第 1 号」来排：3 位就是 001、2 位就是 01，
// 正好对得上用户在别处看过的「1001 001 01 01」
const ruleSample = computed(() => [
  '1001',
  ...ruleWidths.value.slice(1).map((w) => '0'.repeat(Math.max(0, w - 1)) + '1')
].join(' '))
// 账上已经有第 N 级的科目，级次就调不到 N 以下 —— 后端也拦一道，这里先置灰
const minLevel = computed(() => Math.max(2, usedLevel.value))
function setWidth(i, v) {
  const next = ruleWidths.value.slice()
  next[i] = v
  ruleWidths.value = next
}

async function openEdit(row) {
  editTarget.value = row
  editForm.company_name = row.company_name
  editForm.name = row.name
  editForm.subject_code_rule = row.subject_code_rule || '4-2-2-2'
  editVisible.value = true
  // 账上用到第几级，只有单账套的详情里才有（列表页不给，免得每个账套都多扫一次科目表）
  try {
    const detail = await api.getBook(row.id)
    editTarget.value = { ...row, ...detail }
    editForm.subject_code_rule = detail.subject_code_rule || editForm.subject_code_rule
    usedLevel.value = detail.subject_max_level || 2
  } catch { /* 拿不到就按 2 级起算，后端还会再拦一道 */ }
}

async function submitEdit() {
  saving.value = true
  try {
    await api.updateBook(editTarget.value.id, { ...editForm })
    await store.loadBooks()
    editVisible.value = false
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function switchTo(id, year) {
  await store.setBook(id)
  if (year) store.setYear(year)
  ElMessage.success(`已进入《${store.currentBook?.name}》${store.currentPeriod?.code || ''}`)
  router.push('/vouchers')
}

async function goOpenings(id) {
  if (store.bookId !== id) await store.setBook(id)
  router.push('/openings')
}

// 账套列表刷新后，把「当前账套」的对齐到顶栏正在看的那一年
watch(() => store.currentPeriod?.year, (y) => {
  const b = store.currentBook
  if (b && y && b.years?.some((x) => x.year === y)) picked[b.id] = y
})

onMounted(async () => {
  loading.value = true
  try {
    await store.loadBooks()
    const b = store.currentBook
    if (b && store.currentPeriod?.year) picked[b.id] = store.currentPeriod.year
    // 重挂载后把「刚建好账套」的提示接回来，并直接把弹窗开出来，
    // 免得用户建完账不知道下一步该录期初
    const last = store.lastCreatedBookId
    if (last && store.books.some((x) => x.id === last)) {
      newBookId.value = last
      visible.value = true
    }
  } finally {
    loading.value = false
  }
})
</script>


.rule-block { width: 100%; }
.rule-row { display: flex; align-items: center; gap: 5px; margin-bottom: 8px; flex-wrap: wrap; }
.rule-label { width: 70px; text-align: right; color: var(--cwq-text); }
.rule-dash { color: var(--cwq-muted); }
.rule-note { margin-left: 10px; color: var(--cwq-danger); font-size: 12px; white-space: nowrap; }
.rule-sample { color: var(--cwq-ok); font-size: 14px; letter-spacing: 1px; margin-left: 75px; }
<style scoped>
/* 年度下拉选项：文字在左、「已结完」贴右且垂直居中 */
.year-opt { display: flex; align-items: center; }
.year-opt .el-tag { margin-left: auto; }
.page-title { margin: 0 0 2px; font-size: 18px; }
.ml { margin-left: 8px; }
.mt { margin-top: 8px; }
.mb { margin-bottom: 12px; }
.ok { color: var(--cwq-ok); font-weight: 600; }
</style>
