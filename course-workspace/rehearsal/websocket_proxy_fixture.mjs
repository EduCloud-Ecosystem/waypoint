// Synthetic loopback-only upstreams for the pinned CHP diagnostic control.
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import crypto from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [moduleRoot, directory] = process.argv.slice(2);
const metadata = JSON.parse(fs.readFileSync(path.join(moduleRoot, 'package.json')));
if (metadata.name !== 'configurable-http-proxy' || metadata.version !== '5.1.0') {
  throw new Error('proxy_version_refused');
}
const { ConfigurableProxy } = await import(pathToFileURL(path.join(moduleRoot, 'lib/configproxy.js')));
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
// Select and close an ephemeral port. If another process takes it, the parent
// fails this control rather than accepting anything except a refused connection.
const reservation = net.createServer();
await listen(reservation);
const refusedPort = reservation.address().port;
await new Promise(resolve => reservation.close(resolve));

const upstream = http.createServer((req, res) => { res.writeHead(404); res.end(); });
upstream.on('upgrade', (req, socket) => {
  if (req.url.includes('/synthetic-upstream/')) {
    const body = 'synthetic-body-canary';
    socket.end(`HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
    return;
  }
  const accept = crypto.createHash('sha1').update(req.headers['sec-websocket-key'] +
    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  const message = Buffer.from('PROXY_OK');
  socket.write(Buffer.concat([Buffer.from([0x81, message.length]), message]));
  socket.on('data', () => socket.end());
});
await listen(upstream);
const proxy = new ConfigurableProxy({ssl: {
  key: fs.readFileSync(path.join(directory, 'key.pem')),
  cert: fs.readFileSync(path.join(directory, 'cert.pem')),
}});
for (const name of ['good', 'upstream', 'refused']) {
  await proxy.addRoute(`/user/alice/api/kernels/synthetic-${name}/`, {
    target: `http://127.0.0.1:${name === 'refused' ? refusedPort : upstream.address().port}`,
  });
}
await listen(proxy.proxyServer);
fs.writeFileSync(path.join(directory, 'ready.json'), JSON.stringify({
  port: proxy.proxyServer.address().port, node: process.version, proxy: metadata.version,
}), {mode: 0o600});
// The parent also enforces a deadline and reaps this exact child.
setTimeout(() => process.exit(2), 120000);
