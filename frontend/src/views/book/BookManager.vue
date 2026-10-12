<template>
  <div class="page book-page">
    <div class="book-page-head">
      <div class="book-page-heading">
        <h2 class="book-page-title">账套管理</h2>
        <span class="book-count">{{ store.books.length }} 个账套</span>
      </div>
      <!-- 谁都能建账套，建完自己就是 OWNER；管理员授权管的是别人的账套 -->
      <el-button type="primary" class="create-book-button" @click="openCreate">
        <el-icon><Plus /></el-icon>
        新建账套
      </el-button>
    </div>

    <section class="book-grid" v-loading="loading" aria-label="账套列表">
      <article
        v-for="row in store.books"
        :key="row.id"
        class="book-tile"
        :class="{ 'is-current': row.id === store.bookId }"
        :aria-label="`账套：${row.name}`"
      >
        <div class="book-tile-head">
          <div class="book-tile-ident">
            <span class="book-tile-icon" aria-hidden="true"><el-icon><Folder /></el-icon></span>
            <div class="book-tile-title">
              <el-link class="book-name-link" type="primary" @click="switchTo(row.id, pickedYear(row))">
                {{ row.name }}
              </el-link>
              <el-tag v-if="row.id === store.bookId" size="small" type="success">当前</el-tag>
            </div>
          </div>
          <el-tag :type="yearOf(row).carried ? 'info' : 'success'" size="small">
            {{ yearOf(row).carried ? '已结转' : '使用中' }}
          </el-tag>
        </div>

        <div class="book-tile-meta">
          <div class="book-fact">
            <span class="book-fact-label">会计年度</span>
            <!-- 默认停在最新年度；选择其他年度会同步到顶栏 -->
            <el-select
              :model-value="pickedYear(row)"
              class="book-year-select"
              size="small"
              @change="(v) => onYearPick(row, v)"
              @click.stop
            >
              <el-option v-for="y in row.years" :key="y.year" :label="`${y.year} 年`" :value="y.year">
                <span class="year-opt">
                  <span>{{ y.year }} 年</span>
                  <el-tag v-if="y.carried" size="small" type="info">已结完</el-tag>
                </span>
              </el-option>
            </el-select>
          </div>
          <div class="book-fact book-fact-closed">
            <span class="book-fact-label">已结账</span>
            <strong :class="yearOf(row).carried ? 'ok' : ''">
              {{ yearOf(row).closed }} / {{ yearOf(row).total }} 期
            </strong>
          </div>
        </div>

        <div class="book-tile-actions">
          <el-button type="primary" plain class="enter-book-button" @click="switchTo(row.id, pickedYear(row))">
            进入账套<el-icon><Right /></el-icon>
          </el-button>
          <el-tooltip content="编辑账套" placement="top">
            <el-button circle class="edit-book-button" :aria-label="`编辑账套 ${row.name}`" @click="openEdit(row)">
              <el-icon><Edit /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </article>

      <el-empty
        v-if="!loading && !store.books.length"
        description="还没有账套，点右上角「新建账套」自己开一个；也可以请管理员在「用户与授权」里把已有账套授权给你"
      />
    </section>

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
import { Edit, Folder, Plus, Right } from '@element-plus/icons-vue'
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
.book-page { padding: 24px; }
.book-page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.book-page-heading { display: flex; align-items: center; gap: 12px; }
.book-page-title {
  margin: 0;
  color: var(--cwq-chrome-strong);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.35;
}
.book-count {
  padding-left: 12px;
  border-left: 1px solid #d9dfe7;
  color: #778292;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.create-book-button { min-height: 38px; padding: 0 16px; }
.create-book-button :deep(.el-icon) { margin-right: 5px; }
.book-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr));
  align-items: stretch;
  gap: 12px;
}
.book-tile {
  display: flex;
  min-width: 0;
  min-height: 170px;
  flex-direction: column;
  padding: 14px 16px 12px;
  background: #fff;
  border: 1px solid #e2e7ed;
  border-radius: 8px;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}
.book-tile:hover {
  border-color: #cbd6e2;
  box-shadow: 0 5px 16px rgb(31 39 51 / 6%);
}
.book-tile.is-current {
  background: #f9fbff;
  border-color: #c5d8f0;
  box-shadow: inset 3px 0 0 var(--cwq-primary);
}
.book-tile-head { display: flex; min-height: 42px; align-items: flex-start; justify-content: space-between; gap: 10px; }
.book-tile-ident { display: flex; min-width: 0; align-items: flex-start; gap: 9px; }
.book-tile-icon {
  display: grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  color: #68788b;
  background: #f1f4f7;
  border-radius: 7px;
  font-size: 15px;
}
.book-tile.is-current .book-tile-icon { color: #2864a8; background: #eaf2fc; }
.book-tile-title {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 7px;
  padding-top: 3px;
}
.book-name-link {
  min-width: 0;
  color: #2c3848;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
  text-align: left;
  white-space: normal;
}
.book-name-link :deep(.el-link__inner) { text-align: left; white-space: normal; overflow-wrap: anywhere; }
.book-tile-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  margin-top: 8px;
  padding: 9px 0;
  border-top: 1px solid #edf0f3;
  border-bottom: 1px solid #edf0f3;
}
.book-fact { display: flex; min-width: 0; align-items: center; gap: 7px; }
.book-fact + .book-fact { padding-left: 18px; border-left: 1px solid #edf0f3; }
.book-fact-label { flex: 0 0 auto; color: #8993a1; font-size: 12px; line-height: 1.4; }
.book-fact strong { color: #3a4655; font-size: 14px; font-weight: 600; }
.book-year-select { width: 100px; }
.book-tile-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; padding-top: 8px; }
.enter-book-button { min-width: 92px; }
.enter-book-button :deep(.el-button__text) { font-size: 12px; }
.enter-book-button :deep(.el-icon) { margin-left: 6px; }
.enter-book-button { height: 30px; padding: 0 10px; }
.edit-book-button { width: 30px; height: 30px; }
/* 年度下拉选项：文字在左、「已结完」贴右且垂直居中 */
.year-opt { display: flex; align-items: center; }
.year-opt .el-tag { margin-left: auto; }
.mt { margin-top: 8px; }
.mb { margin-bottom: 12px; }
.ok { color: var(--cwq-ok); font-weight: 600; }

@media (max-width: 700px) {
  .book-page { padding: 16px; }
  .book-page-head { margin-bottom: 18px; }
  .book-page-title { font-size: 20px; }
  .book-grid { grid-template-columns: 1fr; gap: 10px; }
  .book-tile { min-height: 166px; padding: 14px; }
}
@media (max-width: 420px) {
  .book-page-heading { align-items: flex-start; flex-direction: column; gap: 4px; }
  .book-count { padding-left: 0; border-left: 0; }
  .book-page-head { align-items: flex-end; }
  .create-book-button { padding: 0 11px; }
  .book-tile-meta { gap: 8px; }
  .book-fact { align-items: flex-start; flex-direction: column; gap: 4px; }
  .book-fact + .book-fact { padding-left: 12px; }
  .book-year-select { width: 110px; }
}
</style>
