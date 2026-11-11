import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';

// 静态托管 frontend/dist，并把 /api 与 /media 反代到后端。用法：
//   node scripts/serve-dist.mjs <dist 目录> <端口> [后端地址]
const DIST = path.resolve(process.argv[2] || 'frontend/dist');
const PORT = Number(process.argv[3] || 4173);
const BACKEND = process.argv[4] || 'http://127.0.0.1:8000';
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
};

// 源文件 index.html 里的 <script type="module" src="/src/main.jsx"> 是给 vite dev 的。
// vite build 正常会把它换成打包后的入口，但沙箱下可能残留；残留就等于让浏览器再拉一次
// /src/main.jsx（拿到的是 index.html），首屏白屏。托管时统一过滤掉，省得依赖构建细节。
const DEV_ENTRY = /\s*<script type="module" src="\/src\/main\.jsx"><\/script>/;

function stripDevEntry(body) {
  const text = body.toString('utf8');
  return DEV_ENTRY.test(text) ? Buffer.from(text.replace(DEV_ENTRY, ''), 'utf8') : body;
}

function sendFile(req, res, file) {
  let body;
  try {
    body = fs.readFileSync(file);
    if (path.extname(file).toLowerCase() === '.html') body = stripDevEntry(body);
  } catch (error) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('not found: ' + file);
  }
  res.writeHead(200, {
    'content-type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream',
    'content-length': body.length,
    // 文件名带内容哈希，可以让浏览器长期缓存；index.html 必须每次回源。
    'cache-control': file.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable',
  });
  res.end(req.method === 'HEAD' ? undefined : body);
}

function proxy(req, res, target) {
  const url = new URL(req.url, 'http://127.0.0.1');
  const base = new URL(target);
  const client = base.protocol === 'https:' ? https : http;
  const upstream = client.request({
    protocol: base.protocol,
    hostname: base.hostname,
    port: base.port || (base.protocol === 'https:' ? 443 : 80),
    method: req.method,
    path: url.pathname + url.search,
    headers: Object.assign({}, req.headers, { host: base.host }),
  }, (upstreamRes) => {
    res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
    upstreamRes.pipe(res);
  });
  upstream.on('error', (error) => {
    if (res.headersSent) return res.destroy();
    res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('backend unreachable: ' + error.message);
  });
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
  if (pathname === '/api' || pathname.startsWith('/api/') || pathname.startsWith('/media/')) {
    return proxy(req, res, BACKEND);
  }
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('method not allowed');
  }
  // SPA：先找真实文件，找不到就把 index.html 交回给前端路由。
  const candidate = path.resolve(DIST, pathname.replace(/^[/\\]+/, ''));
  if (candidate.startsWith(DIST) && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
    return sendFile(req, res, candidate);
  }
  return sendFile(req, res, path.join(DIST, 'index.html'));
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('[serve-dist] ' + DIST + ' -> http://127.0.0.1:' + PORT + '（/api /media 反代 ' + BACKEND + '）');
});