"""KODEX domestic sector ETF quotes and market-cap treemap."""

from concurrent.futures import ThreadPoolExecutor
from html import escape
import json
import os
import re

import requests

from company_catalog import read_json, write_atomic


SECTORS = {
    "091160": "반도체",
    "305720": "2차전지",
    "091180": "자동차",
    "0115D0": "조선",
    "0080G0": "방산",
    "102960": "기계장비",
    "266420": "헬스케어",
    "117460": "에너지·화학",
    "117680": "철강",
    "117700": "건설",
    "091170": "은행",
    "102970": "증권",
    "140700": "보험",
    "266410": "필수소비재",
    "266390": "경기소비재",
    "266360": "콘텐츠",
    "300950": "게임",
    "140710": "운송",
}
CACHE_FILE = ".kodex-sector-heatmap.json"


def etf_holdings(fetch, day, isin):
    try:
        holdings = []
        for row in fetch(day, isin).to_dict("records"):
            ticker = str(row["COMPST_ISU_CD"]).strip()
            if not re.fullmatch(r"[0-9A-Z]{6}", ticker):
                continue
            weight = str(row["COMPST_RTO"]).replace(",", "").strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", weight):
                continue
            holdings.append({
                "ticker": ticker, "name": str(row["COMPST_ISU_NM"]), "weight": float(weight),
            })
        return sorted(holdings, key=lambda holding: -holding["weight"])
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
        return []


def load_snapshot(out, asof, env):
    path = out / CACHE_FILE
    cached = read_json(path)
    if cached.get("asof", "") >= asof or not all(env.get(key) for key in ("KRX_ID", "KRX_PW")):
        return cached

    for key in ("KRX_ID", "KRX_PW"):
        os.environ[key] = env[key]
    try:
        from pykrx.website.krx.etx.core import PDF, 전종목시세_ETF

        day = asof.replace("-", "")
        quotes = 전종목시세_ETF().fetch(day)
        if len(quotes) < len(SECTORS):
            raise ValueError("KRX ETF 시세가 비어 있거나 불완전합니다")

        rows, isins = [], []
        for quote in quotes.to_dict("records"):
            ticker = quote["ISU_SRT_CD"]
            if ticker not in SECTORS or not quote["ISU_ABBRV"].startswith("KODEX "):
                continue
            cap = str(quote["MKTCAP"]).replace(",", "").strip()
            close = str(quote["TDD_CLSPRC"]).replace(",", "").strip()
            change = str(quote["FLUC_RT"]).replace(",", "").strip()
            if not (cap.isdecimal() and close.isdecimal() and int(cap) > 0 and int(close) > 0):
                continue
            if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", change):
                continue
            rows.append({
                "ticker": ticker, "sector": SECTORS[ticker], "name": quote["ISU_ABBRV"],
                "cap": int(cap), "close": int(close), "change": float(change),
            })
            isins.append(quote["ISU_CD"])

        if not rows:
            raise ValueError("KODEX 업종 ETF 시세를 확인할 수 없습니다")
        with ThreadPoolExecutor(max_workers=3) as executor:
            holdings = executor.map(lambda isin: etf_holdings(PDF().fetch, day, isin), isins)
            for row, members in zip(rows, holdings):
                row["holdings"] = members

        result = {
            "asof": asof,
            "rows": sorted(rows, key=lambda row: -row["cap"]),
            "missing": list(SECTORS.keys() - {row["ticker"] for row in rows}),
        }
        write_atomic(path, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return result
    except (requests.RequestException, ImportError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"  ! 섹터 히트맵 · {type(error).__name__} · 마지막 저장 시세 사용")
        return cached


CSS = """
.workspace-grid:has(#sector-heatmap.on){grid-template-columns:minmax(0,1fr)}
.workspace-grid:has(#sector-heatmap.on)>.dataset-pane{display:none}
.sector-map-header,.sector-map-selector,.sector-map-legend,.sector-map-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sector-map-header,.sector-map-heading{justify-content:space-between}
.sector-map-header p,.sector-map-heading p{margin:0;color:var(--text-3)}
.sector-map-selector select{max-width:100%;padding:6px 9px;color:var(--text);background:var(--surface-2);
  border:1px solid var(--border);border-radius:7px;font:inherit}
.sector-map-legend{margin:12px 0;color:var(--text-3);font-size:12px}
.sector-map-legend>span{display:inline-flex;gap:5px;align-items:center}
.sector-map-legend i{width:16px;height:9px;border-radius:2px}
#sector-map-chart{width:100%;height:clamp(340px,49vw,560px)}
#sector-map-detail h2,#sector-map-detail h3{margin:0;color:var(--text)}
#sector-map-detail h2{font-size:19px}
#sector-map-detail h3{font-size:14px}
#sector-map-detail dl{display:flex;flex-wrap:wrap;column-gap:clamp(18px,4vw,50px);row-gap:12px;margin:14px 0 20px}
#sector-map-detail dt{color:var(--text-3);font-size:12px}
#sector-map-detail dd{margin:3px 0 0;font-size:17px;font-weight:700}
.sector-map-name{font:inherit;padding:0;border:0;background:none;color:var(--accent);cursor:pointer;text-align:left}
.sector-map-name:hover{text-decoration:underline}
.sector-map-holdings{max-height:310px;overflow:auto;margin-top:8px}
.sector-map-holdings table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.sector-map-holdings :is(th,td){padding:8px 4px;border-bottom:1px solid var(--border);text-align:left}
.sector-map-holdings :is(th,td):last-child{text-align:right}
.sector-map-holdings small{color:var(--text-3);margin-inline-start:6px}
#sector-map-detail .sector-up{color:var(--bad)}
#sector-map-detail .sector-down{color:var(--series-1)}
"""


def flow_etf_panels(snapshot):
    rows = snapshot.get("rows", [])
    if not rows:
        return '<div class="tab" id="investor-flow"><p class="card">조회할 수 있는 섹터 ETF가 없습니다.</p></div>'

    asof = escape(snapshot["asof"])
    panels = []
    for row in rows:
        ticker = escape(row["ticker"], quote=True)
        sector = escape(row["sector"])
        change = row["change"]
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        panels.append(
            f'<div class="tab flow-etf" id="flow-{ticker}"><section class="card">'
            f'<p class="flow-etf-date">{ticker} · {sector} · <time datetime="{asof}">{asof}</time> KRX 기준</p>'
            '<dl class="flow-etf-facts">'
            f'<div><dt>종가</dt><dd>{row["close"]:,}원</dd></div>'
            f'<div><dt>전일 대비</dt><dd data-change="{direction}">{change:+.2f}%</dd></div>'
            f'<div><dt>시가총액</dt><dd>{row["cap"] / 1e8:,.0f}억 원</dd></div>'
            '</dl></section></div>'
        )
    return "".join(panels)


def markup(snapshot):
    tab = '<div class="tab" id="sector-heatmap">'
    rows = snapshot.get("rows", [])
    if not rows:
        return tab + '<p class="card">조회할 수 있는 KODEX ETF 시세가 아직 없습니다.</p></div>'

    date = escape(snapshot["asof"])
    options = "".join(
        f'<option value="{escape(row["ticker"])}">{escape(row["sector"])}</option>' for row in rows
    )
    unavailable = ", ".join(SECTORS[ticker] for ticker in sorted(snapshot.get("missing", [])) if ticker in SECTORS)
    note = f'<p class="note">이 날짜에 시세가 없는 섹터 {escape(unavailable)}</p>' if unavailable else ""
    return tab + f"""
<section class="card">
  <div class="sector-map-header">
    <p>국내 섹터 KODEX ETF {len(rows)}개 · KRX 종가 {date}</p>
    <label class="sector-map-selector">섹터 <select id="sector-map-choice">{options}</select></label>
  </div>
  <div class="sector-map-legend" aria-label="히트맵 범례">
    <span>면적 · ETF 시가총액</span>
    <span>색 · 전 거래일 대비</span>
    <span><i style="background:#16629c"></i>하락</span>
    <span><i style="background:#41454f"></i>보합</span>
    <span><i style="background:#a63a42"></i>상승</span>
  </div>
  <div id="sector-map-chart" class="xchart" aria-label="KODEX ETF 섹터 히트맵"></div>
  {note}
</section>
<section class="card" id="sector-map-detail" aria-live="polite">
  <div class="sector-map-heading"><h2 id="sector-map-sector"></h2><p id="sector-map-etf"></p></div>
  <dl>
    <div><dt>시가총액</dt><dd id="sector-map-cap"></dd></div>
    <div><dt>종가</dt><dd id="sector-map-close"></dd></div>
    <div><dt>전 거래일 대비</dt><dd id="sector-map-change"></dd></div>
  </dl>
  <h3>ETF 편입 종목</h3>
  <p class="note" id="sector-map-holdings-note"></p>
  <div class="sector-map-holdings" id="sector-map-holdings">
    <table aria-label="ETF 편입 종목"><thead><tr><th scope="col">종목</th><th scope="col">편입 비중</th></tr></thead>
    <tbody id="sector-map-members"></tbody></table>
  </div>
</section>
</div>"""


JS = r"""
const drawSectorHeatmap = (() => {
  const data = document.getElementById('sector-map-data');
  const host = document.getElementById('sector-map-chart');
  if (!data || !host) return () => {};

  const sectors = JSON.parse(data.textContent);
  const byTicker = new Map(sectors.map(row => [row.ticker, row]));
  const byId = id => document.getElementById('sector-map-' + id);
  const choice = byId('choice');
  const directory = document.getElementById('company-directory');
  const companies = new Set(directory ? JSON.parse(directory.textContent).map(row => row.ticker) : []);
  const number = new Intl.NumberFormat('ko-KR');
  const amount = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 2});
  const capText = cap => cap >= 1e12 ? amount.format(cap / 1e12) + '조 원' : amount.format(cap / 1e8) + '억 원';
  const pct = change => (change > 0 ? '+' : '') + change.toFixed(2) + '%';
  const color = change => {
    if (change === 0) return '#41454f';
    const strength = Math.min(Math.abs(change) / 5, 1);
    const pale = change > 0 ? [91, 47, 53] : [35, 57, 83];
    const strong = change > 0 ? [183, 50, 59] : [24, 103, 163];
    return 'rgb(' + pale.map((tone, i) => Math.round(tone + (strong[i] - tone) * strength)).join(',') + ')';
  };
  let chart;
  let initialized = false;

  function showSector(row) {
    choice.value = row.ticker;
    byId('sector').textContent = row.sector;
    byId('etf').textContent = row.name + ' · ' + row.ticker;
    const cap = byId('cap');
    cap.textContent = capText(row.cap);
    cap.title = number.format(row.cap) + '원';
    byId('close').textContent = number.format(row.close) + '원';
    const change = byId('change');
    change.textContent = pct(row.change);
    change.className = row.change > 0 ? 'sector-up' : row.change < 0 ? 'sector-down' : '';
    const members = byId('members');
    members.replaceChildren();
    byId('holdings').hidden = row.holdings.length === 0;
    byId('holdings-note').textContent = row.holdings.length
      ? '기업 분석에서 제공하는 종목은 이름을 눌러 열 수 있습니다.'
      : '이 날짜의 ETF 편입 종목을 불러오지 못했습니다.';

    for (const member of row.holdings) {
      const line = members.insertRow();
      const label = line.insertCell();
      if (companies.has(member.ticker)) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'sector-map-name';
        button.textContent = member.name;
        button.addEventListener('click', () => window.openCompanyAnalysis?.(member.ticker));
        label.append(button);
      } else {
        label.textContent = member.name;
      }
      const ticker = document.createElement('small');
      ticker.textContent = member.ticker;
      label.append(ticker);
      line.insertCell().textContent = member.weight.toFixed(2) + '%';
    }
  }

  choice.addEventListener('change', () => showSector(byTicker.get(choice.value)));
  return () => {
    if (!initialized) {
      showSector(sectors[0]);
      initialized = true;
    }
    if (typeof echarts === 'undefined') {
      chartUnavailable(host.id);
      host.style.height = 'auto';
      return;
    }
    if (chart) {
      chart.resize();
      return;
    }

    const border = getComputedStyle(host).getPropertyValue('--surface').trim();
    chart = echarts.init(host);
    chart.setOption({
      animation: false,
      tooltip: {
        confine: true,
        renderMode: 'richText',
        formatter: ({data}) => {
          const row = byTicker.get(data?.id);
          if (!row) return '';
          return row.sector + ' · ' + row.name + '\n시가총액 ' + number.format(row.cap)
            + '원\n전 거래일 대비 ' + pct(row.change);
        },
      },
      series: [{
        type: 'treemap', left: 0, right: 0, top: 0, bottom: 0, width: '100%', height: '100%',
        sort: 'desc', visibleMin: 0, roam: false, nodeClick: false, breadcrumb: {show: false},
        itemStyle: {gapWidth: 2, borderWidth: 0, borderColor: border},
        emphasis: {itemStyle: {borderColor: '#ffffff', borderWidth: 2}},
        label: {
          show: true, position: 'inside', align: 'center', verticalAlign: 'middle',
          color: '#ffffff', overflow: 'truncate', fontSize: 13, lineHeight: 19,
          formatter: ({data}) => data?.change != null ? data.name + '\n' + pct(data.change) : '',
        },
        data: sectors.map(row => ({
          id: row.ticker, name: row.sector, value: row.cap, change: row.change, itemStyle: {color: color(row.change)},
        })),
      }],
    });
    chart.on('click', params => {
      const row = byTicker.get(params.data?.id);
      if (row) showSector(row);
    });
    window.addEventListener('resize', () => {
      if (host.clientWidth && host.clientHeight) chart.resize();
    });
  };
})();
"""


def script(snapshot):
    data = json.dumps(snapshot.get("rows", []), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="sector-map-data">{data}</script>\n<script>{JS}</script>'
