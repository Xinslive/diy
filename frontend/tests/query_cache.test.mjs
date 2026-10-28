import assert from 'node:assert/strict'
import queryCache from '../src/data/queryCache.js'
import { serializeQueryKey } from '../src/data/queryKeys.js'

const waitFor = (predicate, timeout = 500) => new Promise((resolve, reject) => {
  const started = Date.now()
  const tick = () => {
    if (predicate()) return resolve()
    if (Date.now() - started > timeout) return reject(new Error('condition timed out'))
    setTimeout(tick, 0)
  }
  tick()
})

queryCache._resetForTests()

assert.notEqual(
  serializeQueryKey(['v1', 'voucher-list', 1, 7, 11, { page: 1 }]),
  serializeQueryKey(['v1', 'voucher-list', 2, 7, 11, { page: 1 }]),
  'different users must not share a query key'
)
assert.notEqual(
  serializeQueryKey(['v1', 'voucher-list', 1, 7, 11, { page: 1 }]),
  serializeQueryKey(['v1', 'voucher-list', 1, 8, 11, { page: 1 }]),
  'different books must not share a query key'
)
assert.equal(
  serializeQueryKey(['v1', 'voucher-list', 1, 7, 11, { b: 2, a: 1 }]),
  serializeQueryKey(['v1', 'voucher-list', 1, 7, 11, { a: 1, b: 2 }]),
  'query parameters are normalized before serialization'
)

const key = ['v1', 'voucher-list', 11, 7, 202601, { page: 1 }]
let calls = 0
let release
const pending = new Promise((resolve) => { release = resolve })
const query = async () => {
  calls += 1
  await pending
  return { rows: [calls] }
}

const first = queryCache.fetch(key, query, {
  profile: 'list', tags: ['vouchers'], scope: 'book'
})
const second = queryCache.fetch(key, query, {
  profile: 'list', tags: ['vouchers'], scope: 'book'
})
assert.strictEqual(first, second, 'concurrent callers must receive the same promise')
assert.equal(calls, 0, 'query starts on the microtask boundary')
release()
assert.deepEqual(await first, { rows: [1] })
assert.equal(calls, 1, 'same-key concurrent calls must make one request')
assert.deepEqual(queryCache.peek(key)?.data, { rows: [1] })

const events = []
const stop = queryCache.subscribe(key, (event) => events.push(event.type))
let refreshed = 0
queryCache.fetch(key, async () => ({ rows: [++refreshed + 1] }), {
  profile: 'list', tags: ['vouchers', 'book:7'], scope: 'book'
})
await waitFor(() => queryCache.peek(key)?.data?.rows?.[0] === 2)
queryCache.invalidate({ scope: 'book', bookId: 7, tags: ['vouchers'] })
await waitFor(() => events.filter((type) => type === 'success').length >= 2)
assert.deepEqual(queryCache.peek(key)?.data, { rows: [3] })
assert.equal(queryCache.peek(key).stale, false, 'active invalidation must refresh the visible query')
assert.ok(events.includes('fetching'), 'refresh emits a fetching event')
stop()

const racedKey = ['v1', 'voucher-list', 11, 7, 202601, { page: 2 }]
let releaseOld
const oldRequest = queryCache.fetch(racedKey, () => new Promise((resolve) => { releaseOld = resolve }), {
  profile: 'list', tags: ['vouchers', 'book:7'], scope: 'book'
})
await waitFor(() => typeof releaseOld === 'function')
queryCache.invalidate({ scope: 'book', bookId: 7, tags: ['vouchers'] })
await queryCache.fetch(racedKey, async () => ({ rows: ['after-write'] }), {
  profile: 'list', tags: ['vouchers', 'book:7'], scope: 'book'
})
releaseOld({ rows: ['before-write'] })
await oldRequest
assert.deepEqual(queryCache.peek(racedKey)?.data, { rows: ['after-write'] },
  'an in-flight pre-write response must not repopulate an invalidated cache entry')

const old = queryCache.peek(key)?.data
await assert.rejects(() => queryCache.fetch(key, async () => { throw new Error('offline') }, {
  profile: 'list', tags: ['vouchers'], scope: 'book'
}))
assert.deepEqual(queryCache.peek(key)?.data, old, 'refresh failure retains the last usable data')

queryCache._resetForTests()
console.log('query cache tests passed')
