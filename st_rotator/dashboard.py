"""控制台前端页面（单文件 HTML，零 CDN 依赖）。

为什么要塞在 Python 字符串里
---------------------------
这个工具是零依赖、可离线部署的。如果前端拆成独立的 .html/.js/.css 文件，就得处理
"打包后文件在哪"的问题；而引 CDN 又会让内网环境直接白屏。所以整页内联，由网关自己
吐出来——打开一个 URL 就有完整界面。

为什么是 Web 而不是 tkinter
--------------------------
托管 Python 不带 tkinter（`ModuleNotFoundError: No module named 'tkinter'`），
而系统自带的 3.9 又太老。用本地 Web 页面 + Edge 的 ``--app`` 模式开窗，没有地址栏和
标签页，观感和原生桌面程序一致，还能顺带把实时表格、日志、代码复制都做得很舒服。
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多 Key 轮换控制台</title>
<style>
  :root {
    --bg: #f4f6fa;
    --card: #ffffff;
    --border: #e2e7f0;
    --border-strong: #cfd7e6;
    --text: #16202f;
    --muted: #6a7688;
    --accent: #2f6feb;
    --accent-soft: #eaf1ff;
    --green: #0f7a55;
    --green-bg: #e4f6ee;
    --amber: #8a5b00;
    --amber-bg: #fff3d4;
    --red: #b3261e;
    --red-bg: #fdeceb;
    --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12161d;
      --card: #1a1f28;
      --border: #2a3240;
      --border-strong: #3a4456;
      --text: #e6ebf2;
      --muted: #93a0b4;
      --accent: #5b93ff;
      --accent-soft: #1e2b45;
      --green: #4fd1a5;
      --green-bg: #14302a;
      --amber: #e8b84b;
      --amber-bg: #33290f;
      --red: #ff8078;
      --red-bg: #3a1d1c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 13px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1360px; margin: 0 auto; padding: 18px 20px 40px; }

  header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 18px; margin-bottom: 16px;
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot.on { background: #16a34a; box-shadow: 0 0 0 3px rgba(22,163,74,.16); }
  .dot.paused { background: #d97706; box-shadow: 0 0 0 3px rgba(217,119,6,.16); }
  .spacer { flex: 1; }

  button {
    font-family: inherit; font-size: 12.5px; cursor: pointer;
    border: 1px solid var(--border-strong); background: var(--card); color: var(--text);
    padding: 6px 13px; border-radius: 7px; transition: .14s;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); color: #fff; }
  button.danger:hover:not(:disabled) { border-color: var(--red); color: var(--red); }
  button.tiny { padding: 3px 9px; font-size: 11.5px; border-radius: 6px; }
  button.primary.dirty { box-shadow: 0 0 0 2px var(--amber-bg), 0 0 0 3px var(--amber); }
  .opt-dirty { color: var(--amber); font-size: 11.5px; margin-left: 6px; }

  .grid { display: grid; gap: 14px; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); margin-bottom: 14px; }
  .two { grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); }
  @media (max-width: 980px) { .two { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 15px 17px; min-width: 0;
  }
  .card h2 {
    font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: .7px;
    color: var(--muted); margin: 0 0 12px; display: flex; align-items: center; gap: 8px;
  }
  .card h2 .spacer { flex: 1; }

  .kpi .label { font-size: 11.5px; color: var(--muted); letter-spacing: .3px; }
  .kpi .value { font-size: 22px; font-weight: 640; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .kpi .value small { font-size: 12px; color: var(--muted); font-weight: 400; margin-left: 3px; }
  .kpi .value.green { color: var(--green); }
  .kpi .value.amber { color: var(--amber); }
  .kpi .value.red { color: var(--red); }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; font-weight: 600; color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: .5px;
    padding: 0 8px 7px; border-bottom: 1px solid var(--border); white-space: nowrap;
  }
  td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  td.mono { font-family: var(--mono); font-size: 11.5px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  th.num { text-align: right; }

  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .pill.healthy { background: var(--green-bg); color: var(--green); }
  .pill.cooldown { background: var(--amber-bg); color: var(--amber); }
  .pill.invalid { background: var(--red-bg); color: var(--red); }
  .pill.neutral { background: var(--accent-soft); color: var(--accent); }

  input, select {
    font-family: inherit; font-size: 12.5px; color: var(--text);
    background: var(--card); border: 1px solid var(--border-strong);
    border-radius: 7px; padding: 6px 10px; width: 100%;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  input.mono { font-family: var(--mono); font-size: 12px; }
  label.field { display: block; margin-bottom: 9px; }
  label.field > span { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 4px; }
  dialog {
    background: var(--card); color: var(--text); border: 1px solid var(--border-strong);
    border-radius: 12px; padding: 18px; max-width: 560px; width: 90%;
  }
  dialog::backdrop { background: rgba(0, 0, 0, .45); }
  dialog textarea {
    width: 100%; box-sizing: border-box; font-family: var(--mono); font-size: 12px;
    color: var(--text); background: var(--bg); border: 1px solid var(--border-strong);
    border-radius: 7px; padding: 8px 10px; resize: vertical; margin-bottom: 10px;
  }
  dialog textarea:focus { outline: none; border-color: var(--accent); }
  .row { display: flex; gap: 9px; align-items: flex-end; }
  .row > * { min-width: 0; }
  .row .grow { flex: 1; }

  .kv { display: flex; gap: 8px; align-items: center; margin-bottom: 7px; font-size: 12.5px; }
  .kv > .k { color: var(--muted); min-width: 74px; flex-shrink: 0; }
  .kv > .v { font-family: var(--mono); font-size: 11.5px; word-break: break-all; }
  .kv > .v.grow { flex: 1; }

  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px; margin: 0; overflow-x: auto;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.6;
  }
  .tabs { display: flex; gap: 4px; margin-bottom: 9px; flex-wrap: wrap; }
  .tabs button { padding: 4px 11px; font-size: 11.5px; }
  .tabs button.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }

  #logbox, #replenish-logbox {
    height: 208px; overflow-y: auto; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.65;
  }
  #replenish-logbox { height: 168px; }
  #logbox div, #replenish-logbox div { white-space: pre-wrap; word-break: break-all; }
  #logbox .warn, #replenish-logbox .warn { color: var(--amber); }
  #logbox .err, #replenish-logbox .err { color: var(--red); }
  #logbox .ok, #replenish-logbox .ok { color: var(--green); }
  #logbox .dim, #replenish-logbox .dim { color: var(--muted); }

  .toast {
    position: fixed; right: 22px; bottom: 22px; z-index: 50;
    display: flex; flex-direction: column; gap: 8px; align-items: flex-end;
  }
  .toast div {
    background: var(--card); border: 1px solid var(--border-strong); border-left: 3px solid var(--accent);
    border-radius: 8px; padding: 9px 14px; font-size: 12.5px; max-width: 380px;
    box-shadow: 0 6px 22px rgba(20,30,50,.14); animation: pop .18s ease-out;
  }
  .toast div.err { border-left-color: var(--red); }
  .toast div.ok { border-left-color: var(--green); }
  @keyframes pop { from { opacity: 0; transform: translateY(6px); } }
  .chart-tip {
    position: fixed; display: none; z-index: 60; pointer-events: none;
    background: var(--card); border: 1px solid var(--border-strong); border-radius: 6px;
    padding: 5px 9px; font-size: 11.5px; color: var(--text); white-space: nowrap;
    box-shadow: 0 6px 22px rgba(20,30,50,.14);
  }

  .banner {
    display: none; align-items: center; gap: 10px; margin-bottom: 14px;
    background: var(--amber-bg); border: 1px solid var(--amber); color: var(--amber);
    border-radius: 10px; padding: 10px 15px; font-size: 12.5px;
  }
  .banner.show { display: flex; }
  .banner input { max-width: 260px; }
  .muted { color: var(--muted); }
  .empty { padding: 22px; text-align: center; color: var(--muted); }
  .spin { display: inline-block; animation: rot .9s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  .badge {
    display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10.5px;
    background: var(--accent-soft); color: var(--accent); margin-right: 3px;
  }
  svg.spark { display: block; width: 100%; height: 34px; margin-top: 6px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>多 Key 轮换控制台</h1>
      <div class="sub" id="subline">正在连接…</div>
    </div>
    <div class="spacer"></div>
    <button id="btn-pause">暂停接入</button>
    <button id="btn-refresh">刷新</button>
  </header>

  <div class="banner" id="authbar">
    <span>该网关启用了本地鉴权，请输入访问 Token：</span>
    <input id="token-input" class="mono" type="password" placeholder="Bearer Token">
    <button class="primary tiny" id="btn-token">确认</button>
  </div>

  <div style="display:flex;justify-content:flex-end;margin-bottom:6px"><button class="tiny" id="btn-refresh-quota">刷新余量</button></div>
  <div class="grid kpis" id="kpis"></div>
  <div id="replenish">
    <div class="card" style="margin-bottom:14px">
      <h2>自动补号 <span class="spacer"></span><span id="replenish-badge"></span><span class="muted" id="replenish-note" style="text-transform:none;letter-spacing:0;margin-left:8px"></span></h2>
      <div class="grid kpis" id="replenish-stats" style="margin-bottom:14px"></div>
      <div class="row" style="margin-bottom:12px;align-items:flex-end;flex-wrap:wrap;padding-top:14px;border-top:1px solid var(--border)">
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px;white-space:nowrap">
          <input id="opt-rp-enabled" type="checkbox" style="width:auto;margin:0">
          <span style="font-weight:600">启用自动补号</span>
        </label>
        <label class="field" style="width:96px;margin-bottom:0">
          <span>目标账号数</span>
          <input id="opt-rp-target" type="number" min="0">
        </label>
        <label class="field" style="width:110px;margin-bottom:0">
          <span>检查间隔(秒)</span>
          <input id="opt-rp-interval" type="number" min="60" step="60">
        </label>
        <label class="field" style="width:120px;margin-bottom:0">
          <span>短信关键词</span>
          <input id="opt-rp-keyword" type="text">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>每日额度上限</span>
          <input id="opt-rp-cap" type="number" min="0" step="0.5">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>短信轮询间隔</span>
          <input id="opt-rp-sms-interval" type="number" min="1">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>短信超时(秒)</span>
          <input id="opt-rp-sms-timeout" type="number" min="5" step="5">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>新 Key 名称</span>
          <input id="opt-rp-keyname" type="text">
        </label>
        <label class="field" style="width:232px;margin-bottom:0">
          <span>新 Key 类型</span>
          <select id="opt-rp-keytype">
            <option value="API_KEY_TYPE_TOKEN_PLAN">API_KEY_TYPE_TOKEN_PLAN</option>
            <option value="API_KEY_TYPE_METERED">API_KEY_TYPE_METERED</option>
          </select>
        </label>
        <label class="field" style="width:150px;margin-bottom:0">
          <span>易码 Token（留空不修改）</span>
          <input id="opt-rp-sms-token" type="password" autocomplete="off" placeholder="已配置则留空">
        </label>
        <button class="primary" id="btn-save-replenish" style="height:33px">保存并应用</button>
        <span class="opt-dirty" id="opt-dirty-hint" style="display:none">● 有未保存的改动</span>
      </div>
      <div class="muted" style="font-size:11.5px;margin-bottom:8px">
        自动补号：可用账号数低于目标时，通过易码短信平台自动注册新账号并换绑 Key；
        每日消费达到上限后停止，次日重置。「易码 Token」为密钥，回填时不显示，仅在你重新输入时才会提交。
      </div>
      <h2 style="margin-top:14px;padding-top:14px;border-top:1px solid var(--border)">补号日志 <span class="spacer"></span><button class="tiny" id="btn-clear-replenish-log">清屏</button></h2>
      <div id="replenish-logbox"></div>
      <h2 style="margin-top:14px;padding-top:14px;border-top:1px solid var(--border)">注册记录</h2>
      <div class="row" style="margin-bottom:10px;align-items:flex-end;flex-wrap:wrap">
        <label class="field grow" style="margin-bottom:0;flex:1;min-width:160px">
          <span>搜索</span>
          <input id="rp-reg-q" type="text" placeholder="手机号 / 用户名">
        </label>
        <label class="field" style="width:92px;margin-bottom:0">
          <span>结果</span>
          <select id="rp-reg-status">
            <option value="">全部</option>
            <option value="ok">成功</option>
            <option value="fail">失败</option>
          </select>
        </label>
        <label class="field" style="width:92px;margin-bottom:0">
          <span>类型</span>
          <select id="rp-reg-kind">
            <option value="">全部</option>
            <option value="new">新号</option>
            <option value="takeover">接管</option>
          </select>
        </label>
        <button class="primary" id="rp-reg-search" style="height:33px">查询</button>
      </div>
      <div id="rp-reg-table" style="overflow-x:auto"></div>
      <div class="row" style="margin-top:10px;align-items:center">
        <button class="tiny" id="rp-reg-prev">上一页</button>
        <span class="muted" id="rp-reg-page"></span>
        <button class="tiny" id="rp-reg-next">下一页</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Token 消耗（近 24 小时）<span class="spacer"></span><span class="muted" id="usage-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div id="usage-chart"></div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>积分采样明细（仅有差异）<span class="spacer"></span><span class="muted" id="samples-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div id="samples"></div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>泄漏守卫 <span class="spacer"></span><span id="leak-guard-badge"></span><span class="muted" id="leak-guard-note" style="text-transform:none;letter-spacing:0;margin-left:8px"></span></h2>
    <div class="grid kpis" id="leak-guard-stats" style="margin-bottom:14px"></div>
    <div id="leak-guard-pending"></div>
    <div class="muted" style="font-size:11.5px;margin-top:8px;line-height:1.6">
      每 10 分钟扫描一次：若窗口内网关无 token 消耗、却有账号通用池积分被消耗，判定该账号疑似泄漏并记入待轮换清单；
      每天 02:00 统一「重新登录 → 注销全部 Key → 新建 Key」并更新配置。
    </div>
  </div>

  <div class="grid two" style="margin-bottom:14px">
    <div class="card">
      <h2>网关接入信息 <span class="spacer"></span><button class="tiny" id="btn-copy-base">复制地址</button></h2>
      <div id="gateway"></div>
      <div class="tabs" id="snippet-tabs" style="margin-top:12px"></div>
      <pre id="snippet"></pre>
    </div>

    <div class="card">
      <h2>默认模型 <span class="spacer"></span><button class="tiny" id="btn-models">刷新清单</button></h2>
      <label class="field">
        <span>当前生效模型</span>
        <select id="model-select"></select>
      </label>
      <div id="model-meta" style="font-size:12px;margin-bottom:6px"></div>
      <div id="model-count" class="muted" style="font-size:11.5px;margin-bottom:11px"></div>
      <div class="row">
        <button class="primary grow" id="btn-apply-model">应用为默认模型</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:11px;line-height:1.6">
        网关对上层完全透明：客户端请求里带 <code>model</code> 就用它的，
        没带则回落到这里的默认值。
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Key 池 <span class="spacer"></span><span class="muted" id="pool-note" style="text-transform:none;letter-spacing:0"></span><button class="tiny" id="btn-export-accounts">复制全部账号</button><button class="primary" id="btn-import">批量新增</button></h2>
    <div class="row" style="margin-bottom:10px;align-items:flex-end;flex-wrap:wrap">
      <label class="field" style="width:150px;margin-bottom:0"><span>用户名</span>
        <input id="pool-f-user" type="text" placeholder="筛选用户名" autocomplete="off"></label>
      <label class="field" style="width:140px;margin-bottom:0"><span>手机号</span>
        <input id="pool-f-phone" type="text" placeholder="筛选手机号" autocomplete="off"></label>
      <label class="field grow" style="margin-bottom:0;flex:1;min-width:140px"><span>Key</span>
        <input id="pool-f-key" class="mono" type="text" placeholder="筛选 Key（脱敏片段）" autocomplete="off"></label>
      <label class="field" style="width:104px;margin-bottom:0"><span>状态</span>
        <select id="pool-f-status">
          <option value="">全部</option>
          <option value="healthy">可用</option>
          <option value="cooldown">冷却中</option>
          <option value="invalid">已失效</option>
        </select></label>
      <label class="field" style="width:84px;margin-bottom:0"><span>每页</span>
        <select id="pool-page-size">
          <option value="10">10</option>
          <option value="20" selected>20</option>
          <option value="50">50</option>
        </select></label>
      <button class="tiny" id="pool-f-reset" style="height:33px">重置</button>
    </div>
    <div id="pool"></div>
    <div class="row" style="margin-top:10px;align-items:center">
      <button class="tiny" id="pool-prev">上一页</button>
      <span class="muted" id="pool-page"></span>
      <button class="tiny" id="pool-next">下一页</button>
      <span class="spacer"></span>
      <span class="muted" id="pool-count" style="text-transform:none;letter-spacing:0"></span>
    </div>

    <div style="margin-top:15px;padding-top:14px;border-top:1px solid var(--border)">
      <h2 style="margin-bottom:10px">添加 Key</h2>
      <div class="row">
        <label class="field grow" style="margin-bottom:0">
          <span>API Key（支持一次粘贴多把，逗号或换行分隔）</span>
          <input id="new-key" class="mono" placeholder="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        </label>
        <label class="field" style="width:150px;margin-bottom:0">
          <span>归属账号</span>
          <input id="new-account" placeholder="留空自动命名">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>并发上限</span>
          <input id="new-concurrency" type="number" min="1" max="64" value="4">
        </label>
        <button class="primary" id="btn-add" style="height:33px">添加</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:9px">
        添加前会先逐把校验凭据（失败的直接拒收，不污染池子）；成功后立即写入配置文件并参与轮换。
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>模型测试 <span class="spacer"></span><span class="muted" id="modeltest-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div class="row" style="margin-bottom:12px">
      <label class="field grow" style="margin-bottom:0">
        <span>模型</span>
        <select id="modeltest-model"></select>
      </label>
      <div style="width:200px;position:relative">
        <span style="display:block;font-size:11.5px;color:var(--muted);margin-bottom:4px">账号（可多选，服务端并发）</span>
        <button type="button" id="modeltest-accounts" style="width:100%;box-sizing:border-box;height:33px;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:var(--card);color:var(--text);border:1px solid var(--border-strong);border-radius:7px;padding:0 10px;font-size:13px;cursor:pointer">选择账号…</button>
        <div id="modeltest-accounts-menu" style="display:none;position:absolute;top:100%;left:0;right:0;z-index:30;margin-top:4px;max-height:220px;overflow:auto;background:var(--card);border:1px solid var(--border-strong);border-radius:8px;padding:4px;box-shadow:0 8px 24px rgba(0,0,0,.18)"></div>
      </div>
      <label class="field" style="width:112px;margin-bottom:0">
        <span>推理强度</span>
        <select id="modeltest-effort">
          <option value="none">关</option>
          <option value="low">低</option>
          <option value="medium" selected>中</option>
          <option value="high">高</option>
        </select>
      </label>
      <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
        <input id="modeltest-stream" type="checkbox" checked style="width:auto;margin:0">
        <span>流式请求</span>
      </label>
      <button class="primary" id="modeltest-run" style="height:33px">开始测试</button>
    </div>
    <label class="field" style="margin-bottom:10px">
      <span>测试提示词</span>
      <textarea id="modeltest-prompt" class="mono" rows="3" placeholder="输入测试提示词…"
        style="width:100%;box-sizing:border-box;font-family:var(--mono);font-size:12px;color:var(--text);background:var(--bg);border:1px solid var(--border-strong);border-radius:7px;padding:8px 10px;resize:vertical"></textarea>
    </label>
    <div id="modeltest-results"></div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>运行参数</h2>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>调度策略</span>
          <select id="opt-strategy">
            <option value="round_robin">round_robin · 轮转（配额均摊最均匀）</option>
            <option value="least_inflight">least_inflight · 最少在途</option>
            <option value="least_recent">least_recent · 最久未用</option>
            <option value="weighted">weighted · 加权随机</option>
          </select>
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>限速模式</span>
          <select id="opt-rate-mode">
            <option value="adaptive">adaptive · AIMD</option>
            <option value="fixed">fixed · 固定</option>
            <option value="off">off · 不限速</option>
          </select>
        </label>
        <label class="field" style="width:112px;margin-bottom:0">
          <span>目标 QPS</span>
          <input id="opt-qps" type="number" step="0.05" min="0.01">
        </label>
      </div>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>单请求等待预算（秒，0 = 不限）</span>
          <input id="opt-wait" type="number" step="5" min="0">
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>最大重试次数</span>
          <input id="opt-attempts" type="number" min="1" max="50">
        </label>
        <button class="primary" id="btn-apply-options" style="height:33px">保存并应用</button>
      </div>
      <div class="row" style="margin-bottom:12px;align-items:flex-end;flex-wrap:wrap">
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-enabled" type="checkbox" style="width:auto;margin:0">
          <span style="font-weight:600">智能一换一</span>
        </label>
        <label class="field" style="width:82px;margin-bottom:0">
          <span>并发</span>
          <input id="opt-fx-concurrency" type="number" min="1" max="64">
        </label>
        <label class="field" style="width:96px;margin-bottom:0">
          <span>单轮上限</span>
          <input id="opt-fx-req" type="number" min="1" max="2048">
        </label>
        <label class="field" style="width:110px;margin-bottom:0">
          <span>触发间隔(秒)</span>
          <input id="opt-fx-interval" type="number" min="60" step="60">
        </label>
        <label class="field" style="width:130px;margin-bottom:0">
          <span>长文 max_tokens</span>
          <input id="opt-fx-tokens" type="number" min="128" max="16384" step="128">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>大图边长px</span>
          <input id="opt-fx-imgsize" type="number" min="256" max="2048" step="256">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>每请求图片数</span>
          <input id="opt-fx-imgcount" type="number" min="1" max="9">
        </label>
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-image" type="checkbox" style="width:auto;margin:0">
          <span>带大图</span>
        </label>
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-yield" type="checkbox" style="width:auto;margin:0">
          <span title="有用户请求在途时烧点主动让路，优先保证首字延迟">服务优先</span>
        </label>
        <label class="field" style="width:118px;margin-bottom:0">
          <span>内存下限(MB)</span>
          <input id="opt-fx-minmem" type="number" min="0" max="8192" step="50">
        </label>
      </div>
      <div class="muted" style="font-size:11.5px;margin-bottom:8px">
        智能一换一：账号因「套餐额度耗尽」冷却期间，自动用该账号持续调用推广池模型
        （sensenova-6.8-flash-lite，长文/多图交替），按官方活动「1 推广池积分换 1 通用池积分」加速回补；
        每轮结束探测一次通用池，回补成功立即复活账号收工；撞推广池窗口限流会自动放慢，耗尽才停。
      </div>
      <div class="muted" style="font-size:11.5px">
        AIMD 模式下「目标 QPS」是**起始速率**；工具会自己往上下界之间收敛，撞 429 就降、
        长时间干净就升。改参数会重置收敛点，从起始速率重新探测。
      </div>
    </div>

    <div class="card">
      <h2>实时日志 <span class="spacer"></span>
        <button class="tiny" id="btn-autoscroll">自动滚动：开</button>
        <button class="tiny" id="btn-clearlog">清屏</button>
      </h2>
      <div id="logbox"></div>
      <div class="muted" style="font-size:11.5px;margin-top:8px" id="logfile"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="chart-tip" id="usage-tip"></div>

<dialog id="import-dialog">
  <h2>批量新增 Key</h2>
  <p class="muted" style="font-size:11.5px">每行一条，支持两种格式：<br>
    1) 纯 apikey：<code>sk-xxxx</code><br>
    2) 手机--用户名--密码--apikey：<code>13800000000--user--pass--sk-xxxx</code></p>
  <textarea id="import-lines" rows="8" class="mono" placeholder="sk-xxxx&#10;13800000000--user--pass--sk-yyyy"></textarea>
  <label class="field"><span>归属账号（仅格式 1，留空自动命名）</span><input id="import-account" placeholder="留空自动命名"></label>
  <div class="row"><button class="primary" id="btn-import-run">导入</button>
    <button id="btn-import-close">关闭</button></div>
  <div id="import-result"></div>
</dialog>

<script>
"use strict";

var S = {
  token: localStorage.getItem("st_rotator_token") || localStorage.getItem("sn_rotator_token") || "",
  cursor: 0,
  autoscroll: true,
  tab: "python",
  optionsDirty: false,
  models: [],
  quota: [],
  usage: [],
  samples: [],
  timerState: null,
  timerLog: null,
  timerCountdown: null,
  timerQuota: null,
  timerUsage: null,
  timerSamples: null,
  modelTestController: null,
  modelTestGrid: null,
  modelTestCards: null,
  modelTestTimer: null,
  modelTestStart: 0,
  rpRegPage: 1,
  rpRegSize: 20,
  poolPage: 1,
  poolSize: 20
};

/* 窗口是带 #token=xxx 打开的（fragment 不会发给服务端，也不进 Referer）。
   读出来存进 localStorage，然后立刻从地址栏抹掉，避免残留在可见 URL 里。 */
(function readTokenFromHash() {
  var match = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
  if (!match) return;
  try {
    S.token = decodeURIComponent(match[1]);
    localStorage.setItem("st_rotator_token", S.token);
  } catch (err) { /* 非法编码，忽略 */ }
  history.replaceState(null, "", location.pathname + location.search);
})();

/* ------------------------------------------------------------------ 工具 */

function $(id) { return document.getElementById(id); }

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, kind) {
  var box = $("toast");
  var item = document.createElement("div");
  item.className = kind || "";
  item.textContent = message;
  box.appendChild(item);
  setTimeout(function () { item.remove(); }, kind === "err" ? 6000 : 3200);
}

function fmtInt(n) { return Number(n || 0).toLocaleString("en-US"); }

function fmtTime(ts) {
  if (!ts) return "—";
  var d = new Date(ts * 1000);
  function p(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  var d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
  var m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  if (d) return d + "天" + h + "小时";
  if (h) return h + "小时" + m + "分";
  if (m) return m + "分" + s + "秒";
  return s + "秒";
}

function fmtCtx(n) {
  if (!n) return "";
  if (n >= 1048576) return Math.round(n / 1048576 * 10) / 10 + "M";
  if (n >= 1024) return Math.round(n / 1024) + "K";
  return String(n);
}

/* ------------------------------------------------------------------ 余量 */

function fmtCountdown(resetAt, longForm) {
  if (!resetAt) return "—";
  var secs = resetAt - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "—";
  var d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
  return longForm ? (d + " d " + h + " h " + m + " m") : (h + " h " + m + " m");
}

/* 只取“未来”的重置时间：个别账号窗口刚过期时 reset_at 会短暂落在过去，若计入会把倒计时显示成 —。 */
function minReset(a, b) { var now = Math.floor(Date.now() / 1000); if (!b || b <= now) return a; return a ? Math.min(a, b) : b; }

function quotaAggregate() {
  var agg = {g5: 0, g7: 0, f5: 0, f7: 0, reset5: null, reset7: null, ok: 0};
  (S.quota || []).forEach(function (a) {
    if (a.status !== "ok") return;
    agg.ok += 1;
    function add(pair, k5, k7, r5, r7) {
      if (!pair) return;
      if (pair.h5) { agg[k5] += pair.h5.remaining || 0; agg[r5] = minReset(agg[r5], pair.h5.reset_at); }
      if (pair.d7) { agg[k7] += pair.d7.remaining || 0; agg[r7] = minReset(agg[r7], pair.d7.reset_at); }
    }
    add(a.general, "g5", "g7", "reset5", "reset7");
    add(a.flash_lite, "f5", "f7", "reset5", "reset7");
  });
  return agg;
}

/* 没有任何可用账号贡献时聚合值显示 —（而非误导性的 0）。 */
function aggText(agg, key) { return agg.ok ? fmtInt(Math.round(agg[key])) : "—"; }

function quotaFor(account) {
  var list = S.quota || [];
  for (var i = 0; i < list.length; i++) if (list[i].account === account) return list[i];
  return null;
}

function quotaRemaining(pair, window) {
  if (!pair || !pair[window]) return "—";
  var remaining = pair[window].remaining;
  return remaining == null ? "—" : fmtInt(Math.round(remaining));
}

function quotaResetAt(pair, window) {
  return pair && pair[window] ? pair[window].reset_at : null;
}

function countdownCell(resetAt, longForm) {
  if (!resetAt) return '<td class="num">—</td>';
  return '<td class="num" data-reset-at="' + esc(resetAt) + '"' + (longForm ? ' data-long="1"' : "") + '>' +
    esc(fmtCountdown(resetAt, longForm)) + "</td>";
}

async function fetchQuota(force) {
  try {
    var data = await api("/api/quota" + (force ? "?refresh=1" : ""));
    S.quota = data.accounts || [];
    S.consumption = data.consumption || null;
  } catch (e) { S.quota = []; }
}

/* 余量单独按 ~60s 节奏刷新：避免 2s 轮询对失败账号反复重登（C1）。 */
async function refreshQuota(force) {
  await fetchQuota(force);
  renderKpis(lastState);
  renderPool(lastState);
  renderUsage();
}

async function fetchUsage() {
  try { S.usage = (await api("/api/usage?hours=24")).buckets || []; }
  catch (e) { S.usage = []; }
}

async function refreshUsage() {
  await fetchUsage();
  renderUsage();
}

async function fetchSamples() {
  try { S.samples = (await api("/api/credits/samples?nonzero=1&limit=200")).samples || []; }
  catch (e) { S.samples = []; }
}

async function refreshSamples() {
  await fetchSamples();
  renderSamples();
}

/* 积分采样明细：只展示有差异（delta>0）的账号采样，按时间倒序。 */
function renderSamples() {
  var host = $("samples");
  if (!host) return;
  var data = (S.samples || []).slice().reverse();
  var note = $("samples-note");
  if (!data.length) { host.innerHTML = '<div class="empty">暂无差异（采样每 5 分钟一次）</div>'; if (note) note.textContent = ""; return; }
  var head = "<tr><th>时间</th><th>账号</th><th>池</th><th class='num'>used</th><th class='num'>消耗 Δ</th></tr>";
  var body = data.map(function (s) {
    var t = new Date(s.ts * 1000);
    var hh = ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2) + ":" + ("0" + t.getSeconds()).slice(-2);
    var pool = s.pool === "general" ? "通用" : "专属";
    return "<tr><td class='mono'>" + hh + "</td><td>" + esc(s.account) + "</td><td>" + pool + "</td><td class='num'>" + fmtInt(Math.round(s.used)) + "</td><td class='num'>+" + fmtInt(Math.round(s.delta)) + "</td></tr>";
  }).join("");
  host.innerHTML = "<table>" + head + body + "</table>";
  if (note) note.textContent = "共 " + data.length + " 条差异采样";
}

/* 近 24 小时：柱=token（本网关 chat 请求，蓝输入/橙输出），线=上游池额度消耗（绿=通用/紫=专属，整账号口径，右轴）。 */
function tipText(hh, b, gv, fv) {
  return hh + ":00　token 输入 " + fmtInt(b.prompt) + " / 输出 " + fmtInt(b.completion) + " / 合计 " + fmtInt(b.total) +
    "　·　积分 通用 " + fmtInt(Math.round(gv)) + " / 专属 " + fmtInt(Math.round(fv));
}

function renderUsage() {
  var host = $("usage-chart");
  if (!host) return;
  var data = S.usage || [];
  var note = $("usage-note");
  if (!data.length) { host.innerHTML = '<div class="empty">暂无数据</div>'; if (note) note.textContent = ""; return; }
  var max = 0;
  data.forEach(function (b) { if (b.total > max) max = b.total; });
  var consSeries = (S.consumption && S.consumption.series) || [];
  var consByHour = {};
  consSeries.forEach(function (c) { consByHour[c.hour] = c; });
  var creditMax = 0;
  data.forEach(function (b) {
    var c = consByHour[b.hour];
    if (c) creditMax = Math.max(creditMax, c.general || 0, c.flash_lite || 0);
  });
  if (max <= 0 && creditMax <= 0) { host.innerHTML = '<div class="empty">近 24 小时暂无数据</div>'; if (note) note.textContent = ""; return; }
  var W = 960, H = 176, padL = 8, padR = 8, padT = 10, baseY = 146, n = data.length;
  var bw = (W - padL - padR) / n;
  var bars = "", labels = "", ptsG = [], ptsF = [];
  data.forEach(function (b, i) {
    var x = padL + i * bw;
    var t = new Date(b.hour * 1000);
    var hh = ("0" + t.getHours()).slice(-2);
    var c = consByHour[b.hour];
    var gv = c ? (c.general || 0) : 0;
    var fv = c ? (c.flash_lite || 0) : 0;
    var totalH = max > 0 ? (baseY - padT) * (b.total / max) : 0;
    var promptH = b.total > 0 ? totalH * (b.prompt / b.total) : 0;
    var completionH = totalH - promptH;
    bars += '<g data-tip="' + esc(tipText(hh, b, gv, fv)) + '">' +
      '<rect x="' + x.toFixed(1) + '" y="' + padT + '" width="' + bw.toFixed(1) + '" height="' + (baseY - padT) + '" fill="transparent"/>' +
      '<rect x="' + (x + bw * 0.1).toFixed(1) + '" y="' + (baseY - promptH - completionH).toFixed(1) + '" width="' + (bw * 0.8).toFixed(1) + '" height="' + Math.max(0, completionH).toFixed(1) + '" fill="#f0a429"/>' +
      '<rect x="' + (x + bw * 0.1).toFixed(1) + '" y="' + (baseY - promptH).toFixed(1) + '" width="' + (bw * 0.8).toFixed(1) + '" height="' + Math.max(0, promptH).toFixed(1) + '" fill="#5b93ff"/></g>';
    if (creditMax > 0) {
      var cx = x + bw / 2;
      ptsG.push([cx, baseY - (gv / creditMax) * (baseY - padT)]);
      ptsF.push([cx, baseY - (fv / creditMax) * (baseY - padT)]);
    }
    if (t.getHours() % 3 === 0) {
      labels += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (baseY + 18) + '" fill="#93a0b4" font-size="11" text-anchor="middle">' + hh + '</text>';
    }
  });
  function poly(pts, color) {
    if (pts.length < 2) return "";
    return '<polyline fill="none" stroke="' + color + '" stroke-width="2" style="pointer-events:none" points="' +
      pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") + '"/>';
  }
  var lines = creditMax > 0 ? poly(ptsG, "#34d399") + poly(ptsF, "#a78bfa") : "";
  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">' +
    '<line x1="' + padL + '" y1="' + baseY + '" x2="' + (W - padR) + '" y2="' + baseY + '" stroke="#2a3240"/>' +
    bars + lines + labels + '</svg>';
  var sum = data.reduce(function (a, b) { return a + b.total; }, 0);
  if (note) note.textContent = "柱=token 合计 " + fmtInt(sum) + "（本网关 chat，蓝=输入/橙=输出） · 线=上游池额度消耗（整账号口径、含非本网关用量、上游结算有延迟；绿=通用/紫=专属，右轴）";
  host.onmousemove = function (e) {
    var tip = $("usage-tip");
    if (!tip) return;
    var g = e.target && e.target.closest ? e.target.closest("g[data-tip]") : null;
    if (g) {
      tip.textContent = g.getAttribute("data-tip");
      tip.style.display = "block";
      tip.style.left = (e.clientX + 12) + "px";
      tip.style.top = (e.clientY + 12) + "px";
    } else {
      tip.style.display = "none";
    }
  };
  host.onmouseleave = function () { var tip = $("usage-tip"); if (tip) tip.style.display = "none"; };
}

/* ------------------------------------------------------------------ 请求 */

async function api(path, options) {
  options = options || {};
  var headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (S.token) headers["Authorization"] = "Bearer " + S.token;
  var response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers: headers }));
  } catch (err) {
    throw new Error("网关无响应：" + err.message);
  }
  if (response.status === 401) {
    $("authbar").classList.add("show");
    throw new Error("需要 Token 才能访问控制台接口");
  }
  var data = {};
  try { data = await response.json(); } catch (err) { /* 空响应体 */ }
  if (!response.ok) {
    var detail = (data && data.error && (data.error.message || data.error)) || ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ------------------------------------------------------------------ 渲染 */

function consText(pool, win) {
  var c = S.consumption;
  if (!c || !c[pool] || c[pool][win] == null) return "—";
  return fmtInt(Math.round(c[pool][win]));
}


function renderKpis(state) {
  var s = state.summary || {};
  var rate = state.rate_control || {};
  var m = state.metrics || {};
  var rateText, rateClass = "", rateSub = "";
  if (rate.mode === "adaptive") {
    rateText = (rate.rate || 0).toFixed(2);
    rateSub = "区间 " + rate.min_rate + "~" + rate.max_rate;
    if (rate.penalties > rate.raises * 1.6 && rate.penalties > 3) rateClass = "amber";
    else if (rate.penalties === 0) rateClass = "green";
  } else if (rate.mode === "fixed") {
    rateText = (rate.rate || 0).toFixed(2); rateSub = "固定速率";
  } else {
    rateText = "关"; rateSub = "不限速";
  }
  var attempts = m.upstream_attempts || 0;
  var amp = m.client_requests ? (attempts / m.client_requests) : 0;
  var ampText = "上游尝试 " + fmtInt(attempts) + " 次";
  if (amp > 1.05) ampText += "（放大 " + amp.toFixed(2) + "×）";
  var agg = quotaAggregate();
  var cards = [
    ["可用 Key", fmtInt(s.healthy), "green", "共 " + fmtInt(s.total) + " 把"],
    ["冷却中", fmtInt(s.cooldown), s.cooldown ? "amber" : "", "等待恢复"],
    ["已失效", fmtInt(s.invalid), s.invalid ? "red" : "", "401/403 已隔离"],
    ["当前限速", rateText, rateClass, rateSub + " req/s"],
    ["已服务请求", fmtInt(m.client_requests), "", ampText],
    ["运行时长", fmtDuration(m.uptime_seconds), "", m.stream_requests ? "其中流式 " + fmtInt(m.stream_requests) : "进程已启动"],
    ["通用积分 5h 累计余量", aggText(agg, "g5"), agg.g5 ? "green" : "", "所有可用账号合计"],
    ["通用积分 7d 累计余量", aggText(agg, "g7"), agg.g7 ? "green" : "", "所有可用账号合计"],
    ["Flash-Lite 专属积分 5h 累计余量", aggText(agg, "f5"), agg.f5 ? "green" : "", "所有可用账号合计"],
    ["Flash-Lite 专属积分 7d 累计余量", aggText(agg, "f7"), agg.f7 ? "green" : "", "所有可用账号合计"],
    ["5h 重置倒计时", fmtCountdown(agg.reset5, false), "", "最早到期窗口", agg.reset5, false],
    ["7d 重置倒计时", fmtCountdown(agg.reset7, true), "", "最早到期窗口", agg.reset7, true],
    ["近1h 通用积分消耗", consText("general", "h1"), "", "上游池额度差值（整账号口径）"],
    ["近5h 通用积分消耗", consText("general", "h5"), "", "上游池额度差值（整账号口径）"],
    ["近24h 通用积分消耗", consText("general", "h24"), "", "上游池额度差值（整账号口径）"],
    ["近7d 通用积分消耗", consText("general", "d7"), "", "上游池额度差值（整账号口径）"],
    ["近30d 通用积分消耗", consText("general", "d30"), "", "上游池额度差值（整账号口径）"],
    ["近1h 专属积分消耗", consText("flash_lite", "h1"), "", "上游池额度差值（整账号口径）"],
    ["近5h 专属积分消耗", consText("flash_lite", "h5"), "", "上游池额度差值（整账号口径）"],
    ["近24h 专属积分消耗", consText("flash_lite", "h24"), "", "上游池额度差值（整账号口径）"],
    ["近7d 专属积分消耗", consText("flash_lite", "d7"), "", "上游池额度差值（整账号口径）"],
    ["近30d 专属积分消耗", consText("flash_lite", "d30"), "", "上游池额度差值（整账号口径）"]
  ];
  $("kpis").innerHTML = cards.map(function (c) {
    var attr = c[4] ? ' data-reset-at="' + esc(c[4]) + '"' + (c[5] ? ' data-long="1"' : "") : "";
    return '<div class="card kpi"><div class="label">' + esc(c[0]) + '</div>' +
      '<div class="value ' + c[2] + '"' + attr + '>' + esc(c[1]) + '</div>' +
      '<div class="label" style="margin-top:2px">' + esc(c[3]) + '</div></div>';
  }).join("");
  if (rate.mode === "adaptive") renderSpark(rate);
}

function renderSpark(rate) {
  var history = (rate.history || []).filter(function (p) { return p && p.length === 2; });
  // 曲线要挂在"当前限速"卡片下（第 4 张），而不是第一张——它是速率的历史轨迹
  var host = $("kpis").children[3];
  if (!host || history.length < 2) return;
  var rates = history.map(function (p) { return p[1]; });
  var lo = Math.min.apply(null, rates), hi = Math.max.apply(null, rates);
  if (hi - lo < 1e-6) { hi = lo + 0.01; }
  var w = 100, h = 30;
  var points = history.map(function (p, i) {
    var x = i / (history.length - 1) * w;
    var y = h - (p[1] - lo) / (hi - lo) * (h - 4) - 2;
    return x.toFixed(2) + "," + y.toFixed(2);
  }).join(" ");
  var svg = '<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" ' +
    'title="AIMD 速率轨迹（' + history.length + ' 次调整）">' +
    '<polyline fill="none" stroke="var(--accent)" stroke-width="1.2" ' +
    'vector-effect="non-scaling-stroke" points="' + points + '"></polyline></svg>';
  var node = host.querySelector(".kpi-spark");
  if (!node) {
    node = document.createElement("div");
    node.className = "kpi-spark";
    host.appendChild(node);
  }
  node.innerHTML = svg;
}

var REPLENISH_BADGES = {
  blocked_cap: ["今日额度已用尽", "invalid"],
  captcha_required: ["需要滑块验证码", "cooldown"],
  idle: ["已达目标", "neutral"],
  ok: ["补号成功", "healthy"],
  check_error: ["补号失败", "invalid"],
  unavailable: ["不可用", "neutral"]
};

var LEAK_GUARD_BADGES = {
  idle: ["正常", "healthy"],
  flagged: ["发现疑似泄漏", "cooldown"],
  rotated: ["已轮换", "healthy"],
  check_error: ["异常", "invalid"],
  disabled: ["未启用", "neutral"],
  not_running: ["已开启未运行", "neutral"]
};

function fmtClock(ts) {
  if (!ts) return "—";
  var d = new Date(ts * 1000);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/* 泄漏守卫：网关空转却有通用池积分消耗 → 记录待轮换账号，每日 02:00 统一轮换。 */
function renderLeakGuard(state) {
  var lg = state.leak_guard_status || {};
  var badgeEl = $("leak-guard-badge");
  var noteEl = $("leak-guard-note");
  var known = LEAK_GUARD_BADGES[lg.status] || (lg.status ? [lg.status, "neutral"] : null);
  if (badgeEl) badgeEl.innerHTML = known ? '<span class="pill ' + known[1] + '">' + esc(known[0]) + "</span>" : "";
  if (noteEl) noteEl.textContent = lg.message || "";
  var pendingCount = Number(lg.pending_count || 0);
  var hh = ("0" + (lg.rotate_hour != null ? lg.rotate_hour : 2)).slice(-2);
  var mm = ("0" + (lg.rotate_minute != null ? lg.rotate_minute : 0)).slice(-2);
  var cards = [
    ["待轮换账号", fmtInt(pendingCount), pendingCount ? "cooldown" : "", "疑似泄漏，等待定时轮换"],
    ["最近扫描", fmtClock(lg.last_scan_at), "", "窗口 " + fmtInt(lg.window_seconds || 0) + "s"],
    ["下次轮换", fmtClock(lg.next_rotate_at), "", "每日 " + hh + ":" + mm],
    ["最近轮换", lg.last_rotate_date || "—", "", "上次统一轮换日期"]
  ];
  var host = $("leak-guard-stats");
  if (host) {
    host.innerHTML = cards.map(function (c) {
      return '<div class="card kpi"><div class="label">' + esc(c[0]) + '</div>' +
        '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
        '<div class="label" style="margin-top:2px">' + esc(c[3]) + '</div></div>';
    }).join("");
  }
  var pendHost = $("leak-guard-pending");
  if (pendHost) {
    var pend = lg.pending || [];
    if (!pend.length) {
      pendHost.innerHTML = '<div class="empty">暂无待轮换账号</div>';
    } else {
      pendHost.innerHTML = "<table><tr><th>待轮换账号</th></tr>" +
        pend.map(function (n) { return "<tr><td>" + esc(n) + "</td></tr>"; }).join("") + "</table>";
    }
  }
}

function renderReplenish(state) {
  var rs = (state.replenish_status || {})._replenish || {};
  var rp = state.replenish_state || {};
  var rpo = (state.options && state.options.replenish) || {};
  var spend = rp.spend || {};
  var enabled = !!(state.options && state.options.replenish && state.options.replenish.enabled);
  var badge = "";
  var known = REPLENISH_BADGES[rs.status];
  if (rpo.sms_token_ok === false) {
    badge = '<span class="pill invalid">易码 Token 不可用</span>';
  } else if (known) {
    badge = '<span class="pill ' + known[1] + '">' + known[0] + "</span>";
  } else if (rs.status) {
    badge = '<span class="pill neutral">' + esc(rs.status) + "</span>";
  } else if (rp.running === false) {
    badge = '<span class="pill neutral">未启动</span>';
  }
  var note = "";
  if (rs.message) note = esc(rs.message);
  else if (rpo.sms_token_configured === false) note = "未配置易码 Token，补号未运行";
  else if (rpo.sms_token_ok === false) note = "易码 Token 不可用";
  else if (enabled && rp.running === false) note = "已开启，重启 ui / tray 后生效";
  var badgeEl = $("replenish-badge");
  if (badgeEl) badgeEl.innerHTML = badge;
  var noteEl = $("replenish-note");
  if (noteEl) noteEl.textContent = note;

  var balance = spend.last_balance != null
    ? "¥" + Number(spend.last_balance).toFixed(2) : "—";
  var consumed = "¥" + Number(rp.consumed || 0).toFixed(2);
  var avail = rp.available != null ? fmtInt(rp.available) : "—";
  var target = rp.target != null ? fmtInt(rp.target) : "—";
  var invalid = Number(rp.invalid_accounts || 0);
  var cards = [
    ["当前余额", balance, "", "易码短信平台"],
    ["今日已用", consumed, "", "当日余额差（换天自动归零）"],
    ["可用 / 目标", avail + " / " + target, "", "账号余量"],
    ["取号数量", fmtInt(rp.used_phones), "", "累计已用手机号"],
    ["注册成功", fmtInt(rp.registrations_ok), "", "累计成功建号"],
    ["已重置 Key", fmtInt(rp.rotations), "", "auto_renew 轮换次数"],
    ["已失效账号", fmtInt(rp.invalid_accounts), invalid ? "red" : "", "密码错误账号"]
  ];
  var host = $("replenish-stats");
  if (host) {
    host.innerHTML = cards.map(function (c) {
      return '<div class="card kpi"><div class="label">' + esc(c[0]) + '</div>' +
        '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
        '<div class="label" style="margin-top:2px">' + esc(c[3]) + '</div></div>';
    }).join("");
  }
}

async function renderReplenishRecords() {
  var host = $("rp-reg-table");
  if (!host) return;
  var q = encodeURIComponent(($("rp-reg-q").value || "").trim());
  var status = encodeURIComponent($("rp-reg-status").value || "");
  var kind = encodeURIComponent($("rp-reg-kind").value || "");
  var data;
  try {
    data = await api("/api/replenish/registrations?page=" + S.rpRegPage +
      "&size=" + S.rpRegSize + "&q=" + q + "&status=" + status + "&kind=" + kind);
  } catch (err) {
    host.innerHTML = '<div class="empty">' + esc(err.message) + "</div>";
    return;
  }
  var items = data.items || [];
  if (!items.length) {
    host.innerHTML = '<div class="empty">暂无注册记录</div>';
  } else {
    host.innerHTML = "<table><tr><th>注册时间</th><th>手机号</th><th>用户名</th><th>密码</th>" +
      "<th>新号</th><th>重置密码</th><th>成功</th><th>失败原因</th><th>失败详情</th></tr>" +
      items.map(function (e) {
        var when = new Date((e.created_at || 0) * 1000);
        var time = isNaN(when.getTime()) ? "" : when.toLocaleString();
        var isNew = e.is_new
          ? '<span class="pill healthy">新号</span>'
          : '<span class="pill cooldown">接管</span>';
        var ok = e.success
          ? '<span class="pill healthy">成功</span>'
          : '<span class="pill invalid">失败</span>';
        return "<tr><td>" + esc(time) + "</td><td>" + esc(e.phone) + "</td><td>" + esc(e.username) +
          "</td><td class='mono'>" + esc(e.password) + "</td><td>" + isNew + "</td><td>" +
          (e.password_reset ? "是" : "") + "</td><td>" + ok + "</td><td>" + esc(e.reason) +
          "</td><td>" + esc(e.detail) + "</td></tr>";
      }).join("") + "</table>";
  }
  var total = Number(data.total || 0);
  var lastPage = Math.max(1, Math.ceil(total / S.rpRegSize));
  $("rp-reg-page").textContent = "第 " + S.rpRegPage + " / " + lastPage + " 页（共 " + total + " 条）";
  $("rp-reg-prev").disabled = S.rpRegPage <= 1;
  $("rp-reg-next").disabled = S.rpRegPage >= lastPage;
}

function renderGateway(state) {
  var g = state.gateway || {};
  var tokenText = g.token || S.token || "未显示（用 #token= 打开可显示）";
  var rows = [
    ["Base URL", g.base_url],
    ["对话端点", g.chat_endpoint],
    ["API Key", tokenText],
    ["上游", g.upstream],
    ["健康检查", g.health_endpoint],
    ["运行统计", g.stats_endpoint]
  ];
  $("gateway").innerHTML = rows.map(function (r) {
    return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
      '<span class="v grow">' + esc(r[1]) + '</span></div>';
  }).join("");
  renderSnippet(g);
}

function renderSnippetTabs() {
  var tabs = [["python", "Python (OpenAI SDK)"], ["curl", "cURL"], ["node", "Node.js"], ["generic", "通用 Agent 配置"]];
  $("snippet-tabs").innerHTML = tabs.map(function (t) {
    return '<button data-tab="' + t[0] + '" class="' + (S.tab === t[0] ? "active" : "") + '">' + esc(t[1]) + '</button>';
  }).join("");
  Array.prototype.forEach.call($("snippet-tabs").children, function (button) {
    button.onclick = function () { S.tab = button.dataset.tab; renderSnippetTabs(); renderSnippet(lastGateway); };
  });
}

var lastGateway = {};

function renderSnippet(g) {
  if (g) lastGateway = g;
  g = lastGateway || {};
  var base = g.base_url || "http://127.0.0.1:8080/v1";
  var token = g.token || S.token || "<本地口令>";
  var model = g.model || "deepseek-v4-flash";
  var text;
  if (S.tab === "curl") {
    text = 'curl ' + base + '/chat/completions \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -H "Authorization: Bearer ' + token + '" \\\n' +
      '  -d \'{\n' +
      '    "model": "' + model + '",\n' +
      '    "messages": [{"role": "user", "content": "你好"}],\n' +
      '    "stream": false\n' +
      '  }\'';
  } else if (S.tab === "node") {
    text = 'import OpenAI from "openai";\n\n' +
      'const client = new OpenAI({\n' +
      '  baseURL: "' + base + '",\n' +
      '  apiKey: "' + token + '",   // 网关本地鉴权，非商汤 Key\n' +
      '});\n\n' +
      'const stream = await client.chat.completions.create({\n' +
      '  model: "' + model + '",\n' +
      '  messages: [{ role: "user", content: "你好" }],\n' +
      '  stream: true,\n' +
      '});\n' +
      'for await (const chunk of stream) {\n' +
      '  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");\n' +
      '}';
  } else if (S.tab === "generic") {
    text = '# 通用 OpenAI 兼容配置（WorkBuddy / Dify / Cherry Studio / LobeChat …）\n' +
      'base_url : ' + base + '\n' +
      'api_key  : ' + token + '\n' +
      'model    : ' + model + '\n' +
      'stream   : 支持（含 tool_calls 透传）\n\n' +
      '# 说明\n' +
      '#   - 上层的 api_key 填上面这个本地 Token，不是商汤的 sk- Key\n' +
      '#   - 商汤的多把 Key 全部由本网关内部轮换，上层无感知\n' +
      '#   - 429 / 冷却 / 坏 Key 隔离都在网关内消化';
  } else {
    text = 'from openai import OpenAI\n\n' +
      'client = OpenAI(\n' +
      '    base_url="' + base + '",\n' +
      '    api_key="' + token + '",   # 网关本地鉴权，非商汤 Key\n' +
      ')\n\n' +
      'stream = client.chat.completions.create(\n' +
      '    model="' + model + '",\n' +
      '    messages=[{"role": "user", "content": "你好"}],\n' +
      '    stream=True,\n' +
      ')\n' +
      'for chunk in stream:\n' +
      '    delta = chunk.choices[0].delta.content\n' +
      '    if delta:\n' +
      '        print(delta, end="", flush=True)';
  }
  $("snippet").textContent = text;
}

function renderModels(state) {
  var catalog = state.models || {};
  S.models = catalog.models || [];
  var select = $("model-select");
  var current = state.default_model;
  var options = S.models.map(function (m) { return m.id; });
  if (current && options.indexOf(current) === -1) options.unshift(current);
  if (!options.length) options = [current].filter(Boolean);
  select.innerHTML = options.map(function (id) {
    return '<option value="' + esc(id) + '"' + (id === current ? " selected" : "") + '>' + esc(id) + '</option>';
  }).join("") || '<option value="">（无可用模型）</option>';
  select.dataset.current = current || "";

  // 注意：详情写在 #model-meta，条数写在 #model-count。两者必须分开——
  // 早先共用一个元素时，"共 N 个模型"会把模型详情覆盖掉，详情永远看不见。
  var count = $("model-count");
  if (catalog.error) {
    count.innerHTML = '<span style="color:var(--red)">拉取模型清单失败：' + esc(catalog.error) + '</span>';
  } else if (!S.models.length) {
    count.textContent = "暂无清单，点「刷新清单」从上游拉取。";
  } else {
    var hidden = catalog.hidden || [];
    count.textContent = "共 " + S.models.length + " 个模型" + (catalog.cached ? "（缓存）" : "（刚拉取）")
      + (hidden.length ? "，已按配置隐藏 " + hidden.length + " 个：" + hidden.join("、") : "");
  }
  updateModelMeta();
}

function updateModelMeta() {
  var id = $("model-select").value;
  var found = S.models.filter(function (m) { return m.id === id; })[0];
  var node = $("model-meta");
  if (!found) { node.innerHTML = ""; return; }
  var bits = [];
  if (found.context_length) bits.push("上下文 " + fmtCtx(found.context_length));
  if (found.max_output_length) bits.push("最大输出 " + fmtCtx(found.max_output_length));
  if ((found.input_modalities || []).length) {
    bits.push("输入 " + found.input_modalities.join("+") + " → 输出 " + (found.output_modalities || []).join("+"));
  }
  var badges = (found.features || []).map(function (f) { return '<span class="badge">' + esc(f) + '</span>'; }).join("");
  node.innerHTML = '<span class="muted">' + esc(bits.join(" · ")) + "</span>" +
    (badges ? "<div style='margin-top:5px'>" + badges + "</div>" : "");
}

/* ------------------------------------------------------------------ 模型测试 */

/* 模型 / 账号下拉只在选项集合变化时重建，避免 2s 轮询每次都打断用户已选的值。 */
function renderModelTest(state) {
  var modelSel = $("modeltest-model");
  if (!modelSel) return;
  var ids = ((state.models || {}).models || []).map(function (m) { return m.id; });
  var names = state.account_names || [];
  var key = ids.join("\u0000") + "\u0000" + names.join("\u0000");
  if (modelSel.dataset.built === key) return;
  modelSel.innerHTML = ids.map(function (id) {
    return '<option value="' + esc(id) + '">' + esc(id) + '</option>';
  }).join("") || '<option value="">（无可用模型）</option>';
  var prev = mtSelectedAccounts();
  $("modeltest-accounts-menu").innerHTML = names.map(function (a) {
    return '<label style="display:flex;align-items:center;gap:6px;padding:5px 8px;border-radius:6px;cursor:pointer">' +
      '<input type="checkbox" value="' + esc(a) + '"' + (prev.indexOf(a) !== -1 ? " checked" : "") +
      ' style="width:auto;margin:0"><span>' + esc(a) + '</span></label>';
  }).join("") || '<div class="muted" style="padding:6px 8px">（无账号）</div>';
  mtUpdateAccountsLabel();
  var current = state.default_model;
  if (current && ids.indexOf(current) !== -1) modelSel.value = current;
  modelSel.dataset.built = key;
}

/* 账号下拉多选：已勾选账号 / 更新按钮文案。 */
function mtSelectedAccounts() {
  var menu = $("modeltest-accounts-menu");
  if (!menu) return [];
  return Array.prototype.slice
    .call(menu.querySelectorAll('input[type=checkbox]:checked'))
    .map(function (c) { return c.value; });
}

function mtUpdateAccountsLabel() {
  var btn = $("modeltest-accounts");
  if (!btn) return;
  var names = mtSelectedAccounts();
  btn.textContent = !names.length
    ? "选择账号…"
    : (names.length <= 2 ? names.join("、") : "已选 " + names.length + " 个账号");
}

/* 响应计时：点击开始后实时刷新 #modeltest-note。 */
function mtStartTimer() {
  mtStopTimer();
  S.modelTestStart = Date.now();
  var note = $("modeltest-note");
  if (note) note.textContent = "测试中… 0.0s";
  S.modelTestTimer = setInterval(function () {
    if (note) note.textContent = "测试中… " + ((Date.now() - S.modelTestStart) / 1000).toFixed(1) + "s";
  }, 100);
}

function mtStopTimer() {
  if (S.modelTestTimer) { clearInterval(S.modelTestTimer); S.modelTestTimer = null; }
}

/* 结果度量：首字 / 处理 / 输入 / 缓存 / 输出。 */
function modelTestMetrics(usage, latencyMs, ttftMs) {
  var u = usage || {};
  var details = u.prompt_tokens_details || {};
  var cached = null;
  if (details.cached_tokens != null) cached = details.cached_tokens;
  else if (u.cached_tokens != null) cached = u.cached_tokens;
  else if (u.prompt_cache_hit_tokens != null) cached = u.prompt_cache_hit_tokens;
  var n = function (v) { return v == null ? "—" : v; };
  return "首字 " + (ttftMs == null ? "—" : ttftMs + " ms") +
    " · 处理 " + (latencyMs == null ? "—" : latencyMs + " ms") +
    " · 输入 " + n(u.prompt_tokens) +
    " · 缓存 " + n(cached) +
    " · 输出 " + n(u.completion_tokens);
}

/* 每账号一张结果卡；流式与非流式共用这一渲染路径。 */
function modelTestCard(account) {
  var host = $("modeltest-results");
  if (!S.modelTestGrid) {
    S.modelTestGrid = document.createElement("div");
    S.modelTestGrid.className = "grid two";
    host.appendChild(S.modelTestGrid);
  }
  S.modelTestCards = S.modelTestCards || {};
  var card = document.createElement("div");
  card.className = "card";
  card.dataset.mtAccount = account;
  card.innerHTML = "<h3>" + esc(account) +
    ' <span class="pill neutral">测试中</span></h3>' +
    '<div class="mt-think" style="display:none;margin-bottom:8px">' +
    '<span class="muted" style="font-size:11.5px">思考</span>' +
    '<pre class="mono" style="white-space:pre-wrap;max-height:180px;overflow:auto;color:var(--muted);margin:4px 0 0"></pre></div>' +
    '<div class="mt-out" style="display:none">' +
    '<span class="muted" style="font-size:11.5px">输出</span>' +
    '<pre class="mono" style="white-space:pre-wrap;max-height:260px;overflow:auto;margin:4px 0 0"></pre></div>' +
    '<div class="muted mt-metrics" style="font-size:12px;margin-top:6px"></div>';
  S.modelTestGrid.appendChild(card);
  S.modelTestCards[account] = card;
  return card;
}

function modelTestPill(card, klass, text) {
  var pill = card.querySelector(".pill");
  if (pill) { pill.className = "pill " + klass; pill.textContent = text; }
}

function modelTestMuted(card, text) {
  var node = card.querySelector(".mt-metrics");
  if (node) node.innerHTML = esc(text);
}

/* 把一段文本追加到指定区块（思考 / 输出）；区块首次有内容时自动显示。 */
function mtAppend(card, cls, text) {
  var box = card.querySelector("." + cls);
  if (!box) return;
  box.style.display = "block";
  box.querySelector("pre").innerHTML += esc(text);
}

function onModelTestEvent(name, obj) {
  var card = (S.modelTestCards || {})[obj.account];
  if (name === "start") {
    modelTestCard(obj.account);
  } else if (name === "reasoning") {
    if (card) mtAppend(card, "mt-think", obj.text);
  } else if (name === "token") {
    if (card) mtAppend(card, "mt-out", obj.token);
  } else if (name === "usage") {
    if (card) card._mtUsage = obj.usage || {};
  } else if (name === "done") {
    if (card) {
      modelTestPill(card, "healthy", "成功");
      modelTestMuted(card, modelTestMetrics(card._mtUsage, obj.latency_ms, obj.ttft_ms));
    }
  } else if (name === "error") {
    card = card || modelTestCard(obj.account);
    modelTestPill(card, "invalid", "失败");
    modelTestMuted(card, obj.message || "未知错误");
  } else if (name === "complete") {
    mtStopTimer();
    var run = $("modeltest-run");
    run.disabled = false;
    run.innerHTML = "开始测试";
    var note = $("modeltest-note");
    if (note) note.textContent = "";
    toast("测试完成", "ok");
  }
}

/* 仓库内唯一的流式读取器：按 \n\n 拆帧，完整帧回调，残留半帧留在 buf 等下一段。 */
async function parseSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop();
    frames.forEach(function (frame) {
      let name = "message";
      let data = "";
      frame.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) name = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (!data) return;
      let obj;
      try { obj = JSON.parse(data); } catch (err) { return; }
      onEvent(name, obj);
    });
  }
}

/* 非流式结果：复用同一套每账号卡片（ok→成功/healthy，error→失败/invalid + error.message）。 */
function modelTestRenderResults(results) {
  results.forEach(function (r) {
    var card = modelTestCard(r.account);
    if (r.status === "ok") {
      modelTestPill(card, "healthy", "成功");
      modelTestMuted(card, modelTestMetrics(r.usage, r.latency_ms, r.ttft_ms));
    } else {
      modelTestPill(card, "invalid", "失败");
      modelTestMuted(card, (r.error && r.error.message) || "未知错误");
    }
    if (r.reasoning) mtAppend(card, "mt-think", r.reasoning);
    if (r.text) mtAppend(card, "mt-out", r.text);
  });
}

async function runModelTest() {
  var model = $("modeltest-model").value;
  var accounts = mtSelectedAccounts();
  var prompt = $("modeltest-prompt").value.trim();
  if (!model) { toast("请先选择模型", "err"); return; }
  if (!accounts.length) { toast("请至少选择一个账号", "err"); return; }
  if (!prompt) { toast("请输入测试提示词", "err"); return; }
  var run = $("modeltest-run");
  run.disabled = true;
  run.innerHTML = '<span class="spin">◌</span> 测试中…';
  $("modeltest-results").innerHTML = "";
  S.modelTestGrid = null;
  S.modelTestCards = {};
  var note = $("modeltest-note");
  mtStartTimer();
  var body = {
    model: model,
    accounts: accounts,
    prompt: prompt,
    reasoning_effort: $("modeltest-effort").value,
    stream: $("modeltest-stream").checked
  };
  var controller = null;
  if (body.stream) {
    if (S.modelTestController) S.modelTestController.abort();
    S.modelTestController = new AbortController();
    controller = S.modelTestController;
  }
  try {
    if (!body.stream) {
      var data = await api("api/model-test", { method: "POST", body: JSON.stringify(body) });
      modelTestRenderResults(data.results || []);
      mtStopTimer();
      if (note) note.textContent = "";
    } else {
      var headers = { "Content-Type": "application/json" };
      if (S.token) headers["Authorization"] = "Bearer " + S.token;
      var response = await fetch("api/model-test", {
        method: "POST",
        headers: headers,
        body: JSON.stringify(body),
        signal: controller.signal
      });
      if (response.status === 401) {
        $("authbar").classList.add("show");
        throw new Error("需要 Token 才能访问控制台接口");
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      await parseSSE(response, onModelTestEvent);
    }
  } catch (err) {
    if (controller && controller.signal.aborted) return;
    mtStopTimer();
    toast(err.message, "err");
    if (note) note.textContent = "测试失败：" + err.message;
  } finally {
    if (!(controller && controller.signal.aborted)) {
      run.disabled = false;
      run.innerHTML = "开始测试";
    }
    if (S.modelTestController === controller) S.modelTestController = null;
  }
}

function poolFilteredKeys(keys) {
  var fu = ($("pool-f-user") ? $("pool-f-user").value : "").trim().toLowerCase();
  var fp = ($("pool-f-phone") ? $("pool-f-phone").value : "").trim().toLowerCase();
  var fk = ($("pool-f-key") ? $("pool-f-key").value : "").trim().toLowerCase();
  var fs = $("pool-f-status") ? $("pool-f-status").value : "";
  return keys.filter(function (k) {
    if (fs && k.status !== fs) return false;
    if (fu) {
      var u = String(k.username || "").toLowerCase() + " " + String(k.account || "").toLowerCase();
      if (u.indexOf(fu) === -1) return false;
    }
    if (fp && String(k.phone || "").toLowerCase().indexOf(fp) === -1) return false;
    if (fk && String(k.key || "").toLowerCase().indexOf(fk) === -1) return false;
    return true;
  });
}

function renderPool(state) {
  var keys = state.keys || [];
  var as = (state.account_status) || {};
  if (!keys.length) {
    $("pool").innerHTML = '<div class="empty">池里还没有 Key。在下面添加至少一把才能对外提供服务。</div>';
    if ($("pool-page")) $("pool-page").textContent = "";
    if ($("pool-count")) $("pool-count").textContent = "";
    if ($("pool-prev")) $("pool-prev").disabled = true;
    if ($("pool-next")) $("pool-next").disabled = true;
  } else {
    var filtered = poolFilteredKeys(keys);
    var size = S.poolSize || 20;
    var lastPage = Math.max(1, Math.ceil(filtered.length / size));
    if (S.poolPage > lastPage) S.poolPage = lastPage;
    if (S.poolPage < 1) S.poolPage = 1;
    var start = (S.poolPage - 1) * size;
    var pageKeys = filtered.slice(start, start + size);
    var head = "<tr><th class='num'>ID</th><th>账号</th><th>Key</th><th>状态</th><th>冷却</th><th>RPM</th>" +
      "<th class='num'>成功/失败</th><th class='num'>429</th><th class='num'>延迟</th>" +
      "<th class='num' title='通用积分池 5 小时窗口剩余额度'>通用 5h 余量</th>" +
      "<th class='num' title='通用积分池 5 小时窗口重置倒计时'>通用 5h 重置</th>" +
      "<th class='num' title='通用积分池 7 天窗口剩余额度'>通用 7d 余量</th>" +
      "<th class='num' title='通用积分池 7 天窗口重置倒计时'>通用 7d 重置</th>" +
      "<th class='num' title='Flash-Lite 专属积分池 5 小时窗口剩余额度'>FL 专属 5h 余量</th>" +
      "<th class='num' title='Flash-Lite 专属积分池 7 天窗口剩余额度'>FL 专属 7d 余量</th>" +
      "<th class='num' title='该账号 Key 最近一次写入时间'>更新时间</th><th></th></tr>";
    var body = pageKeys.map(function (k) {
      var st = k.stats || {};
      var statusText = { healthy: "可用", cooldown: "冷却中", invalid: "已失效" }[k.status] || k.status;
      var cooldown = k.cooldown_remaining > 0 ? k.cooldown_remaining + "s" : "—";
      var q = quotaFor(k.account);
      var general = q && q.status === "ok" ? q.general : null;
      var flash = q && q.status === "ok" ? q.flash_lite : null;
      var label = k.username || k.account;
      return "<tr>" +
        '<td class="num">' + esc(k.id) + "</td>" +
        "<td>" + esc(label) +
          (as[k.account] && as[k.account].status === "password_error"
            ? ' <span class="pill invalid" title="' + esc(as[k.account].message || "登录失败：密码错误") + '">密码错误</span>'
            : "") +
        "</td>" +
        '<td class="mono">' + esc(k.key) + "</td>" +
        '<td><span class="pill ' + esc(k.status) + '">' + esc(statusText) + "</span></td>" +
        '<td class="num">' + esc(cooldown) + "</td>" +
        '<td class="mono">' + esc(k.rpm_window) + "</td>" +
        '<td class="num">' + fmtInt(st.successes) + " / " + fmtInt(st.failures) + "</td>" +
        '<td class="num">' + fmtInt(st.rate_limited) + "</td>" +
        '<td class="num">' + (st.avg_latency_ms ? Math.round(st.avg_latency_ms) + "ms" : "—") + "</td>" +
        '<td class="num">' + esc(quotaRemaining(general, "h5")) + "</td>" +
        countdownCell(quotaResetAt(general, "h5")) +
        '<td class="num">' + esc(quotaRemaining(general, "d7")) + "</td>" +
        countdownCell(quotaResetAt(general, "d7"), true) +
        '<td class="num">' + esc(quotaRemaining(flash, "h5")) + "</td>" +
        '<td class="num">' + esc(quotaRemaining(flash, "d7")) + "</td>" +
        '<td class="num" title="' + esc(k.updated_at ? fmtTime(k.updated_at) : "未知") + '">' + esc(fmtTime(k.updated_at)) + "</td>" +
        '<td style="text-align:right;white-space:nowrap">' +
          '<button class="tiny" data-act="verify" data-id="' + esc(k.id) + '" data-label="' + esc(label + " / " + k.key) + '">测试</button> ' +
          '<button class="tiny danger" data-act="remove" data-id="' + esc(k.id) + '" data-label="' + esc(label + " / " + k.key) + '">删除</button>' +
        "</td></tr>";
    }).join("");
    $("pool").innerHTML = "<table>" + head + body + "</table>";
    Array.prototype.forEach.call($("pool").querySelectorAll("button[data-act]"), function (button) {
      button.onclick = function () {
        onPoolAction(button.dataset.act, button.dataset.id, button.dataset.label, button);
      };
    });
    if ($("pool-page")) $("pool-page").textContent = "第 " + S.poolPage + " / " + lastPage + " 页（共 " + filtered.length + " 把）";
    if ($("pool-count")) $("pool-count").textContent = filtered.length !== keys.length ? ("已筛选 " + filtered.length + " / " + keys.length + " 把") : "";
    if ($("pool-prev")) $("pool-prev").disabled = S.poolPage <= 1;
    if ($("pool-next")) $("pool-next").disabled = S.poolPage >= lastPage;
  }
  var s = state.summary || {};
  $("pool-note").textContent = s.total ? ("在途 " + s.inflight + " / 共 " + s.total + " 把") : "";
}

function renderOptions(state) {
  // 用户一旦编辑过运行参数（勾选/输入），就暂停轮询回填：2 秒一次的状态刷新
  // 只能保护「当前聚焦的那一个」输入框，无法阻止它把同一表单里其它未保存字段
  // （例如刚勾上的「自动补号」、刚改的「目标账号数」）覆盖回服务端旧值。
  // 由 markOptionsDirty() 置脏、保存成功后清除。
  if (S.optionsDirty) return;
  var opt = state.options || {};
  var strategy = $("opt-strategy");
  if (document.activeElement !== strategy) strategy.value = opt.strategy || "round_robin";
  var mode = $("opt-rate-mode");
  if (document.activeElement !== mode) mode.value = opt.rate_mode || "off";
  var qps = $("opt-qps");
  if (document.activeElement !== qps) qps.value = opt.qps;
  var wait = $("opt-wait");
  if (document.activeElement !== wait) wait.value = opt.max_total_wait;
  var attempts = $("opt-attempts");
  if (document.activeElement !== attempts) attempts.value = opt.max_attempts;
  var fx = opt.flash_lite || {};
  var fe = $("opt-fx-enabled");
  if (document.activeElement !== fe) fe.checked = !!fx.enabled;
  var fi = $("opt-fx-image");
  if (document.activeElement !== fi) fi.checked = !!fx.image_enabled;
  var fy = $("opt-fx-yield");
  if (document.activeElement !== fy) fy.checked = fx.yield_to_serve !== false;
  var fm = $("opt-fx-minmem");
  if (document.activeElement !== fm && fx.min_available_mb != null) fm.value = fx.min_available_mb;
  [["opt-fx-concurrency", "concurrency"], ["opt-fx-req", "requests_per_trigger"],
   ["opt-fx-interval", "min_interval_s"], ["opt-fx-tokens", "long_text_max_tokens"],
   ["opt-fx-imgsize", "image_size"], ["opt-fx-imgcount", "multi_image_count"]].forEach(function (p) {
    var el = $(p[0]);
    if (document.activeElement !== el && fx[p[1]] != null) el.value = fx[p[1]];
  });
  var rp = opt.replenish || {};
  var rpe = $("opt-rp-enabled");
  if (document.activeElement !== rpe) rpe.checked = !!rp.enabled;
  var rpt = $("opt-rp-target");
  if (document.activeElement !== rpt && rp.target_count != null) rpt.value = rp.target_count;
  [["opt-rp-interval", "interval_seconds"], ["opt-rp-keyword", "keyword"],
   ["opt-rp-cap", "daily_spend_cap"], ["opt-rp-sms-interval", "sms_poll_interval"],
   ["opt-rp-sms-timeout", "sms_poll_timeout"], ["opt-rp-keyname", "key_name"]].forEach(function (p) {
    var el = $(p[0]);
    if (document.activeElement !== el && rp[p[1]] != null) el.value = rp[p[1]];
  });
  var rpk = $("opt-rp-keytype");
  if (document.activeElement !== rpk && rp.key_type) rpk.value = rp.key_type;
  // sms_token 是密钥：渲染时置空（服务端 snapshot 本就不回显），仅在用户重新输入后随请求发送。
  // 必须跳过正在输入的输入框，否则 2 秒轮询会把用户刚敲进去的 token 抹掉。
  var rps = $("opt-rp-sms-token");
  if (document.activeElement !== rps) rps.value = "";
}

/* ------------------------------------------------------------------ 日志 */

function classifyLog(text) {
  if (/\[警告\]|WARNING|429/.test(text)) return "warn";
  if (/\[错误\]|ERROR|失败|Traceback/.test(text)) return "err";
  if (/成功|已启动|ok/i.test(text)) return "ok";
  return "";
}

function logLine(item) {
  var line = document.createElement("div");
  line.className = classifyLog(item.text);
  var stamp = document.createElement("span");
  stamp.className = "dim";
  stamp.textContent = (item.time || "") + "  ";
  line.appendChild(stamp);
  line.appendChild(document.createTextNode(item.text));
  return line;
}

function appendReplenishLog(item) {
  var box = $("replenish-logbox");
  if (!box) return;
  box.appendChild(logLine(item));
  while (box.childElementCount > 300) box.removeChild(box.firstChild);
  if (S.autoscroll) box.scrollTop = box.scrollHeight;
}

function appendLogs(items) {
  var box = $("logbox");
  items.forEach(function (item) {
    // 补号日志只在「自动补号」面板内显示，不进主「实时日志」
    if (item.text.indexOf("[补号]") !== -1) { appendReplenishLog(item); return; }
    box.appendChild(logLine(item));
  });
  while (box.childElementCount > 800) box.removeChild(box.firstChild);
  if (S.autoscroll) box.scrollTop = box.scrollHeight;
}

async function pollLogs() {
  try {
    var data = await api("/api/logs?cursor=" + S.cursor);
    S.cursor = data.cursor;
    if (data.items && data.items.length) appendLogs(data.items);
  } catch (err) { /* 静默：日志轮询失败不该刷屏 */ }
}

/* ------------------------------------------------------------------ 交互 */

async function onPoolAction(action, keyId, label, button) {
  if (action === "remove") {
    if (!confirm("确定要从池中删除并写回配置文件吗？\n\n" + label)) return;
  }
  button.disabled = true;
  var original = button.textContent;
  button.innerHTML = '<span class="spin">◌</span>';
  try {
    var result = await api("/api/keys/" + action, {
      method: "POST",
      body: JSON.stringify({ id: keyId })
    });
    toast(result.message || "操作完成", result.ok === false ? "err" : "ok");
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function onAddKeys() {
  var raw = $("new-key").value.trim();
  if (!raw) { toast("请先填入 API Key", "err"); return; }
  var button = $("btn-add");
  button.disabled = true;
  button.innerHTML = '<span class="spin">◌</span> 校验中…';
  try {
    var result = await api("/api/keys/add", {
      method: "POST",
      body: JSON.stringify({
        keys: raw,
        account: $("new-account").value.trim(),
        max_concurrency: parseInt($("new-concurrency").value, 10) || 4
      })
    });
    var msg = "已添加 " + result.added.length + " 把 Key";
    if (result.rejected && result.rejected.length) msg += "；" + result.rejected.length + " 把被拒收";
    toast(msg, result.added.length ? "ok" : "err");
    if (result.rejected && result.rejected.length) {
      result.rejected.forEach(function (r) { toast("拒收 " + r.key + "：" + r.reason, "err"); });
    }
    if (result.added.length) { $("new-key").value = ""; }
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "添加";
  }
}

/* 复制全部账号：导出为导入格式（手机--用户名--密码--apikey），可直接粘回「批量新增」。 */
async function onExportAccounts() {
  try {
    var data = await api("/api/accounts/export");
    if (!data.text) { toast("没有可导出的账号（需配置 user/password）", "err"); return; }
    var label = "全部账号（" + data.accounts + " 个 / " + data.lines + " 行" +
      (data.skipped ? "，跳过 " + data.skipped + " 个无凭据" : "") + "）";
    copyText(data.text, label);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function onImportKeys() {
  var lines = $("import-lines").value;
  if (!lines.trim()) { toast("请先粘贴内容", "err"); return; }
  var btn = $("btn-import-run"); btn.disabled = true; btn.innerHTML = '<span class="spin">◌</span> 校验中…';
  try {
    var result = await api("/api/keys/import", { method: "POST", body: JSON.stringify({
      lines: lines, account: $("import-account").value.trim(), max_concurrency: 4 }) });
    var s = result.summary || {};
    $("import-result").innerHTML = "<table><tr><th>行</th><th>输入</th><th>判定</th><th>原因</th></tr>" +
      (result.results || []).map(function (r) {
        return "<tr><td>" + r.line + "</td><td class='mono'>" + esc(r.input_masked) + "</td><td>" +
          esc(r.status) + "</td><td>" + esc(r.reason) + "</td></tr>"; }).join("") + "</table>";
    toast("导入完成：成功 " + (s.ok||0) + "，失败 " + (s.error||0) + "，跳过 " + (s.skipped||0), (s.ok ? "ok" : "err"));
    if (s.ok) { $("import-dialog").close(); }
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "导入"; }
}

async function onApplyModel() {
  var model = $("model-select").value;
  if (!model) return;
  try {
    var result = await api("/api/model", { method: "POST", body: JSON.stringify({ model: model }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

/* 运行参数表单的「未保存」跟踪：任一字段被编辑即置脏并暂停轮询回填，
   保存成功后清脏并恢复回填。按钮上的 * 与提示文字给出可见反馈。 */
var OPT_FIELD_IDS = [
  "opt-strategy", "opt-rate-mode", "opt-qps", "opt-wait", "opt-attempts",
  "opt-fx-enabled", "opt-fx-concurrency", "opt-fx-req", "opt-fx-interval",
  "opt-fx-tokens", "opt-fx-imgsize", "opt-fx-imgcount", "opt-fx-image",
  "opt-fx-yield", "opt-fx-minmem",
  "opt-rp-enabled", "opt-rp-target", "opt-rp-interval", "opt-rp-keyword",
  "opt-rp-cap", "opt-rp-sms-interval", "opt-rp-sms-timeout", "opt-rp-keyname",
  "opt-rp-keytype", "opt-rp-sms-token"
];

function markOptionsDirty() {
  if (S.optionsDirty) return;
  S.optionsDirty = true;
  updateOptionsDirtyUi();
}

function updateOptionsDirtyUi() {
  var dirty = !!S.optionsDirty;
  ["btn-apply-options", "btn-save-replenish"].forEach(function (id) {
    var el = $(id);
    if (!el) return;
    el.classList.toggle("dirty", dirty);
    el.textContent = dirty ? "保存并应用 *" : "保存并应用";
    el.title = dirty ? "有未保存的改动，点击保存并立即生效" : "";
  });
  Array.prototype.forEach.call(document.querySelectorAll(".opt-dirty"), function (hint) {
    hint.style.display = dirty ? "" : "none";
  });
}

async function onApplyOptions() {
  var body = {
    strategy: $("opt-strategy").value,
    rate_mode: $("opt-rate-mode").value,
    qps: parseFloat($("opt-qps").value),
    max_total_wait: parseFloat($("opt-wait").value),
    max_attempts: parseInt($("opt-attempts").value, 10),
    flash_lite: {
      enabled: $("opt-fx-enabled").checked,
      image_enabled: $("opt-fx-image").checked,
      yield_to_serve: $("opt-fx-yield").checked,
      min_available_mb: parseFloat($("opt-fx-minmem").value),
      concurrency: parseInt($("opt-fx-concurrency").value, 10),
      requests_per_trigger: parseInt($("opt-fx-req").value, 10),
      min_interval_s: parseFloat($("opt-fx-interval").value),
      long_text_max_tokens: parseInt($("opt-fx-tokens").value, 10),
      image_size: parseInt($("opt-fx-imgsize").value, 10),
      multi_image_count: parseInt($("opt-fx-imgcount").value, 10)
    },
    replenish: {
      enabled: $("opt-rp-enabled").checked,
      target_count: parseInt($("opt-rp-target").value, 10),
      interval_seconds: parseFloat($("opt-rp-interval").value),
      keyword: $("opt-rp-keyword").value.trim(),
      daily_spend_cap: parseFloat($("opt-rp-cap").value),
      sms_poll_interval: parseFloat($("opt-rp-sms-interval").value),
      sms_poll_timeout: parseFloat($("opt-rp-sms-timeout").value),
      key_name: $("opt-rp-keyname").value.trim(),
      key_type: $("opt-rp-keytype").value
    }
  };
  var smsToken = $("opt-rp-sms-token").value.trim();
  if (smsToken) body.replenish.sms_token = smsToken;   // 留空 = 不修改已存密钥
  try {
    var result = await api("/api/options", { method: "POST", body: JSON.stringify(body) });
    toast(result.message, "ok");
    // 保存成功后清脏：恢复 2 秒轮询回填，并以服务端最终值刷新表单。
    S.optionsDirty = false;
    updateOptionsDirtyUi();
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

async function onTogglePause() {
  var next = !(lastState.metrics && lastState.metrics.paused);
  try {
    var result = await api("/api/pause", { method: "POST", body: JSON.stringify({ paused: next }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

function copyText(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { toast("已复制" + label, "ok"); },
      function () { toast("复制失败", "err"); });
    return;
  }
  var area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); toast("已复制" + label, "ok"); }
  catch (err) { toast("复制失败", "err"); }
  area.remove();
}

/* ------------------------------------------------------------------ 主循环 */

var lastState = {};

async function refreshState() {
  try {
    var state = await api("/api/state");
    lastState = state;
    $("authbar").classList.remove("show");
    renderKpis(state);
    renderReplenish(state);
    renderLeakGuard(state);
    renderGateway(state);
    renderModels(state);
    renderModelTest(state);
    renderPool(state);
    renderOptions(state);
    var g = state.gateway || {};
    $("subline").innerHTML = '<span class="dot ' + (state.metrics.paused ? "paused" : "on") + '"></span>' +
      (state.metrics.paused ? "已暂停接入（上层会收到 503）" : "运行中") +
      " · 监听 " + esc(g.listen) + " · " + fmtInt(state.summary.total) + " 把 Key · v" + esc(state.version);
    $("btn-pause").textContent = state.metrics.paused ? "恢复接入" : "暂停接入";
    if (state.log_file) $("logfile").textContent = "日志文件：" + state.log_file;
    else $("logfile").textContent = "未启用文件日志（仅内存缓冲）";
  } catch (err) {
    $("subline").innerHTML = '<span class="dot" style="background:var(--red)"></span>连接失败：' + esc(err.message);
  }
}

function tickCountdowns() {
  Array.prototype.forEach.call(document.querySelectorAll("[data-reset-at]"), function (node) {
    var resetAt = parseInt(node.getAttribute("data-reset-at"), 10);
    node.textContent = fmtCountdown(resetAt, node.getAttribute("data-long") === "1");
  });
}

function bind() {
  $("btn-refresh").onclick = function () { refreshState(); toast("已刷新"); };
  $("btn-refresh-quota").onclick = async function () { await fetchQuota(true); renderKpis(lastState); renderPool(lastState); await refreshUsage(); await refreshSamples(); toast("余量已刷新", "ok"); };
  $("btn-pause").onclick = onTogglePause;
  $("btn-add").onclick = onAddKeys;
  $("btn-import").onclick = function () { $("import-dialog").showModal(); };
  $("btn-export-accounts").onclick = onExportAccounts;
  $("btn-import-close").onclick = function () { $("import-dialog").close(); };
  $("btn-import-run").onclick = onImportKeys;
  $("btn-apply-model").onclick = onApplyModel;
  $("modeltest-run").onclick = runModelTest;
  $("modeltest-accounts").onclick = function (event) {
    event.stopPropagation();
    var menu = $("modeltest-accounts-menu");
    menu.style.display = menu.style.display === "block" ? "none" : "block";
  };
  $("modeltest-accounts-menu").onclick = function (event) { event.stopPropagation(); };
  $("modeltest-accounts-menu").onchange = mtUpdateAccountsLabel;
  document.addEventListener("click", function () {
    var menu = $("modeltest-accounts-menu");
    if (menu) menu.style.display = "none";
  });
  $("btn-apply-options").onclick = onApplyOptions;
  $("btn-save-replenish").onclick = onApplyOptions;
  // 任一运行参数字段被编辑 -> 置脏并暂停轮询回填，直到点击保存。
  OPT_FIELD_IDS.forEach(function (id) {
    var el = $(id);
    if (!el) return;
    el.addEventListener("input", markOptionsDirty);
    el.addEventListener("change", markOptionsDirty);
  });
  updateOptionsDirtyUi();
  $("btn-models").onclick = async function () {
    try { await api("/api/models/refresh", { method: "POST" }); await refreshState(); toast("模型清单已刷新", "ok"); }
    catch (err) { toast(err.message, "err"); }
  };
  $("btn-copy-base").onclick = function () { copyText((lastGateway.base_url || ""), "网关地址"); };
  $("model-select").onchange = updateModelMeta;
  $("new-key").addEventListener("keydown", function (event) { if (event.key === "Enter") onAddKeys(); });
  $("btn-clearlog").onclick = function () { $("logbox").innerHTML = ""; };
  $("btn-clear-replenish-log").onclick = function () { $("replenish-logbox").innerHTML = ""; };
  $("rp-reg-search").onclick = function () { S.rpRegPage = 1; renderReplenishRecords(); };
  $("rp-reg-prev").onclick = function () {
    if (S.rpRegPage > 1) { S.rpRegPage -= 1; renderReplenishRecords(); }
  };
  $("rp-reg-next").onclick = function () {
    if (!$("rp-reg-next").disabled) { S.rpRegPage += 1; renderReplenishRecords(); }
  };
  $("rp-reg-q").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { S.rpRegPage = 1; renderReplenishRecords(); }
  });
  ["pool-f-user", "pool-f-phone", "pool-f-key"].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("input", function () { S.poolPage = 1; renderPool(lastState); });
  });
  if ($("pool-f-status")) $("pool-f-status").addEventListener("change", function () { S.poolPage = 1; renderPool(lastState); });
  if ($("pool-page-size")) $("pool-page-size").addEventListener("change", function () {
    S.poolSize = parseInt($("pool-page-size").value, 10) || 20; S.poolPage = 1; renderPool(lastState);
  });
  if ($("pool-prev")) $("pool-prev").onclick = function () { if (S.poolPage > 1) { S.poolPage -= 1; renderPool(lastState); } };
  if ($("pool-next")) $("pool-next").onclick = function () { if (!$("pool-next").disabled) { S.poolPage += 1; renderPool(lastState); } };
  if ($("pool-f-reset")) $("pool-f-reset").onclick = function () {
    ["pool-f-user", "pool-f-phone", "pool-f-key"].forEach(function (id) { if ($(id)) $(id).value = ""; });
    if ($("pool-f-status")) $("pool-f-status").value = "";
    S.poolPage = 1; renderPool(lastState);
  };
  $("btn-autoscroll").onclick = function () {
    S.autoscroll = !S.autoscroll;
    this.textContent = "自动滚动：" + (S.autoscroll ? "开" : "关");
  };
  $("btn-token").onclick = async function () {
    S.token = $("token-input").value.trim();
    localStorage.setItem("st_rotator_token", S.token);
    await refreshState();
  };
  $("token-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter") $("btn-token").click();
  });
  renderSnippetTabs();
}

bind();
refreshState().then(function () { refreshQuota(false); refreshUsage(); refreshSamples(); });
renderReplenishRecords();  // 注册记录只在启动时拉一次，不做 2 秒轮询
pollLogs();
S.timerState = setInterval(refreshState, 2000);
S.timerLog = setInterval(pollLogs, 1200);
S.timerCountdown = setInterval(tickCountdowns, 1000);
  S.timerQuota = setInterval(function () { refreshQuota(false); }, 60000);
  S.timerUsage = setInterval(refreshUsage, 60000);
  S.timerSamples = setInterval(refreshSamples, 60000);
</script>
</body>
</html>
"""


# 登录页：口令不对时网关回同一个文档，只把错误条从隐藏切成可见。
# 用单份模板 + 占位符替换，避免维护两份几乎相同的 HTML 字面量。
_LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 多 Key 轮换控制台</title>
<style>
  :root {
    --bg: #12161d;
    --card: #1a1f28;
    --border: #2a3240;
    --border-strong: #3a4456;
    --text: #e6ebf2;
    --muted: #93a0b4;
    --accent: #5b93ff;
    --red: #ff8078;
    --red-bg: #3a1d1c;
    --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 13px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .login {
    width: 100%; max-width: 360px; margin: 20px;
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 26px 26px 24px;
  }
  .login h1 { font-size: 16px; margin: 0 0 4px; font-weight: 650; letter-spacing: .2px; }
  .login .sub { margin: 0 0 20px; color: var(--muted); font-size: 12px; }
  label.field { display: block; margin-bottom: 14px; }
  label.field > span { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 5px; }
  input[type=password] {
    font-family: var(--mono); font-size: 13px; color: var(--text);
    background: var(--bg); border: 1px solid var(--border-strong);
    border-radius: 7px; padding: 9px 11px; width: 100%;
  }
  input[type=password]:focus { outline: none; border-color: var(--accent); }
  button {
    font-family: inherit; font-size: 13px; cursor: pointer; width: 100%;
    border: 1px solid var(--accent); background: var(--accent); color: #fff;
    padding: 9px 13px; border-radius: 7px; transition: .14s;
  }
  button:hover { filter: brightness(1.08); }
  .error {
    margin: 0 0 16px; padding: 9px 12px; border-radius: 7px;
    background: var(--red-bg); color: var(--red); font-size: 12.5px;
  }
</style>
</head>
<body>
  <form class="login" method="POST" action="/login">
    <h1>多 Key 轮换控制台</h1>
    <p class="sub">该网关启用了本地鉴权，请输入访问 Token。</p>
    <div class="error" id="login-error" style="__ERROR_STYLE__">口令不正确，请重试</div>
    <label class="field">
      <span>访问 Token</span>
      <input type="password" name="token" class="mono" placeholder="Bearer Token" autocomplete="current-password" autofocus required>
    </label>
    <input type="hidden" name="next" id="login-next" value="">
    <button type="submit">登录</button>
  </form>
<script>
"use strict";
/* 带 #token=xxx 打开时：fragment 不会发给服务端，也不进 Referer。
   读出来填进输入框（用 .value 赋值，绝不拼接标记），抹掉地址栏 fragment，再提交。
   没有 fragment 时绝不自动提交，交给用户手输。 */
(function () {
  var nextEl = document.getElementById("login-next");
  if (nextEl) nextEl.value = location.pathname;
  var match = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
  if (!match) return;
  var input = document.querySelector('input[name="token"]');
  if (!input) return;
  try {
    input.value = decodeURIComponent(match[1]);
  } catch (err) {
    return;
  }
  history.replaceState(null, "", location.pathname + location.search);
  if (input.form) input.form.submit();
})();
</script>
</body>
</html>
"""

LOGIN_HTML = _LOGIN_TEMPLATE.replace("__ERROR_STYLE__", "display:none")
LOGIN_HTML_INVALID = _LOGIN_TEMPLATE.replace("__ERROR_STYLE__", "")
