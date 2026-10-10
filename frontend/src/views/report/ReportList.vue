<template>
  <div class="page">
    <div class="page-head"><h2 class="page-title">报表</h2></div>
    <div class="card">
      <p class="muted" style="margin-top:0">
        报表模板与账套无关，公式里引用的是科目编码，因此一份模板可直接用于所有账套。
        点击报表进入后，可在单元格上直接改公式。
      </p>
      <el-table :data="templates" border style="margin-top:12px">
        <el-table-column label="报表名称" min-width="200">
          <template #default="{ row }">
            <router-link :to="`/reports/${row.id}`" style="color:var(--cwq-primary);text-decoration:none">
              {{ row.name }}
            </router-link>
          </template>
        </el-table-column>
        <el-table-column label="编码" width="160" prop="code" />
        <el-table-column label="类型" width="140">
          <template #default="{ row }">
            <el-tag size="small">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内置" width="90" align="center">
          <template #default="{ row }">{{ row.is_builtin ? '是' : '否' }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import * as api from '@/api/report'

const templates = ref([])
const typeLabel = (t) => ({ BALANCE_SHEET: '资产负债表', INCOME_SHEET: '利润表', CUSTOM: '自定义' }[t] || t)
onMounted(async () => { templates.value = await api.templates() })
</script>
