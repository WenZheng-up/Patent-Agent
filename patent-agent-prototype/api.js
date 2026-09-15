/* ============================================================
   Patent-Agent · API 层 —— 对接 FastAPI 后端（同源托管，见 backend/API_CONTRACT.md）
   统一信封 { code, msg, data }；检索执行走 SSE 事件流。
   ============================================================ */
function delay(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

/* 案件 id：URL ?id= 优先，缺省回落演示案件 */
function caseIdFromUrl(fallback) {
  return new URLSearchParams(location.search).get('id') || fallback || 'CN2026-0881';
}

async function request(method, path, body) {
  var opt = { method: method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  var resp = await fetch(path, opt);
  return await resp.json();
}

/* GET /api/cases — 案件列表 */
async function fetchCases() {
  return request('GET', '/api/cases');
}

/* GET /api/cases/:id — 案件全量数据 */
async function fetchCase(id) {
  return request('GET', '/api/cases/' + encodeURIComponent(id));
}

/* POST /api/cases — 新建案件（解析交底书，v1 规则 stub） */
async function createCase(payload) {
  return request('POST', '/api/cases', {
    title: payload.title,
    client: payload.client,
    disclosureText: payload.disclosureText || ''
  });
}

/* POST /api/cases/:id/query/confirm — HITL 检索式审批
   payload: { expr, groups?, dateFrom?, dateTo?, ipc?, budget? } */
async function confirmQuery(id, payload, edited) {
  return request('POST', '/api/cases/' + encodeURIComponent(id) + '/query/confirm', {
    query: payload.expr,
    groups: payload.groups || null,
    edited: !!edited,
    budget: payload.budget || null,
    dateFrom: payload.dateFrom || null,
    dateTo: payload.dateTo || null,
    ipc: payload.ipc || null
  });
}

/* GET /api/cases/:id/retrieval/stream — SSE 两阶段检索
   onEvent(uiEvent) 实时追加 Agent 运行时事件；
   resolve { code, data: { summary, hits, events } }；错误帧 reject 错误信封。 */
function runRetrieval(id, onEvent, opts) {
  opts = opts || {};
  var budget = opts.budget || 20;
  var minScore = opts.minScore || 2;
  var url = '/api/cases/' + encodeURIComponent(id) + '/retrieval/stream' +
    '?budget=' + budget + '&minScore=' + minScore;

  return new Promise(function (resolve, reject) {
    var es = new EventSource(url);
    var closed = false;
    /* 看门狗：120s 无任何帧（含网络静默挂起）→ 主动判超时 */
    var watchdog = null;
    function arm() {
      if (watchdog) clearTimeout(watchdog);
      watchdog = setTimeout(function () {
        if (closed) return;
        finish();
        reject({ code: 5003, msg: '检索超时（120s 无响应），请重试' });
      }, 120000);
    }
    function finish() {
      if (watchdog) clearTimeout(watchdog);
      if (!closed) { closed = true; es.close(); }
    }

    es.addEventListener('open', arm);
    es.addEventListener('progress', function (e) {
      arm();
      try { if (onEvent) onEvent(JSON.parse(e.data)); } catch (err) { /* 忽略坏帧 */ }
    });
    es.addEventListener('result', function (e) {
      arm();
      finish();
      resolve({ code: 0, msg: 'ok', data: JSON.parse(e.data) });
    });
    es.addEventListener('done', function () { arm(); finish(); });
    es.addEventListener('error', function (e) {
      arm();
      if (e && e.data) {
        finish();
        try { reject(JSON.parse(e.data)); } catch (err) { reject({ code: 5000, msg: '检索失败' }); }
      } else if (!closed && es.readyState === EventSource.CLOSED) {
        /* 服务端已关闭但未收到 result（异常收尾） */
        finish();
        reject({ code: 5003, msg: '检索连接中断，请重试' });
      }
      /* readyState=CONNECTING 为正常自动重连，交由看门狗兜底 */
    });
    arm();
  });
}

/* GET /api/cases/:id/retrieval — 检索终态（首屏恢复用） */
async function fetchRetrieval(id) {
  return request('GET', '/api/cases/' + encodeURIComponent(id) + '/retrieval');
}

/* GET /api/cases/:id/comparison — 对比文件分析（v1 关键词骨架 + EvidenceRef） */
async function fetchComparison(id) {
  return request('GET', '/api/cases/' + encodeURIComponent(id) + '/comparison');
}

/* POST /api/cases/:id/report/export — 导出报告（v1 可打印 HTML） */
async function generateReport(id) {
  return request('POST', '/api/cases/' + encodeURIComponent(id) + '/report/export');
}
