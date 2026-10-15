<template>
  <!-- 按需引入后语言改由这里提供：<el-config-provider> 是 Element Plus 在
       不整包注册时配 locale 的正规位置，等价于原来
       app.use(ElementPlus, { locale: zhCn })，但不用把整个库注册进来。
       组件本体由 unplugin-vue-components 的 ElementPlusResolver 自动引入。 -->
  <el-config-provider :locale="zhCn">
    <router-view />
  </el-config-provider>
</template>

<script setup>
import { onBeforeUnmount } from 'vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { installCellSelection } from '@/composables/useCellSelection'

// 表格矩形选区是**全局**的：装一次，全系统所有表格（裸 table、el-table、
// 打印台拼出来的）都按同一个形状选，不用每张表各接一遍。setup 跑的时候
// document 已经在，直接装即可；组件卸载时拆掉。
const removeSelection = installCellSelection()
onBeforeUnmount(removeSelection)
</script>