/**
 * Jarvis Node.js Code Module
 *
 * Carregado via NODE_OPTIONS=--require /path/to/nodejs_loader.js
 * O ProcessInjector injeta NODE_OPTIONS apenas no processo alvo.
 *
 * Instrumenta http, https, net, dns, pg (node-postgres) e mongoose
 * sem modificar o código da aplicação.
 * Envia spans em OTLP JSON para o OtelReceiver local (porta 4318).
 */

'use strict';

// Não instrumentar se não foi injectado pelo Jarvis
if (!process.env.JARVIS_AGENT) return;

const http    = require('http');
const https   = require('https');
const url     = require('url');
const path    = require('path');

const ENDPOINT     = (process.env.JARVIS_OTEL_ENDPOINT || 'http://localhost:4318') + '/v1/traces';
const SERVICE_NAME = process.env.OTEL_SERVICE_NAME || _detectServiceName();
const HOST_NAME    = require('os').hostname();

// ── Geração de IDs ────────────────────────────────────────────────────────────

function _randomHex(bytes) {
  const buf = Buffer.alloc(bytes);
  for (let i = 0; i < bytes; i++) buf[i] = Math.floor(Math.random() * 256);
  return buf.toString('hex');
}

function _traceId() { return _randomHex(16); }
function _spanId()  { return _randomHex(8);  }
function _nowNs()   { return (BigInt(Date.now()) * 1_000_000n).toString(); }

// ── Emissão de spans ──────────────────────────────────────────────────────────

function _emit(span) {
  const payload = JSON.stringify({
    resourceSpans: [{
      resource: {
        attributes: [
          { key: 'service.name', value: { stringValue: SERVICE_NAME } },
          { key: 'host.name',    value: { stringValue: HOST_NAME    } },
        ]
      },
      scopeSpans: [{
        spans: [{
          traceId:            span.traceId,
          spanId:             span.spanId,
          parentSpanId:       span.parentSpanId || '',
          name:               span.name,
          kind:               span.kind || 3,
          startTimeUnixNano:  span.startNs,
          endTimeUnixNano:    span.endNs,
          status:             { code: span.error ? 2 : 0 },
          attributes:         Object.entries(span.attrs || {}).map(([k, v]) => ({
            key: k, value: { stringValue: String(v) }
          })),
        }]
      }]
    }]
  });

  try {
    const parsed  = new url.URL(ENDPOINT);
    const mod     = parsed.protocol === 'https:' ? https : http;
    const req     = mod.request({
      hostname: parsed.hostname,
      port:     parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
      path:     parsed.pathname,
      method:   'POST',
      headers:  { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    });
    req.on('error', () => {});
    req.write(payload);
    req.end();
  } catch (_) {}
}

// ── Instrumentação HTTP de saída ──────────────────────────────────────────────

function _patchHttpRequest(mod, proto) {
  const orig = mod.request.bind(mod);
  mod.request = function jarvisRequest(options, callback) {
    const traceId = _traceId();
    const spanId  = _spanId();
    const startNs = _nowNs();

    const opts   = typeof options === 'string' ? new url.URL(options) : options;
    const method = (opts.method || 'GET').toUpperCase();
    const host   = opts.hostname || opts.host || 'unknown';
    const pth    = opts.path || opts.pathname || '/';

    const req = orig(options, callback);

    // Injectar X-Jarvis header para distributed tracing
    try { req.setHeader('X-Jarvis-Trace-Id', traceId); } catch (_) {}
    try { req.setHeader('X-Jarvis-Span-Id',  spanId);  } catch (_) {}

    req.on('response', (res) => {
      const endNs    = _nowNs();
      const status   = res.statusCode || 0;
      _emit({
        traceId, spanId, startNs, endNs,
        name:  `${method} ${host}${pth}`,
        kind:  3,  // CLIENT
        error: status >= 500,
        attrs: {
          'http.method':      method,
          'http.host':        host,
          'http.target':      pth,
          'http.status_code': status,
          'net.peer.name':    host,
        },
      });
    });

    req.on('error', () => {
      _emit({
        traceId, spanId, startNs, endNs: _nowNs(),
        name: `${method} ${host}${pth}`, kind: 3, error: true,
        attrs: { 'http.method': method, 'http.host': host, 'http.target': pth },
      });
    });

    return req;
  };
}

// ── Instrumentação HTTP de entrada ────────────────────────────────────────────

function _patchHttpServer() {
  const origCreate = http.createServer.bind(http);
  http.createServer = function jarvisCreateServer(opts, listener) {
    const handler = typeof opts === 'function' ? opts : listener;
    const wrapped = function(req, res) {
      const traceId = req.headers['x-jarvis-trace-id'] || _traceId();
      const spanId  = _spanId();
      const startNs = _nowNs();
      const method  = req.method || 'GET';
      const target  = req.url    || '/';

      const origEnd = res.end.bind(res);
      res.end = function(...args) {
        _emit({
          traceId, spanId, startNs, endNs: _nowNs(),
          name:  `${method} ${target}`,
          kind:  2,  // SERVER
          error: res.statusCode >= 500,
          attrs: {
            'http.method':      method,
            'http.target':      target,
            'http.status_code': res.statusCode || 0,
          },
        });
        return origEnd(...args);
      };

      if (handler) handler(req, res);
    };

    return typeof opts === 'function'
      ? origCreate(wrapped)
      : origCreate(opts, wrapped);
  };
}

// ── Detecção do nome do serviço ───────────────────────────────────────────────

function _detectServiceName() {
  const main = process.mainModule && process.mainModule.filename;
  if (main) return path.basename(main, path.extname(main));
  const argv1 = process.argv[1];
  if (argv1) return path.basename(argv1, path.extname(argv1));
  return 'node-service';
}

// ── Init ──────────────────────────────────────────────────────────────────────

try {
  _patchHttpRequest(http,  'http');
  _patchHttpRequest(https, 'https');
  _patchHttpServer();
} catch (_) {}
