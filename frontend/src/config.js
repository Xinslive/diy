/**
 * 运行时配置 —— **动静分离的接缝**。
 *
 * 为什么要单独一个模块：部署形态一旦分成「静态走 CDN、动态留源站」，就出现了
 * **两个必须在运行时才知道、但编译期不知道**的值：
 *
 *   · `apiBase`  —— API 在哪。源站同源时是 `/api`；前后端分域名时是
 *                   `https://api.example.com/api`（跨域，后端要开 CORS）。
 *   · `assetBase` —— 静态资源（带内容哈希的 JS/CSS）从哪取。走 CDN 时是
 *                   `https://cdn.example.com/`。
 *
 * 这两个值**不能烤进构建产物**：CI 每个 commit 只构建一份产物，而产物要能
 * 装到不同环境（生产 / 预发 / 本地）。烤进产物就等于「换环境要重新构建」，
 * 而发布脚本的约定是「取产物 → 装上去」，不重新构建。
 *
 * 所以它们走**运行时配置**：`public/runtime-config.js` 里放一个
 * `window.__CWQ_RUNTIME__`，发布时由 deploy 步骤改写这个文件（不改代码、
 * 不改构建）。页面在加载主模块之前先同步执行这个普通脚本，所以模块代码
 * 读到的永远是**当前这套部署**的值。
 *
 * **读不到时的回落是有意义的，不是「坏了」**：runtime-config.js 缺失
 * （比如 CDN 缓存了一份旧 HTML、配置文件还没同步过去）时回落到同源默认值
 * —— 那正是「源站同源部署」的行为，能继续用。反过来，回落到一个写错的绝对
 * 地址会让整站白屏，而默认同源永远是能跑的那个。
 */

/** 后端路由的字面前缀。插件资产路径、API 路径都以它开头。 */
export const API_ROOT = '/api'

/** 回落值：源站同源部署。 */
const FALLBACK_API_BASE = API_ROOT
const FALLBACK_ASSET_BASE = '/'

/**
 * 读全局运行时配置。
 *
 * 用 `globalThis` 而不是 `window`：这个模块会被测试用 vite 的 ssrLoadModule
 * 在 Node 里加载，那里没有 `window`，直接读 `window` 会抛 ReferenceError
 * —— 而抛错发生在模块顶层，整个 import 链直接断掉，测试报出来的是
 * 「模块加载失败」而不是「配置回落不对」，排查方向整个跑偏。
 *
 * 浏览器里 `globalThis === window`，所以取到的是同一个对象。
 */
function readRuntimeConfig() {
  const raw = typeof globalThis !== 'undefined' ? globalThis.__CWQ_RUNTIME__ : null
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
}

const runtime = readRuntimeConfig()

/**
 * 构建期兜底（`import.meta.env`）。
 *
 * 存在意义有两个：
 *   1. 本地 `vite dev` 不经过发布脚本，runtime-config.js 是仓库里的默认
 *      文件（内容与这里一致），所以正常用不到这一层；
 *   2. 但万一有人把 public/runtime-config.js 删了，构建期的值还能兜住，
 *      而不是让整站直接崩在「读不到配置」上。
 *
 * 注意 `import.meta.env` 在 Node 里由 vite 注入；在纯 Node 环境下这个文件
 * 若不经 vite 加载，`import.meta.env` 是 undefined —— 所以每处都写成
 * `?.`，不假设它一定在。
 */
function buildTime(name) {
  const env = typeof import.meta !== 'undefined' ? import.meta.env : null
  return env && typeof env === 'object' ? env[name] : undefined
}

/**
 * 归一化 API 基址：**去掉结尾斜杠**，保留绝对地址与路径前缀。
 *
 *   `/api`                      → `/api`
 *   `/api/`                     → `/api`
 *   `/`                         → `/`
 *   `/backend/api/`             → `/backend/api`
 *   `https://api.example.com/api/` → `https://api.example.com/api`
 *
 * 去尾斜杠是为了拼接时不出现 `/api//books`。根路径 `/` 是特例：去掉斜杠
 * 会变成空串，所以单独兜住。
 */
export function normalizeApiBase(value, fallback = FALLBACK_API_BASE) {
  if (typeof value !== 'string') return fallback
  const v = value.trim()
  if (!v) return fallback
  const trimmed = v.replace(/\/+$/, '')
  return trimmed || '/'
}

/**
 * 归一化静态资源基址：**保留结尾斜杠**。
 *
 * 它是目录前缀，拼接时后面直接跟 `assets/xxx.js`，所以结尾必须有斜杠，
 * 否则 `https://cdn.example.com` + `assets/x.js` 会拼成
 * `https://cdn.example.comassets/x.js`。
 */
export function normalizeAssetBase(value, fallback = FALLBACK_ASSET_BASE) {
  if (typeof value !== 'string') return fallback
  const v = value.trim()
  if (!v) return fallback
  return v.endsWith('/') ? v : v + '/'
}

/** 当前部署的 API 基址（已归一化）。 */
export const apiBase = normalizeApiBase(
  runtime.apiBase ?? buildTime('VITE_API_BASE'),
  FALLBACK_API_BASE
)

/** 当前部署的静态资源基址（已归一化，带结尾斜杠）。 */
export const assetBase = normalizeAssetBase(
  runtime.assetBase ?? buildTime('VITE_ASSET_BASE'),
  FALLBACK_ASSET_BASE
)

/**
 * 把后端给的**源站路径**换成当前部署该用的 URL。
 *
 * 后端返回的路径一律以 `/api` 开头（那是后端路由的字面前缀，与部署无关），
 * 但浏览器要用它去请求的地方可能是：
 *   · 源站同源：`/api/plugins/assets/...` —— 原样；
 *   · API 在别的域名/别的路径前缀：`https://api.example.com/api/plugins/...`
 *     或 `/backend/api/plugins/...`。
 *
 * 所以规则是：**剥掉后端那个字面 `/api`，接上配置的 `apiBase`**。
 * 同源时 `apiBase` 就是 `/api`，接回去等于原样 —— 这条规则在两种形态下
 * 给出同一个答案，不需要「同源走 A 分支、跨源走 B 分支」的分叉。
 *
 * 已经是绝对地址的（后端若哪天改成直接吐绝对地址）原样返回。
 */
export function toApiUrl(path) {
  if (typeof path !== 'string' || !path) return path
  if (/^https?:\/\//i.test(path)) return path
  if (path === API_ROOT) return apiBase
  if (path.startsWith(API_ROOT + '/')) return apiBase + path.slice(API_ROOT.length)
  return apiBase + (path.startsWith('/') ? path : '/' + path)
}

/**
 * 取出一个 URL 的**路径部分**（去掉 origin 与查询串）。
 *
 * 插件资产的校验要在「路径」上做（不许有 `..`、不许有 `//`），而后端可能
 * 吐相对路径，也可能吐绝对地址 —— 统一先剥成路径再校验，校验规则就只有
 * 一份，不会因为形态不同而松掉一半。
 */
export function pathOf(url) {
  if (typeof url !== 'string' || !url) return null
  if (/^https?:\/\//i.test(url)) {
    try {
      return new URL(url).pathname
    } catch {
      return null
    }
  }
  const q = url.indexOf('?')
  const h = url.indexOf('#')
  let end = url.length
  if (q >= 0 && q < end) end = q
  if (h >= 0 && h < end) end = h
  return url.slice(0, end)
}

/**
 * 把后端给的插件资产路径剥成「去掉 `/api` 前缀」的相对路径。
 *
 * 返回 null 表示「不是一条本系统该发的插件资产路径」—— 调用方据此拒掉。
 * 剥掉 `/api` 之后，剩下的部分要能被严格校验（见 plugins/manifest.js）。
 */
export function stripApiRoot(url) {
  const p = pathOf(url)
  if (typeof p !== 'string') return null
  if (p === API_ROOT) return ''
  if (p.startsWith(API_ROOT + '/')) return p.slice(API_ROOT.length)
  return null
}

/** 调试用：把当前生效的配置摊开给控制台看。 */
export function describeRuntime() {
  return { apiBase, assetBase, source: runtime && Object.keys(runtime).length ? 'runtime' : 'fallback' }
}
