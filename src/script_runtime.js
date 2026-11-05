/* script_runtime.js — 前置/后置脚本运行时（由 scripts.py 拉起的一次性 Node 子进程）
 *
 * 入参：stdin 一整个 JSON
 *   { phase: 'pre'|'post', code, vars, request: {method,url,headers,body,params,timeout},
 *     response: {code,status,headers,body,elapsed_ms,truncated} }
 *
 * 出参：stdout 末尾一行 __PM_DONE__ + JSON
 *   { ok, error, logs: [{level,text}], tests: [{name,passed,message}], vars, request }
 *
 * 用户代码跑在 vm 隔离上下文里，只有 pm / console / setTimeout 三个全局，
 * 拿不到 require、process、fs；同步死循环由 vm timeout 掐断，
 * 异步不返回的由 Python 侧的子进程超时兜底。
 */

'use strict';

const vm = require('vm');
const util = require('util');

const DONE = '__PM_DONE__';
const SYNC_TIMEOUT_MS = 4000;   // 同步代码（含死循环）的运行上限

/* ───────────── 小工具 ───────────── */

let _userLines = 0;   // 用户脚本的行数，用来把行号夹在代码范围内

function errMsg(e) {
  if (!e) return '未知错误';
  let m = String(e.message || e);
  // 用户代码被包进一行 async 箭头函数里，堆栈行号要减 1 才是编辑器里看到的行
  // 语法错误的堆栈只有行号没有列号，所以列号部分是可选的
  const hit = String(e.stack || '').match(/user-script\.js:(\d+)/);
  if (hit) {
    // 语法错误报的是包壳收尾的那行，夹到用户代码最后一行为止
    let line = parseInt(hit[1], 10) - 1;
    if (_userLines) line = Math.min(line, _userLines);
    m += '（第 ' + Math.max(1, line) + ' 行）';
  }
  return m;
}

function fmt(v, max) {
  const cap = max || 300;
  if (typeof v === 'string') return v.length > cap ? JSON.stringify(v.slice(0, cap) + '…') : JSON.stringify(v);
  if (v === undefined) return 'undefined';
  let s;
  try { s = util.inspect(v, { depth: 4, breakLength: 120, maxArrayLength: 50 }); } catch (e) { s = String(v); }
  s = s.replace(/\s+/g, ' ');
  return s.length > cap ? s.slice(0, cap) + '…' : s;
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a && b && typeof a === 'object') {
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every(function (k) { return deepEqual(a[k], b[k]); });
  }
  return false;
}

function isEmpty(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string' || Array.isArray(v)) return v.length === 0;
  if (typeof v === 'object') return Object.keys(v).length === 0;
  return false;
}

function typeOk(v, t) {
  const s = String(t).toLowerCase();
  if (s === 'array') return Array.isArray(v);
  if (s === 'object') return v !== null && typeof v === 'object' && !Array.isArray(v);
  if (s === 'null') return v === null;
  return typeof v === s;
}

/* JSON 安全化：脚本里塞了函数 / undefined 时不要炸掉整个回传 */
function safe(v) {
  if (v === undefined || typeof v === 'function' || typeof v === 'symbol') return null;
  try {
    const s = JSON.stringify(v);
    return s === undefined ? null : JSON.parse(s);
  } catch (e) {
    return String(v);
  }
}

/* ───────────── 断言（chai 的一个够用子集） ───────────── */

function makeExpect(actual, negate) {
  const api = {};
  function check(cond, msg) {
    if (!!cond === !!negate) throw new Error((negate ? '.not 取反后不成立：' : '') + msg);
    return api;
  }
  ['to', 'be', 'been', 'is', 'that', 'which', 'and', 'has', 'have', 'with', 'at', 'of',
    'same', 'but', 'does', 'still', 'also', 'deep'].forEach(function (w) {
    Object.defineProperty(api, w, { get: function () { return api; } });
  });
  Object.defineProperty(api, 'not', { get: function () { return makeExpect(actual, !negate); } });

  api.equal = api.equals = api.eq = function (v) {
    return check(actual === v, '期望等于 ' + fmt(v) + '，实际 ' + fmt(actual));
  };
  api.eql = function (v) {
    return check(deepEqual(actual, v), '期望深等于 ' + fmt(v) + '，实际 ' + fmt(actual));
  };
  api.include = api.includes = api.contain = function (v) {
    let ok = false;
    if (typeof actual === 'string') ok = actual.indexOf(v) >= 0;
    else if (Array.isArray(actual)) ok = actual.some(function (x) { return deepEqual(x, v); });
    else if (actual && typeof actual === 'object') ok = Object.prototype.hasOwnProperty.call(actual, v);
    return check(ok, '期望包含 ' + fmt(v) + '，实际 ' + fmt(actual));
  };
  api.property = function (k, v) {
    const has = actual !== null && actual !== undefined && (k in Object(actual));
    if (v === undefined) return check(has, '期望有属性 ' + k + '，实际 ' + fmt(actual));
    const val = has ? actual[k] : undefined;
    return check(has && deepEqual(val, v), '期望 ' + k + '=' + fmt(v) + '，实际 ' + fmt(val));
  };
  api.lengthOf = function (n) {
    const len = actual === null || actual === undefined ? undefined : actual.length;
    return check(len === n, '期望长度 ' + n + '，实际 ' + len);
  };
  api.above = api.gt = function (n) {
    return check(Number(actual) > Number(n), '期望大于 ' + n + '，实际 ' + fmt(actual));
  };
  api.below = api.lt = function (n) {
    return check(Number(actual) < Number(n), '期望小于 ' + n + '，实际 ' + fmt(actual));
  };
  api.a = api.an = function (t) {
    return check(typeOk(actual, t), '期望类型 ' + t + '，实际 ' + fmt(actual));
  };
  api.match = function (re) {
    return check(new RegExp(re).test(String(actual)), '期望匹配 ' + re + '，实际 ' + fmt(actual));
  };
  api.oneOf = function (list) {
    return check((list || []).some(function (x) { return deepEqual(x, actual); }), '期望属于 ' + fmt(list) + '，实际 ' + fmt(actual));
  };
  [['ok', !!actual], ['true', actual === true], ['false', actual === false],
    ['null', actual === null], ['undefined', actual === undefined],
    ['empty', isEmpty(actual)]].forEach(function (p) {
    Object.defineProperty(api, p[0], {
      get: function () { check(p[1], '期望 ' + p[0] + '，实际 ' + fmt(actual)); return api; }
    });
  });
  return api;
}

/* ───────────── 请求/响应头（Postman HeaderList 的迷你版） ───────────── */

function HeaderList(obj) {
  const self = this;
  this._m = {};
  const src = obj && typeof obj === 'object' ? obj : {};
  Object.keys(src).forEach(function (k) {
    const val = src[k] == null ? '' : String(src[k]);
    self._m[String(k).toLowerCase()] = [String(k), val];
    self[k] = val;   // 兼容 headers['Content-Type'] 这种直取
  });
}
HeaderList.prototype.get = function (k) {
  const h = this._m[String(k).toLowerCase()];
  return h ? h[1] : undefined;
};
HeaderList.prototype.has = function (k) { return !!this._m[String(k).toLowerCase()]; };
HeaderList.prototype.add = function (o) { return this._put(o); };
HeaderList.prototype.upsert = function (o) { return this._put(o); };
HeaderList.prototype._put = function (o) {
  if (o && typeof o === 'object') {
    const k = String(o.key == null ? '' : o.key);
    if (k) this._m[k.toLowerCase()] = [k, o.value == null ? '' : String(o.value)];
  }
  return this;
};
HeaderList.prototype.remove = function (k) { delete this._m[String(k).toLowerCase()]; return this; };
HeaderList.prototype.all = function () {
  const m = this._m;
  return Object.keys(m).map(function (k) { return { key: m[k][0], value: m[k][1] }; });
};
HeaderList.prototype.toObject = function () {
  const m = this._m, out = {};
  Object.keys(m).forEach(function (k) { out[m[k][0]] = m[k][1]; });
  return out;
};
HeaderList.prototype.toJSON = function () { return this.toObject(); };

/* ───────────── 主流程 ───────────── */

async function run(payload) {
  const phase = payload.phase === 'post' ? 'post' : 'pre';
  const logs = [];
  const tests = [];
  const vars = Object.assign({}, payload.vars || {});
  _userLines = String(payload.code || '').split('\n').length;

  function logWith(level) {
    return function () {
      const parts = Array.prototype.slice.call(arguments).map(function (a) {
        return typeof a === 'string' ? a : fmt(a, 600);
      });
      logs.push({ level: level, text: parts.join(' ') });
    };
  }
  const sandboxConsole = {
    log: logWith('log'), info: logWith('info'), debug: logWith('debug'),
    warn: logWith('warn'), error: logWith('error')
  };

  // 变量作用域：全局 / 环境 / 集合变量在本地都指向同一份变量表
  const scope = {
    get: function (k) {
      const v = vars[String(k)];
      return v === undefined ? undefined : v;
    },
    set: function (k, v) {
      if (v === undefined) { delete vars[String(k)]; return; }
      vars[String(k)] = (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v);
      return v;
    },
    unset: function (k) { delete vars[String(k)]; },
    has: function (k) { return Object.prototype.hasOwnProperty.call(vars, String(k)); },
    toObject: function () { return Object.assign({}, vars); },
    replaceIn: function (s) {
      return String(s == null ? '' : s).replace(/\$\{([^{}]+)\}/g, function (m, n) {
        return vars[n] === undefined ? m : vars[n];
      });
    }
  };

  let req = null;
  if (payload.request) {
    const rq = payload.request;
    req = {
      method: String(rq.method || 'GET').toUpperCase(),
      url: String(rq.url || ''),
      headers: new HeaderList(rq.headers),
      body: rq.body === undefined ? '' : rq.body,
      params: Object.assign({}, rq.params || {}),
      timeout: rq.timeout
    };
    req.getHeader = function (k) {
      const h = req.headers;
      return h instanceof HeaderList ? h.get(k) : (h ? h[k] : undefined);
    };
    req.setHeader = function (k, v) {
      if (!(req.headers instanceof HeaderList)) req.headers = new HeaderList(req.headers);
      req.headers.upsert({ key: k, value: v });
      return req;
    };
    req.json = function () {
      try { return typeof req.body === 'string' ? JSON.parse(req.body) : req.body; }
      catch (e) { return undefined; }
    };
    req.setJson = function (obj) {
      req.body = obj;
      req.setHeader('Content-Type', 'application/json');
      return req;
    };
  }

  let resp = null;
  if (payload.response) {
    const rp = payload.response;
    let parsed, hasParsed = false;
    resp = {
      code: rp.code,
      status: String(rp.status || rp.code || ''),
      headers: new HeaderList(rp.headers),
      responseTime: rp.elapsed_ms || 0,
      body: String(rp.body == null ? '' : rp.body),
      truncated: !!rp.truncated,
      text: function () { return String(rp.body == null ? '' : rp.body); },
      json: function () {
        if (!hasParsed) {
          hasParsed = true;
          try { parsed = JSON.parse(String(rp.body == null ? '' : rp.body)); }
          catch (e) { throw new Error('响应不是合法 JSON：' + e.message); }
        }
        return parsed;
      },
      size: function () { return Buffer.byteLength(String(rp.body == null ? '' : rp.body), 'utf8'); }
    };
  }

  const pending = [];
  const pm = {
    info: {
      eventName: phase === 'pre' ? 'prerequest' : 'test',
      phase: phase,
      requestName: String(payload.name || ''),
      iteration: 0
    },
    variables: scope,
    environment: scope,
    globals: scope,
    collectionVariables: scope,
    request: req,
    response: resp,
    test: function (name, fn) {
      const label = String(name == null ? '' : name);
      try {
        const r = fn && fn();
        if (r && typeof r.then === 'function') {
          pending.push(r.then(
            function () { tests.push({ name: label, passed: true, message: '' }); },
            function (e) { tests.push({ name: label, passed: false, message: errMsg(e) }); }
          ));
          return;
        }
        tests.push({ name: label, passed: true, message: '' });
      } catch (e) {
        tests.push({ name: label, passed: false, message: errMsg(e) });
      }
    },
    expect: function (v) { return makeExpect(v, false); },
    sendRequest: function () { throw new Error('暂不支持 pm.sendRequest（本工具只跑单请求）'); }
  };

  const sandbox = { pm: pm, console: sandboxConsole, setTimeout: setTimeout, clearTimeout: clearTimeout };
  vm.createContext(sandbox);

  let error = '';
  try {
    const script = new vm.Script('(async () => {\n' + String(payload.code || '') + '\n})()', { filename: 'user-script.js' });
    const ret = script.runInContext(sandbox, { timeout: SYNC_TIMEOUT_MS, displayErrors: true });
    pending.push(Promise.resolve(ret));
    await Promise.all(pending);
  } catch (e) {
    error = errMsg(e);
  }

  const out = {
    ok: !error,
    error: error,
    logs: logs,
    tests: tests,
    vars: safe(vars),
    phase: phase,
    request: null
  };
  if (req) {
    out.request = {
      method: req.method,
      url: req.url,
      headers: req.headers instanceof HeaderList ? req.headers.toObject() : safe(req.headers),
      params: safe(req.params),
      body: safe(req.body),
      timeout: safe(req.timeout)
    };
  }
  return out;
}

/* ───────────── 入口：读 stdin → 跑 → 吐结果 ───────────── */

const chunks = [];
process.stdin.on('data', function (c) { chunks.push(c); });
process.stdin.on('end', function () {
  let payload = {};
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
  } catch (e) {
    process.stdout.write('\n' + DONE + JSON.stringify({ ok: false, error: '脚本参数解析失败：' + e.message }) + '\n');
    return;
  }
  run(payload).then(function (res) {
    process.stdout.write('\n' + DONE + JSON.stringify(res) + '\n');
  }).catch(function (e) {
    process.stdout.write('\n' + DONE + JSON.stringify({
      ok: false, error: errMsg(e), logs: [], tests: [], vars: payload.vars || {}
    }) + '\n');
  });
});
