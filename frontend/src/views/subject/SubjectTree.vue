<template>
  <div class="page">
    <div class="toolbar no-print">
      <el-input v-model="keyword" placeholder="按编码或名称筛选" clearable style="width: 240px" />
      <span class="muted">{{ year }} 年度科目表</span>
      <span class="muted">科目编码规则 {{ rule }}</span>
      <span v-if="isFiltering" class="muted">命中 {{ hitCount }} 个科目</span>
      <span class="spacer" />
      <el-button :disabled="!store.canWrite" @click="importVisible = true">
        <el-icon><Upload /></el-icon>&nbsp;导入并覆盖
      </el-button>
      <el-button type="primary" :disabled="!store.canWrite" @click="openCreate()">新增科目</el-button>
    </div>

    <div class="card">
      <el-table :data="filtered" row-key="id" border default-expand-all
                :tree-props="{ children: 'children' }" v-loading="loading">
        <!-- 编码列的宽度按本账套的编码规则算，见下面 codeWidth 的注释。
             写死一个数只对某一种规则成立，换个多级账套就折行。 -->
        <el-table-column label="编码" :width="codeWidth" prop="code" />
        <el-table-column label="科目名称" min-width="240" prop="name" />
        <el-table-column label="类别" width="90" align="center">
          <template #default="{ row }">{{ CATEGORY_LABEL[row.category] || row.category }}</template>
        </el-table-column>
        <!-- 表头写死断行：80px 扣掉左右各 12px 内边距只剩 56px，
             刚好卡在「四个 14px 的字」上，于是折成「余额方 / 向」——
             断在词中间。要的是「余额 / 方向」两个词，所以不靠宽度碰运气。 -->
        <el-table-column width="80" align="center">
          <template #header>余额<br>方向</template>
          <template #default="{ row }">{{ row.direction === 'DEBIT' ? '借' : '贷' }}</template>
        </el-table-column>
        <el-table-column v-if="store.canWrite" label="操作" width="190" align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="openCreate(row)">下级</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <SubjectFormDialog v-model="visible" :tree="tree" :parent-id="parentId" :year="year" />
    <SubjectEditDialog v-model="editVisible" :subject="editingSubject" @saved="load" />
    <SubjectImportDialog v-model="importVisible" :year="year" @imported="onImported" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload } from '@element-plus/icons-vue'
import * as api from '@/api/subject'
import SubjectFormDialog from '@/components/SubjectFormDialog.vue'
import SubjectEditDialog from '@/components/SubjectEditDialog.vue'
import SubjectImportDialog from '@/components/SubjectImportDialog.vue'
import { useSessionStore } from '@/stores/session'
import { CATEGORY_LABEL } from '@/utils/subjectCode'

const store = useSessionStore()
const tree = ref([])
const loading = ref(false)
const keyword = ref('')
const visible = ref(false)
const parentId = ref(null)
const editVisible = ref(false)
const editingSubject = ref(null)
const importVisible = ref(false)

/**
 * **当前年度**。
 *
 * 科目表每个年度各有一份（后端迁移 0032）：2026 与 2027 各有自己的
 * 科目表、各有自己的期初。这一页看的是顶栏选中的那一年 ——
 * 「导入并覆盖」也只覆盖那一年，别的年度的科目表一行都不动。
 */
const year = computed(() => store.currentYear || store.currentBook?.latest_year || 0)

const rule = computed(() => store.currentBook?.subject_code_rule || '4-2-2-2')

/**
 * 编码列宽按**本账套的编码规则**算，不写死。
 *
 * 科目树的缩进和展开图标都压在这一列里，最深一级实际要占：
 *   缩进（级数 − 1）× 16px + 展开图标 23px + 编码本身「各级段长之和」位数字
 *   （实测约 8.8px/位，这里按 9px 留余量）+ 单元格左右各 12px 内边距。
 *
 * 写死 180px 只够 4-2-2-2：碰上 4-2-2-2-2 这种五级账套，最深的十二位编码
 * 放不下就会折成两行 —— .cell 的行高跟着翻倍，整行比别的行高一截，
 * 看着就像这行错位了。下限 120px，免得短编码的账套白占地方。
 */
const codeWidth = computed(() => {
  const parts = String(rule.value).split('-').map(Number)
  const digits = parts.reduce((sum, n) => sum + n, 0)
  return Math.max(120, (parts.length - 1) * 16 + 23 + digits * 9 + 24)
})

// ------------------------------------------------------------------ 搜索
//
// 筛选放在前端做：整棵树本来就一次性铺在内存里（表格也没有分页，
// 三千个科目照样全渲染），再为每敲一个键往回要一趟数据是白折腾。
//
// 命中项**连同它下面整棵子树**一起留下 —— 科目是表，搜到 1122 却看不到
// 112201/112202，等于还得再搜一次。不命中的节点只保留有命中的后代那条路径，
// 祖先必须留着：剪枝时把父节点剪掉，这行就成了没有位置的孤儿，
// el-table 按 parent_id 找不着它的上级，整行直接不显示。
function pruneTree(nodes, kw) {
  const out = []
  for (const node of nodes) {
    const children = pruneTree(node.children || [], kw)
    const hit = String(node.code || '').toLowerCase().includes(kw)
      || String(node.name || '').toLowerCase().includes(kw)
    if (hit || children.length) out.push({ ...node, children })
  }
  return out
}

const isFiltering = computed(() => !!keyword.value.trim())
const filtered = computed(() =>
  isFiltering.value ? pruneTree(tree.value, keyword.value.trim().toLowerCase()) : tree.value)

const hitCount = computed(() => {
  if (!isFiltering.value) return 0
  let total = 0
  const walk = (nodes) => nodes.forEach((node) => { total += 1; walk(node.children || []) })
  walk(filtered.value)
  return total
})

async function load() {
  if (!store.bookId || !year.value) return
  loading.value = true
  try {
    tree.value = await api.subjectTree(store.bookId, { year: year.value })
  } finally { loading.value = false }
}

function openCreate(parent = null) {
  parentId.value = parent?.id || null
  visible.value = true
}

function openEdit(subject) {
  editingSubject.value = subject
  editVisible.value = true
}

async function onImported() {
  await store.loadBooks()
  await load()
  ElMessage.success('科目表已覆盖导入')
}

async function remove(row) {
  await ElMessageBox.confirm(`确认删除科目 ${row.code} ${row.name}？`, '删除', { type: 'warning' })
  await api.deleteSubject(store.bookId, row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
// 换账套、换年度都要重取：另一个年度是**另一张**科目表
watch(() => [store.bookId, year.value], load)
</script>
