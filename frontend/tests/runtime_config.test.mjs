/**
 * 运行时配置（src/config.js）—— 动静分离的接缝。
 *
 * 跟 balance_sign 一样：用 vite 的 ssrLoadModule 加载**线上那份源码**，
 * 不复制逻辑。
 *
 * **为什么每个用例都新起一个 vite server**：config.js 在**模块加载时**读
 * 全局配置（window.__CWQ_RUNTIME__）。同一个 server 里模块是缓存的，
 * 第二次 loadModule 拿到的还是第一次那份 —— 于是「改配置」这个动作根本
 * 不生效，测出来的永远是默认值（恒绿测试的一种）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))

/** 在给定的全局配置下加载 config.js（以及可选的 manifest.js）。 */
async function load(runtime, also = null) {
  if (runtime === undefined) delete globalThis.__CWQ_RUNTIME__
  else globalThis.__CWQ_RUNTIME__ = runtime
  const server = await createServer({
    root: ROOT, logLevel: 'silent', server: { middlewareMode: true, hmr: false }
  })
  try {
    const config = await server.ssrLoadModule('/src/config.js')
    const extra = also ? await server.ssrLoadModule(also) : null
    return { config, extra }
  } finally {
    await server.close()
  }
}

// ---------------------------------------------------------------------------
// 基址归一化
// ---------------------------------------------------------------------------

test('没有全局配置时回落同源（API 走 /api，静态资源走 /）', async () => {
  const { config } = await load(undefined)
  assert.equal(config.apiBase, '/api')
  assert.equal(config.assetBase, '/')
})

test('apiBase 去掉结尾斜杠，根路径 / 不被抹成空串', async () => {
  const { config } = await load({ apiBase: '/api/' })
  assert.equal(config.apiBase, '/api')
  const root = await load({ apiBase: '/' })
  assert.equal(root.config.apiBase, '/')
})

test('apiBase 允许绝对地址（前后端分域名）', async () => {
  const { config } = await load({ apiBase: 'https://api.example.com/api/' })
  assert.equal(config.apiBase, 'https://api.example.com/api')
})

test('assetBase 补上结尾斜杠（它是目录前缀）', async () => {
  const { config } = await load({ assetBase: 'https://cdn.example.com' })
  assert.equal(config.assetBase, 'https://cdn.example.com/')
})

test('空值回落默认值，不产生空基址', async () => {
  const { config } = await load({ apiBase: '', assetBase: '' })
  assert.equal(config.apiBase, '/api')
  assert.equal(config.assetBase, '/')
})

// ---------------------------------------------------------------------------
// 后端路径 → 浏览器该用的 URL
// ---------------------------------------------------------------------------

test('后端的 /api 前缀换成配置的 apiBase', async () => {
  const { config } = await load({ apiBase: 'https://api.example.com/api' })
  assert.equal(config.toApiUrl('/api/books'), 'https://api.example.com/api/books')
  assert.equal(config.toApiUrl('/api/plugins/assets/x/y'), 'https://api.example.com/api/plugins/assets/x/y')
})

test('同源部署下 toApiUrl 逐字不变', async () => {
  const { config } = await load({})
  assert.equal(config.toApiUrl('/api/books'), '/api/books')
  assert.equal(config.toApiUrl('/api/plugins/assets/x/y'), '/api/plugins/assets/x/y')
})

test('API 挂在别的路径前缀下时同样正确', async () => {
  const { config } = await load({ apiBase: '/backend/api' })
  assert.equal(config.toApiUrl('/api/books'), '/backend/api/books')
})

test('已经是绝对地址的原样返回（不二次拼接）', async () => {
  const { config } = await load({ apiBase: 'https://api.example.com/api' })
  assert.equal(config.toApiUrl('https://other.example.com/x'), 'https://other.example.com/x')
})

test('stripApiRoot 只认本系统的 /api 前缀', async () => {
  const { config } = await load({})
  assert.equal(config.stripApiRoot('/api/plugins/assets/x'), '/plugins/assets/x')
  assert.equal(config.stripApiRoot('https://api.example.com/api/plugins/assets/x'), '/plugins/assets/x')
  assert.equal(config.stripApiRoot('https://evil.example/index.js'), null)
  assert.equal(config.stripApiRoot('/other/plugins/assets/x'), null)
})

test('pathOf 剥掉 origin 与查询串', async () => {
  const { config } = await load({})
  assert.equal(config.pathOf('https://api.example.com/api/x?y=1'), '/api/x')
  assert.equal(config.pathOf('/api/x'), '/api/x')
})

// ---------------------------------------------------------------------------
// 插件资产：跨域形态下 URL 要跟着 apiBase 走，但**校验不放宽**
// ---------------------------------------------------------------------------

const DIGEST = 'a'.repeat(64)

function meta() {
  return {
    id: 'com.test.demo', name: 'T', version: '1.0.0', digest: DIGEST,
    menu: { module: 'extensions', title: '扩展', icon: 'Grid', route: '/plugin/com.test.demo' },
    entry: `/api/plugins/assets/com.test.demo/${DIGEST}/frontend/index.js`,
    styles: [`/api/plugins/assets/com.test.demo/${DIGEST}/frontend/style.css`],
  }
}

test('跨域形态：插件资产 URL 跟着 apiBase 走', async () => {
  const { extra } = await load({ apiBase: 'https://api.example.com/api' }, '/src/plugins/manifest.js')
  const p = extra.validatePlugin(meta())
  assert.equal(p.entry, `https://api.example.com/api/plugins/assets/com.test.demo/${DIGEST}/frontend/index.js`)
  assert.equal(p.styles[0], `https://api.example.com/api/plugins/assets/com.test.demo/${DIGEST}/frontend/style.css`)
})

test('同源形态：插件资产 URL 与从前逐字一致', async () => {
  const { extra } = await load({}, '/src/plugins/manifest.js')
  const p = extra.validatePlugin(meta())
  assert.equal(p.entry, `/api/plugins/assets/com.test.demo/${DIGEST}/frontend/index.js`)
})

test('跨域形态下，别的源、带查询串、路径穿越**仍然被拒**', async () => {
  const { extra } = await load({ apiBase: 'https://api.example.com/api' }, '/src/plugins/manifest.js')
  for (const [name, mutate] of [
    ['remote', p => { p.entry = 'https://evil.example/index.js' }],
    ['query', p => { p.entry += '?token=leak' }],
    ['escape', p => { p.entry = p.entry.replace('index.js', '../backend/main.py') }],
    ['other_api', p => { p.entry = p.entry.replace('/api/plugins', '/other/plugins') }],
    ['styles', p => { p.styles = ['https://evil.example/style.css'] }],
  ]) {
    const p = meta()
    mutate(p)
    assert.throws(() => extra.validatePlugin(p), undefined, `不安全 ${name} 在跨域形态下竟然被放过了`)
  }
})
