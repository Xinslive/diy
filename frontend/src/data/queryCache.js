import { serializeQueryKey } from './queryKeys.js'

export const CACHE_DEFAULTS = Object.freeze({
  metadata: Object.freeze({ staleTime: 120_000, cacheTime: 600_000 }),
  list: Object.freeze({ staleTime: 15_000, cacheTime: 300_000 }),
  ledger: Object.freeze({ staleTime: 30_000, cacheTime: 300_000 }),
  immediate: Object.freeze({ staleTime: 0, cacheTime: 300_000 }),
})

const entries = new Map()
const inflight = new Map()
const listeners = new Map()
const globalListeners = new Set()
const keyVersions = new Map()
let cacheEpoch = 0

function now() {
  return Date.now()
}

function notify(key, event) {
  const fullEvent = { ...event, key: event.key ?? key }
  const set = listeners.get(key)
  for (const listener of [...(set || [])]) {
    try { listener(fullEvent) } catch { /* one subscriber must not break the cache */ }
  }
  for (const listener of [...globalListeners]) {
    try { listener(fullEvent) } catch { /* one subscriber must not break the cache */ }
  }
}

function optionsFor(options = {}) {
  const defaults = CACHE_DEFAULTS[options.profile] || CACHE_DEFAULTS.list
  return {
    staleTime: options.staleTime ?? defaults.staleTime,
    cacheTime: options.cacheTime ?? defaults.cacheTime,
    tags: Array.isArray(options.tags) ? [...new Set(options.tags.filter(Boolean))] : [],
    scope: options.scope || null,
  }
}

function isExpired(entry, time = now()) {
  return !entry || time - entry.updatedAt > entry.cacheTime
}

function isStale(entry, time = now()) {
  return !entry || entry.invalidated === true || time - entry.updatedAt >= entry.staleTime
}

function readEntry(serialized) {
  const entry = entries.get(serialized)
  if (!entry) return null
  if (isExpired(entry)) {
    entries.delete(serialized)
    notify(serialized, { type: 'evict', key: serialized })
    return null
  }
  return entry
}

function startFetch(serialized, key, query, options) {
  const current = inflight.get(serialized)
  const version = keyVersions.get(serialized) || 0
  if (current?.version === version) return current.promise
  const startedAtEpoch = cacheEpoch
  const existingAtStart = readEntry(serialized)
  const background = Boolean(existingAtStart)

  const promise = Promise.resolve().then(query).then((data) => {
    if (startedAtEpoch !== cacheEpoch || version !== (keyVersions.get(serialized) || 0)) return data
    const previous = entries.get(serialized)
    const policy = optionsFor({ ...previous, ...options })
    const entry = {
      key,
      data,
      updatedAt: now(),
      invalidated: false,
      staleTime: policy.staleTime,
      cacheTime: policy.cacheTime,
      tags: policy.tags.length ? policy.tags : (previous?.tags || []),
      scope: policy.scope || previous?.scope || null,
      query,
      options: { ...options },
    }
    entries.set(serialized, entry)
    notify(serialized, { type: 'success', key: serialized, data, entry })
    return data
  }).catch((error) => {
    if (startedAtEpoch !== cacheEpoch || version !== (keyVersions.get(serialized) || 0)) throw error
    const entry = entries.get(serialized)
    notify(serialized, { type: 'error', key: serialized, error, entry, background })
    throw error
  }).finally(() => {
    if (inflight.get(serialized)?.promise === promise) inflight.delete(serialized)
  })

  const requestPolicy = optionsFor(options)
  inflight.set(serialized, {
    version,
    promise,
    tags: requestPolicy.tags,
    scope: requestPolicy.scope,
  })
  notify(serialized, { type: 'fetching', key: serialized, background })
  return promise
}

export const queryCache = {
  /** Return a live snapshot without starting a request. */
  peek(key) {
    const serialized = serializeQueryKey(key)
    const entry = readEntry(serialized)
    if (!entry) return null
    return {
      key: entry.key,
      data: entry.data,
      updatedAt: entry.updatedAt,
      stale: isStale(entry),
      age: now() - entry.updatedAt,
      tags: [...entry.tags],
      scope: entry.scope,
    }
  },

  has(key) {
    return Boolean(this.peek(key))
  },

  /** Start or join one request. The promise is shared by all callers of the key. */
  fetch(key, query, options = {}) {
    const serialized = serializeQueryKey(key)
    const existing = readEntry(serialized)
    const policy = optionsFor(options)
    if (existing) {
      // Preserve the most recent policy/tags when a later component supplies them.
      existing.staleTime = policy.staleTime
      existing.cacheTime = policy.cacheTime
      if (policy.tags.length) existing.tags = policy.tags
      if (policy.scope) existing.scope = policy.scope
      existing.query = query
      existing.options = { ...existing.options, ...options }
    }
    return startFetch(serialized, key, query, options)
  },

  subscribe(key, listener) {
    const serialized = serializeQueryKey(key)
    let set = listeners.get(serialized)
    if (!set) {
      set = new Set()
      listeners.set(serialized, set)
    }
    set.add(listener)
    return () => {
      set.delete(listener)
      if (!set.size) listeners.delete(serialized)
    }
  },

  subscribeAll(listener) {
    globalListeners.add(listener)
    return () => globalListeners.delete(listener)
  },

  invalidate({ key, keys, tags = [], scope, bookId } = {}) {
    const exact = new Set([...(keys || []), ...(key ? [key] : [])].map(serializeQueryKey))
    const wantedTags = new Set(tags)
    const bookScope = bookId === undefined || bookId === null ? null : `book:${bookId}`
    const removed = []
    const refresh = []
    const hasSelector = exact.size > 0 || wantedTags.size > 0 || Boolean(scope) || bookScope
    const versioned = new Set()
    for (const [serialized, entry] of entries) {
      const exactMatch = exact.has(serialized)
      const tagMatch = wantedTags.size > 0 && [...wantedTags].some((tag) => entry.tags.includes(tag))
      const scopeMatch = scope ? entry.scope === scope : true
      const bookMatch = bookScope ? entry.tags.includes(bookScope) || entry.scope === bookScope : true
      if ((!hasSelector || exactMatch || tagMatch) && scopeMatch && bookMatch) {
        // Keep the last usable value so active pages can continue rendering it while
        // the invalidated query is fetched again in the background.
        keyVersions.set(serialized, (keyVersions.get(serialized) || 0) + 1)
        versioned.add(serialized)
        entry.invalidated = true
        entry.updatedAt = now()
        removed.push(serialized)
        notify(serialized, { type: 'invalidate', key: serialized })
        if (listeners.get(serialized)?.size && typeof entry.query === 'function') {
          refresh.push({ serialized, entry })
        }
      }
    }
    for (const [serialized, request] of inflight) {
      if (versioned.has(serialized)) continue
      const tagMatch = wantedTags.size > 0 && [...wantedTags].some((tag) => request.tags?.includes(tag))
      const scopeMatch = scope ? request.scope === scope : true
      const bookMatch = bookScope
        ? request.tags?.includes(bookScope) || request.scope === bookScope
        : true
      const exactMatch = exact.has(serialized)
      if ((!hasSelector || exactMatch || tagMatch) && scopeMatch && bookMatch) {
        keyVersions.set(serialized, (keyVersions.get(serialized) || 0) + 1)
        versioned.add(serialized)
      }
    }
    for (const { serialized, entry } of refresh) {
      void startFetch(serialized, entry.key, entry.query, entry.options).catch(() => {})
    }
    return removed.length
  },

  remove({ scope, bookId } = {}) {
    const bookScope = bookId === undefined || bookId === null ? null : `book:${bookId}`
    const removed = []
    const versioned = new Set()
    for (const [serialized, entry] of entries) {
      const scopeMatch = !scope || entry.scope === scope
      const bookMatch = !bookScope || entry.tags.includes(bookScope)
      if (scopeMatch && bookMatch) {
        entries.delete(serialized)
        keyVersions.set(serialized, (keyVersions.get(serialized) || 0) + 1)
        versioned.add(serialized)
        removed.push(serialized)
        notify(serialized, { type: 'invalidate', key: serialized })
      }
    }
    for (const [serialized, request] of inflight) {
      if (versioned.has(serialized)) continue
      const scopeMatch = !scope || request.scope === scope
      const bookMatch = !bookScope || request.tags?.includes(bookScope) || request.scope === bookScope
      if (scopeMatch && bookMatch) {
        keyVersions.set(serialized, (keyVersions.get(serialized) || 0) + 1)
      }
    }
    return removed.length
  },

  clear() {
    const keys = [...entries.keys()]
    cacheEpoch += 1
    entries.clear()
    inflight.clear()
    keyVersions.clear()
    for (const key of keys) notify(key, { type: 'invalidate', key })
    return keys.length
  },

  size() {
    return entries.size
  },

  _resetForTests() {
    cacheEpoch += 1
    entries.clear()
    inflight.clear()
    listeners.clear()
    globalListeners.clear()
    keyVersions.clear()
  },
}

export default queryCache
