import express from 'express';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const app = express();
const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 3000);
// The terminal serves one local backend. No arbitrary RPC proxy, model fallback, or broadcast route.
const backend = 'http://127.0.0.1:8765';
app.disable('x-powered-by');
app.use((req, res, next) => {
  const host = req.headers.host;
  if (!host || ![`127.0.0.1:${port}`, `localhost:${port}`].includes(host)) {
    res.status(403).json({error: 'Local terminal only'}); return;
  }
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Frame-Options', 'DENY');
  if (req.path.startsWith('/api/')) res.setHeader('Cache-Control', 'no-store');
  next();
});
app.use(express.json({limit: '2kb'}));
async function proxy(req: express.Request, res: express.Response) {
  try {
    if (req.method === 'POST' &&
       (req.headers['x-cointrade-request'] !== 'terminal' ||
        !req.is('application/json') ||
        (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`))) {
      res.status(403).json({error: 'Same-origin terminal request required'}); return;
    }
    const upstream = await fetch(backend + req.path, {
      method: req.method,
      signal: AbortSignal.timeout(20000),
      headers: req.method === 'POST' ? {'Content-Type':'application/json','X-Cointrade-Request':'terminal'} : {},
      body: req.method === 'POST' ? JSON.stringify(req.body) : undefined,
    });
    const data = await upstream.json();
    res.status(upstream.status).json(data);
  } catch {
    res.status(503).json({error:'Scanner backend unavailable. Last displayed data is stale.'});
  }
}
app.get('/api/state', proxy);
app.get('/api/health', proxy);
app.post('/api/astra', proxy);
app.use('/api', (_req,res) => { res.status(404).json({error:'Unsupported operation'}); });
if (process.env.NODE_ENV === 'production') {
  app.use(express.static(path.join(root,'dist')));
  app.get('*', (_req,res) => res.sendFile(path.join(root,'dist/index.html')));
} else {
  const {createServer} = await import('vite');
  const vite = await createServer({root, server:{middlewareMode:true}, appType:'spa'});
  app.use(vite.middlewares);
}
app.listen(port,'127.0.0.1', () => console.log(`Astra paper terminal: http://127.0.0.1:${port}`));
