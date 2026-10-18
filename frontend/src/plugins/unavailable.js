import { h } from 'vue'
export const PluginUnavailable = { render: () => h('section', { class: 'plugin-unavailable', role: 'alert' }, [
  h('h2', '扩展暂不可用'), h('p', '请返回其他页面，或联系管理员检查扩展状态与使用授权。'),
]) }
