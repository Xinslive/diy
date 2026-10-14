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
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>