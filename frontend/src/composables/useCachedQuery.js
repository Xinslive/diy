import { computed, onUnmounted, ref, watch } from 'vue'
import queryCache from '@/data/queryCache'
import { serializeQueryKey } from '@/data/queryKeys'

/**
 * 页面和弹窗共用的查询状态。
 *
 * 数据存在时，刷新过程只切换 refreshing，不清空 data；只有首次没有数据时
 * 才会把 loading 置为 true。key 改变后旧响应不会写回当前条件。
 */
export function useCachedQuery(options) {
  const data = ref(options.initialData ?? null)
  const loading = ref(false)
  const refreshing = ref(false)
  const error = ref(null)
  const hasData = computed(() => data.value !== null && data.value !== undefined)
  let generation = 0
  let stopSubscription = null
  let previousKey = null

  function enabled() {
    return options.enabled === undefined
      || (typeof options.enabled === 'function' ? options.enabled() : options.enabled)
  }

  function currentKey() {
    return typeof options.key === 'function' ? options.key() : options.key
  }

  function attach(key, mine) {
    stopSubscription?.()
    stopSubscription = queryCache.subscribe(key, (event) => {
      if (mine !== generation) return
      if (event.type === 'success') {
        data.value = event.data
        error.value = null
        loading.value = false
        refreshing.value = false
      } else if (event.type === 'fetching') {
        if (hasData.value) refreshing.value = true
      } else if (event.type === 'error') {
        error.value = event.error
        loading.value = false
        refreshing.value = false
      } else if (event.type === 'invalidate') {
        if (hasData.value) refreshing.value = true
      }
    })
  }

  async function run(force = false) {
    const mine = ++generation
    stopSubscription?.()
    stopSubscription = null
    error.value = null
    if (!enabled()) {
      loading.value = false
      refreshing.value = false
      return data.value
    }

    const key = currentKey()
    const serializedKey = serializeQueryKey(key)
    const keyChanged = previousKey !== null && previousKey !== serializedKey
    if (keyChanged && typeof options.clearOnContextChange === 'function'
        && options.clearOnContextChange(key, previousKey)) {
      data.value = null
    }
    previousKey = serializedKey
    const cached = queryCache.peek(key)
    if (cached) {
      data.value = cached.data
      loading.value = false
      refreshing.value = true
    } else {
      loading.value = true
      refreshing.value = false
    }
    attach(key, mine)

    try {
      const result = await queryCache.fetch(
        key,
        () => options.query(),
        { ...options, force }
      )
      if (mine !== generation) return result
      data.value = result
      error.value = null
      loading.value = false
      refreshing.value = false
      return result
    } catch (e) {
      if (mine !== generation) return undefined
      // Stale data remains visible after a refresh failure.
      error.value = e
      loading.value = false
      refreshing.value = false
      return undefined
    }
  }

  function refresh() {
    return run(true)
  }

  function invalidate() {
    const key = currentKey()
    queryCache.invalidate({ key })
    if (enabled()) return run(true)
    return Promise.resolve()
  }

  watch(currentKey, () => { void run() }, { immediate: true, deep: true })
  onUnmounted(() => stopSubscription?.())

  return { data, loading, refreshing, error, hasData, refresh, invalidate }
}

export default useCachedQuery
