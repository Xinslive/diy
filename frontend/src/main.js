import { createApp } from 'vue'
import { createPinia } from 'pinia'
// Element Plus 改成按需引入之后（见 vite.config.js 的 ElementPlusResolver），
// 全局 CSS 不再整包 import。ElMessage / ElMessageBox 这些是**命令式**调用，
// 不出现在任何模板里，解析器扫不到，样式得在这里单独带进来 ——
// 漏了的话报错/确认框会以「完全没有样式」的裸 HTML 弹出来。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'

import App from './App.vue'
import router from './router'
import './styles/main.css'
import { onUnauthorized } from './api/client'
import { useSessionStore } from './stores/session'

const pinia = createPinia()
const app = createApp(App)
// 不再 app.use(ElementPlus)：整包注册会把 1111KB 塞进阻塞式入口包，
// 而登录页其实只用得着 el-input / el-button。
// 语言（locale）改由 App.vue 的 <el-config-provider> 提供 ——
// 那是按需引入下配 locale 的正规位置，效果与原来的
// app.use(ElementPlus, { locale: zhCn }) 一致。
app.use(pinia).use(router)

// 401 的善后交给 store 在这里装配，而不是让 client.js 直接 import session
// —— 那是 client → session → api/auth → client 的环，ESM 下不报错，
// 只是静默拿到 undefined。见 api/client.js 里 onUnauthorized 的注释。
onUnauthorized(() => useSessionStore(pinia).logout())

app.mount('#app')