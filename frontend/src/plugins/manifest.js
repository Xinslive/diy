import { toApiUrl, stripApiRoot } from '@/config'

const ID = /^[a-z][a-z0-9]*(?:[.-][a-z][a-z0-9]*){1,10}$/
const DIGEST = /^[a-f0-9]{64}$/
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/
const ICONS = new Set(['Calendar', 'Grid', 'Document', 'DataAnalysis', 'Tools', 'Coin', 'Folder'])

/**
 * 校验插件 manifest，并产出**当前部署该用的**资产 URL。
 *
 * ------------------------------------------------------------------
 * 为什么校验与拼装要拆成两步
 * ------------------------------------------------------------------
 * 后端返回的资产路径是**后端自己的路由**（永远以 `/api` 开头，与部署无关）。
 * 浏览器要用它去请求的地方取决于部署形态：
 *
 *   · 前后端同源        → `/api/plugins/assets/...`（原样）
 *   · API 在别的域名    → `https://api.example.com/api/plugins/assets/...`
 *   · API 在别的路径前缀 → `/backend/api/plugins/assets/...`
 *
 * 所以**不能**把后端给的字符串原样丢给浏览器（跨域形态下它会打到前端所在
 * 的域上去），也**不能**直接放宽校验去接受绝对地址（那等于放行任意源的
 * 脚本 —— 插件资产是带票据的执行入口，这条边界必须硬）。
 *
 * 这里的做法是：**先剥成相对路径严校验，再由前端自己拼**。
 *   · 校验对象 = 剥掉 `/api` 之后的相对路径（必须落在
 *     `/plugins/assets/{id}/{digest}/frontend/` 之下、字符集受限、无 `..`、
 *     无 `//`、后缀正确）；
 *   · 拼装来源 = 校验过的相对路径 + 运维配的 `apiBase`。
 *
 * 于是最终 URL 的**来源与路径都是我们自己定的**，后端给的字符串只提供
 * 「哪个插件的哪一份资产」这一层信息。跨域形态下这条边界不比同源时松。
 *
 * ------------------------------------------------------------------
 * 与从前相比，哪一条被放松了
 * ------------------------------------------------------------------
 * 从前是「整条 URL 必须以 `/api/plugins/assets/...` 开头、且整条只含
 * `[a-zA-Z0-9_./-]`」。现在后端若吐绝对地址（`https://api.example.com/api/...`）
 * 也会被接受 —— 但那**不是新的口子**：剥出来的路径仍要过同一套严校验，
 * 而 origin 由 `apiBase` 决定（运维配的，不是后端能指定的）。
 * 真正被拒的仍然是：别的源（`https://evil.example/...`）、路径穿越（`..`）。
 *
 * **关于查询串**：校验只看**路径**（`pathOf` 先把 `?`/`#` 之后的部分剥掉），
 * 所以带查询串的资产 URL 不会被这里拒掉 —— 这不是疏忽，是**必须**这样：
 * 本地开发时 Vite 的 dev server 会把运行期才知道 URL 的动态 import 改写成
 * `__vite__injectQuery(url, 'import')`，浏览器实际请求的是
 * `…/frontend/index.js?import`。宿主若因此 404，插件页在本地就永远是
 * 「扩展暂不可用」。查询串不参与定位也不参与鉴权（定位靠路径、鉴权靠路径里
 * 的票据），宿主那边同样会忽略它。
 */
export function validatePlugin(source) {
  if (!source || typeof source !== 'object' || typeof source.id !== 'string' || !ID.test(source.id) || source.id.length > 96 || typeof source.digest !== 'string' || !DIGEST.test(source.digest) || typeof source.version !== 'string' || !VERSION.test(source.version)) throw new Error('插件标识无效')
  const menu = source.menu
  const prefix = `/plugin/${source.id}`
  if (!menu || menu.module !== 'extensions' || !ICONS.has(menu.icon)
    || typeof menu.title !== 'string' || !menu.title.trim() || menu.title.length > 128 || /[\x00-\x1f]/.test(menu.title)
    || typeof menu.route !== 'string' || !(menu.route === prefix || menu.route.startsWith(prefix + '/'))
    || !/^\/plugin\/[a-z0-9.-]+(?:\/[a-z0-9-]+)*$/.test(menu.route)) throw new Error('插件菜单超出命名空间')
  const name = `plugin:${source.id}:main`
  if ((menu.name && menu.name !== name) || (menu.key && menu.key !== name)) throw new Error('插件路由名称无效')
  // 票据段在 `frontend/` 之后（后端 _asset_url 的约定），这里按同样的边界校验。
  const relPrefix = `/plugins/assets/${source.id}/${source.digest}/frontend/`
  const checkAsset = (value, suffix) => {
    if (typeof value !== 'string' || !value.endsWith(suffix)) return false
    const rel = stripApiRoot(value)
    if (typeof rel !== 'string' || !rel.startsWith(relPrefix)) return false
    const rest = rel.slice(relPrefix.length)
    return /^[a-zA-Z0-9_./-]+$/.test(rest) && !rest.includes('//') && !rest.split('/').some(p => p === '.' || p === '..')
  }
  if (!checkAsset(source.entry, '.js') || !Array.isArray(source.styles || []) || (source.styles || []).some(p => !checkAsset(p, '.css'))) throw new Error('插件资产必须是同源不可变路径')
  return { id: source.id, version: source.version, digest: source.digest, entry: toApiUrl(source.entry),
    styles: [...new Set(source.styles || [])].map(toApiUrl), menu: { ...menu, name, key: name } }
}
