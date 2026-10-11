import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './styles/main.css'
import { onUnauthorized } from './api/client'
import { useSessionStore } from './stores/session'

const pinia = createPinia()
const app = createApp(App)
app.use(pinia).use(router).use(ElementPlus, { locale: zhCn })

// 401 的善后交给 store 在这里装配，而不是让 client.js 直接 import session
// —— 那是 client → session → api/auth → client 的环，ESM 下不报错，
// 只是静默拿到 undefined。见 api/client.js 里 onUnauthorized 的注释。
onUnauthorized(() => useSessionStore(pinia).logout())

app.mount('#app')
