import { createServer } from 'vite'
import { after } from 'node:test'
import { fileURLToPath } from 'node:url'
export async function source(path) {
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), logLevel: 'silent', server: { middlewareMode: true, hmr: false } })
  after(() => server.close())
  return server.ssrLoadModule(path)
}
export function metadata(id = 'com.test.demo', digest = 'a'.repeat(64)) {
  return { id, name: 'Test', version: '1.0.0', digest,
    menu: { module: 'extensions', title: '扩展测试', icon: 'Grid', route: `/plugin/${id}` },
    entry: `/api/plugins/assets/${id}/${digest}/frontend/index.js`,
    styles: [`/api/plugins/assets/${id}/${digest}/frontend/style.css`] }
}
export function storage() {
  const values = new Map()
  globalThis.localStorage = { getItem: k => values.get(k) ?? null, setItem: (k, v) => values.set(k, String(v)), removeItem: k => values.delete(k) }
}
