const ID = /^[a-z][a-z0-9]*(?:[.-][a-z][a-z0-9]*){1,10}$/
const DIGEST = /^[a-f0-9]{64}$/
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/
const ICONS = new Set(['Calendar', 'Grid', 'Document', 'DataAnalysis', 'Tools', 'Coin', 'Folder'])

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
  const assetPrefix = `/api/plugins/assets/${source.id}/${source.digest}/frontend/`
  const checkAsset = (value, suffix) => typeof value === 'string' && value.startsWith(assetPrefix)
    && /^[a-zA-Z0-9_./-]+$/.test(value) && !value.includes('//') && !value.split('/').some(p => p === '.' || p === '..') && value.endsWith(suffix)
  if (!checkAsset(source.entry, '.js') || !Array.isArray(source.styles || []) || (source.styles || []).some(p => !checkAsset(p, '.css'))) throw new Error('插件资产必须是同源不可变路径')
  return { id: source.id, version: source.version, digest: source.digest, entry: source.entry,
    styles: [...new Set(source.styles || [])], menu: { ...menu, name, key: name } }
}
