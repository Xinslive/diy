// Isolated E2E gateway: built application, production CSP, same-origin API proxy.
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
const [port, api, dist] = process.argv.slice(2)
const root = path.resolve(dist)
const policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'; frame-src 'self' about:"
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.png': 'image/png', '.ico': 'image/x-icon' }
http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    const upstream = http.request(new URL(req.url, api), { method: req.method, headers: req.headers }, result => {
      res.writeHead(result.statusCode, result.headers); result.pipe(res)
    })
    upstream.on('error', () => { res.writeHead(502); res.end('API unavailable') })
    req.pipe(upstream)
    return
  }
  const name = path.resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://test').pathname))
  if (!name.startsWith(root + path.sep) && name !== root) { res.writeHead(404); res.end(); return }
  const file = name === root ? path.join(root, 'index.html') : name
  try {
    const content = fs.readFileSync(file)
    res.writeHead(200, { 'content-type': mime[path.extname(file)] || 'application/octet-stream', 'content-security-policy': policy, 'x-content-type-options': 'nosniff' }); res.end(content)
  } catch { res.writeHead(404); res.end() }
}).listen(Number(port), '127.0.0.1', () => console.log('plugin E2E gateway ready'))
