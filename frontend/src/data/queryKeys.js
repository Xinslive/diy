/**
 * 查询 key 的稳定序列化工具。
 *
 * key 由用户、账套、期间和规范化后的查询条件组成。这里不依赖 JSON.stringify
 * 的对象插入顺序，避免两个语义相同但字段顺序不同的条件产生两份缓存。
 */
export const QUERY_CACHE_VERSION = 'v1'

function stable(value) {
  if (value === null || value === undefined) return value
  if (Array.isArray(value)) return value.map(stable)
  if (value instanceof Date) return value.toISOString()
  if (typeof value === 'object') {
    return Object.keys(value).sort().reduce((out, key) => {
      const next = value[key]
      if (next !== undefined) out[key] = stable(next)
      return out
    }, {})
  }
  return value
}
export function normalizeQueryKey(key) {
  if (typeof key === 'function') return normalizeQueryKey(key())
  if (Array.isArray(key)) return key.map(stable)
  return [stable(key)]
}

export function serializeQueryKey(key) {
  return JSON.stringify(normalizeQueryKey(key))
}

export function normalizeParams(params = {}) {
  return stable(params || {})
}

export function makeQueryKey(resource, context = {}) {
  return [
    QUERY_CACHE_VERSION,
    resource,
    context.userId ?? context.user ?? null,
    context.bookId ?? null,
    context.periodId ?? null,
    normalizeParams(context.params || {}),
  ]
}
