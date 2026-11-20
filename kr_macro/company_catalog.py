"""Local company directory and lazily loaded stock pages."""

import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf


MARKETS = (
    ("KOSPI", "KS", "stk", 100),
    ("KOSDAQ", "KQ", "ksq", 50),
)
KRX_DIRECTORY_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/"
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def krx_rows(feed, date, key):
    result = requests.get(
        KRX_DIRECTORY_URL + feed, params={"basDd": date.replace("-", "")},
        headers={"AUTH_KEY": key}, timeout=20,
    )
    result.raise_for_status()
    rows = result.json().get("OutBlock_1")
    if not isinstance(rows, list) or not rows:
        raise ValueError("KRX 종목 데이터가 비어 있습니다")
    return rows


def market_members(trades, metadata, market, suffix, limit):
    common = {
        row["ISU_SRT_CD"]: row for row in metadata
        if row.get("SECUGRP_NM") == "주권" and row.get("KIND_STKCERT_TP_NM") == "보통주"
    }
    members = []
    for row in trades:
        ticker = row.get("ISU_CD", "")
        cap = str(row.get("MKTCAP", "")).replace(",", "")
        if ticker not in common or not re.fullmatch(r"[0-9A-Z]{6}", ticker) or not cap.isdecimal() or int(cap) <= 0:
            continue
        members.append({
            "id": ticker, "ticker": ticker, "name": row["ISU_NM"], "tab": row["ISU_NM"],
            "eng": common[ticker].get("ISU_ENG_NM", ""), "market": market,
            "yahoo": f"{ticker}.{suffix}", "cap": int(cap),
        })
    members.sort(key=lambda member: (-member["cap"], member["ticker"]))
    if len(members) < limit or len({member["ticker"] for member in members}) != len(members):
        raise ValueError("KRX 시가총액 순위를 확인할 수 없습니다")
    return members[:limit]


def load_directory(out, date, key):
    path = out / ".company-directory.json"
    cached = read_json(path)
    if cached.get("asof", "") >= date or not key:
        return cached
    feeds = [f"{prefix}_{kind}" for _, _, prefix, _ in MARKETS for kind in ("bydd_trd", "isu_base_info")]
    try:
        with ThreadPoolExecutor(max_workers=len(feeds)) as executor:
            market_rows = list(executor.map(lambda feed: krx_rows(feed, date, key), feeds))
        members = []
        for index, (market, suffix, _, limit) in enumerate(MARKETS):
            trades, metadata = market_rows[index * 2:index * 2 + 2]
            members += market_members(trades, metadata, market, suffix, limit)
        result = {"asof": date, "members": members}
        write_atomic(path, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return result
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
        print(f"  ! 기업 분석 종목 목록: {type(error).__name__} · 마지막 저장 목록 사용")
        return cached


def update_prices(out, directory, included):
    cache = out / ".company-prices"
    members = [entry for entry in directory.get("members", []) if entry["ticker"] not in included]
    marker = cache / ".refresh.json"
    date = directory.get("asof")
    refreshed = read_json(marker)
    if not members:
        return
    if refreshed.get("date") == date and all((cache / (entry["ticker"] + ".csv")).is_file() for entry in members):
        return
    print(f"  기업 분석: 종목별 주가 {len(members)}개 확인", flush=True)
    end = (pd.Timestamp(date) + pd.Timedelta(days=1)).date()
    for offset in range(0, len(members), 25):
        batch = members[offset:offset + 25]
        symbols = [entry["yahoo"] for entry in batch]
        try:
            prices = yf.download(symbols, start="2016-01-01", end=end, group_by="ticker",
                                 progress=False, auto_adjust=False, threads=6, timeout=12)
        except Exception as error:
            print(f"  ! 기업 분석 주가 묶음: {type(error).__name__}")
            continue
        if prices is None or prices.empty:
            continue
        for entry in batch:
            symbol = entry["yahoo"]
            if symbol not in prices.columns.get_level_values(0):
                continue
            frame = prices[symbol].reindex(columns=PRICE_COLUMNS).dropna(how="all")
            if len(frame) >= 2:
                write_atomic(cache / (entry["ticker"] + ".csv"), frame.to_csv())
    write_atomic(marker, json.dumps({"date": date}, ensure_ascii=False))


def stock_quote(stock):
    price, previous, date = stock.get("price"), stock.get("prev"), stock.get("date")
    if not price or not previous or not date:
        return None
    return {"price": price, "change": (price / previous - 1) * 100, "date": date}


def directory_rows(directory, companies, asset_folder=None, quotes=None):
    """Searchable list, including locally configured analysis pages."""
    quotes = quotes or {}
    pinned = {stock["ticker"]: stock for stock in companies}
    rows = []
    for entry in directory.get("members", []):
        existing = pinned.pop(entry["ticker"], None)
        row = {key: entry[key] for key in ("id", "ticker", "name", "tab", "market", "cap")}
        if existing:
            row.update(id=existing["id"], tab=existing["tab"], builtin=True)
        else:
            row["available"] = asset_folder is not None and (asset_folder / (entry["ticker"] + ".js")).is_file()
        quote = stock_quote(existing) if existing else quotes.get(entry["ticker"])
        if quote:
            row["quote"] = quote
        rows.append(row)
    rows += [
        {key: entry[key] for key in ("id", "ticker", "name", "tab", "market")} | {"builtin": True, "quote": stock_quote(entry)}
        for entry in pinned.values()
    ]
    return rows


def search_markup(date, count):
    detail = f'시총 상위 {count}종목 · {html.escape(date)} 기준' if date else "기업 분석 종목"
    return f"""
<div id="company-search" hidden>
  <label for="company-query">종목 검색</label>
  <input id="company-query" type="search" placeholder="하이닉스 또는 000660" autocomplete="off"
    aria-controls="company-results" aria-describedby="company-search-count">
  <p id="company-search-count" class="company-search-meta">{detail}</p>
  <ul id="company-results" aria-label="종목 검색 결과" hidden></ul>
  <p class="company-search-status" role="status"></p>
  <section class="company-recent-section" aria-labelledby="company-recent-title">
    <h3 id="company-recent-title">최근 본 종목</h3>
    <ul id="company-recent" hidden></ul>
    <p id="company-recent-empty">검색해서 연 종목이 여기에 표시됩니다.</p>
  </section>
</div>"""


CSS = """
#company-search{margin:0 0 11px}
#company-search label{display:block;color:var(--text-2);font-size:12px;font-weight:650;margin-bottom:5px}
#company-query{min-width:0;width:100%;padding:8px 9px;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:8px}
#company-query:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.company-search-meta,.company-search-status{margin:5px 0 0;color:var(--text-3);font-size:11.5px}
.company-search-status:empty{display:none}
#company-results{list-style:none;margin:7px 0 0;padding:0;max-height:236px;overflow:auto;scrollbar-width:thin;border:1px solid var(--border);border-radius:8px;background:var(--bg)}
#company-results li+li{border-top:1px solid var(--border)}
#company-results button{display:grid;gap:2px;width:100%;padding:7px 8px;color:var(--text);text-align:left;font:inherit;border:0;background:transparent;cursor:pointer;overflow-wrap:anywhere}
#company-results button:hover,#company-results button:focus-visible{background:var(--surface-2)}
#company-results button:focus-visible{outline-offset:-2px}
#company-results small{color:var(--text-3);font-variant-numeric:tabular-nums}
.dataset-list button:is([data-group="companies"],[data-group="reports"]){display:none}
.company-recent-section{margin:13px 0 0;padding-top:10px;border-top:1px solid var(--border)}
.company-recent-section h3{margin:0 0 4px;color:var(--text-2);font-size:12px;font-weight:650}
#company-recent-empty{margin:5px 0 0;color:var(--text-3);font-size:11.5px}
#company-recent{list-style:none;margin:0;padding:0;max-height:340px;overflow:auto;scrollbar-width:thin}
#company-recent li+li{border-top:1px solid var(--border)}
#company-recent button{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:7px;width:100%;padding:8px 6px;text-align:left;color:var(--text);font:inherit;border:0;border-radius:5px;background:transparent;cursor:pointer}
#company-recent button:hover,#company-recent button[aria-current=page]{background:var(--surface-2)}
#company-recent button:focus-visible{outline-offset:-2px}
.company-recent-identity,.company-recent-quote{min-width:0;display:grid;line-height:1.45}
.company-recent-identity strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.company-recent-quote{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
#company-recent small{font-size:11px;color:var(--text-3)}
#company-recent [data-direction=up]{color:var(--bad)}
#company-recent [data-direction=down]{color:var(--accent)}
.company-load-message{margin:0;color:var(--text-2)}
"""

JS = r"""
(() => {
  const search = document.getElementById('company-search');
  const query = document.getElementById('company-query');
  const results = document.getElementById('company-results');
  const status = search.querySelector('[role=status]');
  const select = document.querySelector('.dataset-list [data-t=s-catalog]');
  const pane = document.getElementById('s-catalog');
  const reportSelect = document.querySelector('.dataset-list [data-t=r-catalog]');
  const reportPane = document.getElementById('r-catalog');
  const catalog = JSON.parse(document.getElementById('company-directory').textContent);
  const stocksByCode = new Map(catalog.map(stock => [stock.ticker, stock]));
  const pending = new Map();
  let ready = null;
  let request = 0;
  let reportRequest = 0;
  let reportReady = null;
  let searchGroup = 'companies';

  const recentList = document.getElementById('company-recent');
  const recentEmpty = document.getElementById('company-recent-empty');
  const storageKeys = {companies: 'kr-macro-recent-stocks-v1', reports: 'kr-macro-recent-reports-v1'};
  let storageKey = storageKeys.companies;
  const recentLimit = 10;
  const won = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 0});
  const returns = new Intl.NumberFormat('ko-KR', {
    style: 'percent', signDisplay: 'exceptZero', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  let recentCodes = [];

  function savedRecent() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '[]');
      if (Array.isArray(value)) {
        return [...new Set(value.filter(code => typeof code === 'string' && /^[0-9A-Z]{6}$/.test(code)))].slice(0, recentLimit);
      }
    } catch {}
    return recentCodes;
  }

  function renderRecent() {
    const focusTicker = recentList.contains(document.activeElement) ? document.activeElement.dataset.ticker : null;
    const activePrefix = searchGroup === 'reports' ? 'r-' : 's-';
    const activeTicker = dashboardSettings.get('tab')?.startsWith(activePrefix) ? dashboardSettings.get('stock') : null;
    const entries = recentCodes.map(code => stocksByCode.get(code)).filter(Boolean);
    recentList.replaceChildren();
    for (const stock of entries) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.ticker = stock.ticker;
      if (stock.ticker === activeTicker) button.setAttribute('aria-current', 'page');
      const identity = document.createElement('span');
      identity.className = 'company-recent-identity';
      const name = document.createElement('strong');
      name.textContent = stock.name;
      const code = document.createElement('small');
      code.textContent = stock.ticker;
      identity.append(name, code);
      button.append(identity);
      if (stock.quote) {
        const values = document.createElement('span');
        values.className = 'company-recent-quote';
        values.title = stock.quote.date + ' 종가';
        const price = document.createElement('strong');
        const change = document.createElement('small');
        const rate = Math.round(stock.quote.change * 100) / 100;
        price.textContent = won.format(stock.quote.price);
        change.textContent = returns.format(rate / 100);
        change.dataset.direction = rate > 0 ? 'up' : rate < 0 ? 'down' : 'flat';
        values.append(price, change);
        button.append(values);
      }
      item.append(button);
      recentList.append(item);
      if (focusTicker === stock.ticker) button.focus({preventScroll: true});
    }
    recentEmpty.hidden = entries.length > 0;
    recentList.hidden = entries.length === 0;
  }

  function rememberCompany(ticker) {
    recentCodes = [ticker, ...savedRecent().filter(code => code !== ticker)].slice(0, recentLimit);
    try { localStorage.setItem(storageKey, JSON.stringify(recentCodes)); } catch {}
    renderRecent();
  }

  recentList.addEventListener('click', event => {
    const button = event.target.closest('button[data-ticker]');
    if (button) openSearchResult(button.dataset.ticker);
  });
  window.addEventListener('storage', event => {
    if (event.key !== storageKey && event.key !== null) return;
    recentCodes = savedRecent();
    renderRecent();
  });

  const normalize = value => String(value).normalize('NFKC').toLowerCase().replace(/[\s·._-]+/g, '');
  const searchKeys = catalog.map(stock => ({
    stock, fields: [stock.name, stock.tab, stock.ticker, stock.market, stock.market === 'KOSPI' ? '코스피' : '코스닥'].map(normalize),
  }));

  function findStocks(value) {
    const terms = value.trim().split(/\s+/u).map(normalize).filter(Boolean);
    if (!terms.length) return [];
    return searchKeys.filter(item => terms.every(term => item.fields.some(field => field.includes(term))))
      .map(item => item.stock)
      .sort((left, right) => Number(terms.includes(normalize(right.ticker))) - Number(terms.includes(normalize(left.ticker))));
  }

  function renderResults() {
    const matches = findStocks(query.value);
    results.replaceChildren();
    for (const stock of matches) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      const name = document.createElement('strong');
      const info = document.createElement('small');
      button.type = 'button';
      button.dataset.ticker = stock.ticker;
      name.textContent = stock.name;
      info.textContent = stock.ticker + ' · ' + stock.market;
      button.append(name, info);
      item.append(button);
      results.append(item);
    }
    const hasQuery = Boolean(query.value.trim());
    results.hidden = !hasQuery || matches.length === 0;
    status.textContent = hasQuery ? (matches.length ? matches.length + '개 검색 결과' : '검색 결과가 없습니다.') : '';
  }

  window.dashboardCompanyPayload = (ticker, payload) => {
    const job = pending.get('company-data/' + ticker);
    if (job && payload?.company?.id === ticker && Array.isArray(payload.company.ohlcv) && typeof payload.html === 'string') {
      job.deliver(payload);
    }
  };
  window.dashboardReportPayload = (ticker, payload) => {
    if (typeof payload?.html === 'string') pending.get('report-data/' + ticker)?.deliver(payload);
  };

  function loadCompanyAsset(directory, ticker) {
    const key = directory + '/' + ticker;
    if (pending.has(key)) return pending.get(key).result;
    const script = document.createElement('script');
    script.src = './' + directory + '/' + encodeURIComponent(ticker) + '.js';
    let payload;
    const result = new Promise((resolve, reject) => {
      const settle = (callback, value) => {
        clearTimeout(timer);
        pending.delete(key);
        script.remove();
        callback(value);
      };
      const fail = () => settle(reject, new Error('종목 파일을 읽을 수 없습니다'));
      const timer = setTimeout(fail, 10000);
      script.onload = () => payload ? settle(resolve, payload) : fail();
      script.onerror = fail;
    });
    pending.set(key, {result, deliver: value => { payload = value; }});
    document.head.append(script);
    return result;
  }

  function saveView(ticker, view) {
    dashboardSettings.set('stock', ticker);
    dashboardSettings.set('tab', view);
    try { history.replaceState(history.state, '', '#' + dashboardSettings.toString()); } catch {}
  }

  function removePreviousChart() {
    if (!ready) return;
    const state = TV[ready];
    if (state) {
      clearInterval(state.clockTimer);
      state.chart.remove();
      if (state.rsiChart) state.rsiChart.remove();
      delete TV[ready];
    }
    const i = STOCKS.findIndex(stock => stock.id === ready);
    if (i !== -1) STOCKS.splice(i, 1);
    ready = null;
  }

  function showMessage(message, container = pane) {
    const note = document.createElement('p');
    note.className = 'card company-load-message';
    note.setAttribute('role', 'status');
    note.textContent = message;
    container.replaceChildren(note);
  }

  function drawSelected() {
    if (ready && pane.classList.contains('on')) drawStock(ready);
  }

  async function openCompany(ticker) {
    const stock = stocksByCode.get(ticker);
    if (!stock) return false;
    const view = 's-' + stock.id;
    if (stock.builtin) {
      openWorkspaceView(view);
      saveView(ticker, view);
      rememberCompany(ticker);
      return true;
    }
    const loadId = ++request;
    select.dataset.ticker = ticker;
    select.querySelector('span').textContent = stock.name;
    select.querySelector('small').textContent = ticker + ' · ' + stock.market;
    openWorkspaceView('s-catalog');
    if (ready === ticker) {
      rememberCompany(ticker);
      return true;
    }
    removePreviousChart();
    if (!stock.available) {
      showMessage(stock.name + '의 저장된 주가를 아직 불러올 수 없습니다.');
      return false;
    }
    showMessage(stock.name + ' 주가 불러오는 중…');
    try {
      const payload = await loadCompanyAsset('company-data', ticker);
      if (loadId !== request) return false;
      STOCKS.push(payload.company);
      const content = document.createElement('div');
      content.id = view;
      content.innerHTML = payload.html;
      pane.replaceChildren(content);
      ready = ticker;
      drawSelected();
      if (pane.classList.contains('on')) rememberCompany(ticker);
      return true;
    } catch {
      if (loadId === request) showMessage(stock.name + ' 주가 파일을 읽을 수 없습니다. 종목을 다시 선택해 주세요.');
      return false;
    }
  }

  async function openReport(ticker) {
    const stock = stocksByCode.get(ticker);
    if (!stock) return false;
    if (stock.builtin) {
      const view = 'r-' + stock.id;
      openWorkspaceView(view);
      saveView(ticker, view);
      rememberCompany(ticker);
      return true;
    }
    const loadId = ++reportRequest;
    reportSelect.dataset.ticker = ticker;
    reportSelect.querySelector('span').textContent = stock.name;
    reportSelect.querySelector('small').textContent = ticker + ' · ' + stock.market;
    openWorkspaceView('r-catalog');
    if (reportReady === ticker) {
      rememberCompany(ticker);
      return true;
    }
    reportReady = null;
    showMessage(stock.name + ' 리포트 불러오는 중…', reportPane);
    try {
      const payload = await loadCompanyAsset('report-data', ticker);
      if (loadId !== reportRequest) return false;
      reportPane.innerHTML = payload.html;
      reportReady = ticker;
      if (reportPane.classList.contains('on')) rememberCompany(ticker);
      return true;
    } catch {
      if (loadId === reportRequest) {
        showMessage(stock.name + ' 리포트 파일을 읽을 수 없습니다. 종목을 다시 선택해 주세요.', reportPane);
      }
      return false;
    }
  }

  const openSearchResult = ticker => searchGroup === 'reports' ? openReport(ticker) : openCompany(ticker);

  query.addEventListener('input', renderResults);
  query.addEventListener('keydown', event => {
    if (event.isComposing) return;
    const first = results.querySelector('button');
    if (!first || results.hidden) return;
    if (event.key === 'Enter' || event.key === 'ArrowDown') {
      event.preventDefault();
      if (event.key === 'Enter') first.click();
      else first.focus();
    }
  });
  results.addEventListener('click', event => {
    const button = event.target.closest('button[data-ticker]');
    if (button) {
      const ticker = button.dataset.ticker;
      query.value = '';
      renderResults();
      query.focus({preventScroll: true});
      openSearchResult(ticker);
    }
  });
  results.addEventListener('keydown', event => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const active = event.target.closest('button');
    if (!active) return;
    event.preventDefault();
    const item = event.key === 'ArrowDown' ? active.parentElement.nextElementSibling : active.parentElement.previousElementSibling;
    (item?.querySelector('button') || query).focus();
  });

  select.addEventListener('click', () => {
    if (select.dataset.ticker) saveView(select.dataset.ticker, 's-catalog');
    drawSelected();
    renderRecent();
  });
  reportSelect.addEventListener('click', () => {
    if (reportSelect.dataset.ticker) saveView(reportSelect.dataset.ticker, 'r-catalog');
    renderRecent();
  });
  for (const button of datasetButtons.filter(item => item !== select && item !== reportSelect)) {
    button.addEventListener('click', () => {
      if (!dashboardSettings.has('stock')) return;
      dashboardSettings.delete('stock');
      dashboardSettings.set('tab', button.dataset.t);
      try { history.replaceState(history.state, '', '#' + dashboardSettings.toString()); } catch {}
      renderRecent();
    });
  }
  window.openCompanyAnalysis = openCompany;
  window.setCompanyDirectoryGroup = group => {
    if (!storageKeys[group] || searchGroup === group) return;
    searchGroup = group;
    storageKey = storageKeys[group];
    recentCodes = [];
    recentCodes = savedRecent();
    query.value = '';
    renderResults();
    renderRecent();
  };
  recentCodes = savedRecent();
  renderRecent();
  const savedView = dashboardSettings.get('tab');
  const restored = dashboardSettings.get('stock') ||
    catalog.find(stock => stock.builtin && ['s-' + stock.id, 'r-' + stock.id].includes(savedView))?.ticker;
  if (restored) (savedView?.startsWith('r-') ? openReport : openCompany)(restored);
})();
"""


def script(rows):
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", r"<\/")
    return f'<script type="application/json" id="company-directory">{data}</script>\n<script>{JS}</script>'
