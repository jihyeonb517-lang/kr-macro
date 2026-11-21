"""KRX investor net purchases for the dashboard’s stock universe."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from html import escape
import io
import json
import os

import pandas as pd
from requests import RequestException

from company_catalog import read_json, write_atomic


CACHE = ".stock-flow-map.json"
MONTHS = (1, 3, 6, 12)
NEUTRAL_TENTHS = 3
INVESTORS = ("외국인합계", "기관합계")


def summarize(directory, trading_dates, histories):
    asof = pd.Timestamp(directory["asof"])
    dates = pd.DatetimeIndex(trading_dates).normalize().unique().sort_values()
    dates = dates[dates <= asof]
    if len(dates) < 5 or dates[-1] != asof:
        return {}
    periods = {str(month): dates[dates > asof - pd.DateOffset(months=month)] for month in MONTHS}
    periods["recent"] = dates[-5:]
    rows = []
    for member in directory["members"]:
        history = histories.get(member["ticker"])
        sums = {}
        if history is not None and not history.empty:
            history = history[list(INVESTORS)].apply(pd.to_numeric, errors="coerce")
            history.index = pd.DatetimeIndex(history.index).normalize()
            history = history[~history.index.duplicated(keep="last")]
            for period, expected in periods.items():
                totals = history.reindex(expected).sum(min_count=len(expected))
                if totals.notna().all():
                    sums[period] = [int(totals[investor]) for investor in INVESTORS]
        rows.append({**{key: member[key] for key in ("ticker", "name", "market", "cap")}, "sums": sums})
    return {"asof": directory["asof"], "rows": rows}


def load_snapshot(out, directory, trading_dates, env):
    path = out / CACHE
    cached = read_json(path)
    if not directory.get("members") or not all(env.get(key) for key in ("KRX_ID", "KRX_PW")):
        return cached
    if cached.get("asof", "") >= directory["asof"]:
        return cached

    for key in ("KRX_ID", "KRX_PW"):
        os.environ[key] = env[key]
    asof = pd.Timestamp(directory["asof"])
    start = (asof - pd.DateOffset(months=13)).strftime("%Y%m%d")
    stop = asof.strftime("%Y%m%d")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            from pykrx import stock

            def fetch(ticker):
                try:
                    history = stock.get_market_trading_value_by_date(start, stop, ticker)
                    return history if set(INVESTORS).issubset(history.columns) else None
                except (RequestException, ValueError, TypeError, KeyError, IndexError):
                    return None

            tickers = [row["ticker"] for row in directory["members"]]
            histories = {tickers[0]: fetch(tickers[0])}
            with ThreadPoolExecutor(max_workers=4) as executor:
                histories.update(zip(tickers[1:], executor.map(fetch, tickers[1:]), strict=True))
        snapshot = summarize(directory, trading_dates, histories)
        if any(row["sums"].get("recent") is not None for row in snapshot.get("rows", [])):
            write_atomic(path, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
            return snapshot
    except (ImportError, OSError, RequestException, ValueError, TypeError, KeyError) as error:
        print(f"  ! 주식 수급 맵 · {type(error).__name__} · 마지막 저장 자료 확인")
    return cached


CSS = """
#stock-flow-card{--flow-red:#f5666c;--flow-gray:#a8aebb;--flow-blue:#609ffd}
:root[data-theme="light"] #stock-flow-card{--flow-red:#c33148;--flow-gray:#777e89;--flow-blue:#286ad5}
.stock-flow-controls,.stock-flow-options,.stock-flow-key{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.stock-flow-controls{justify-content:space-between;gap:10px;margin:12px 0}
.stock-flow-controls button,.stock-flow-controls select{font:inherit;color:var(--text-2);background:var(--bg);
  border:1px solid var(--border);border-radius:8px;padding:7px 9px;cursor:pointer}
.stock-flow-controls button[aria-pressed="true"]{color:var(--text);background:var(--surface-2);border-color:var(--accent)}
#stock-flow-chart{width:100%;height:clamp(340px,43vw,520px)}
.stock-flow-key{gap:6px 16px;margin:8px 0;color:var(--text-2);font-size:12px}
.stock-flow-key i{display:inline-block;width:9px;height:9px;margin-right:5px;border-radius:50%}
.stock-flow-meta,.stock-flow-note{margin:0;color:var(--text-2);font-size:12px}
.stock-flow-rows{margin-top:12px;min-width:0}
.stock-flow-rows summary{cursor:pointer}
.stock-flow-table-scroll{margin-top:8px;max-width:100%;max-height:410px;overflow:auto}
.stock-flow-rows table{width:100%;min-width:650px;border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums}
.stock-flow-rows th,.stock-flow-rows td{padding:8px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
.stock-flow-rows th{position:sticky;top:0;background:var(--surface-2);font-size:12px;color:var(--text-2)}
.stock-flow-rows :is(th,td):first-child{text-align:left}
.stock-flow-rows tbody tr:hover{background:var(--surface-2)}
.stock-flow-rows [data-flow-tone="red"]{color:var(--flow-red)}
.stock-flow-rows [data-flow-tone="gray"]{color:var(--text-2)}
.stock-flow-rows [data-flow-tone="blue"]{color:var(--flow-blue)}
.stock-flow-tag{display:inline-block;border-radius:6px;padding:2px 6px;font-size:11px;
  background:color-mix(in srgb,currentColor 12%,var(--surface))}
"""


def markup(snapshot):
    tab = '<div class="tab" id="stock-flow-all">'
    if not snapshot.get("rows"):
        return tab + '<p id="stock-flow-card" class="card">조회할 수 있는 종목별 순매수 자료가 없습니다.</p></div>'
    periods = "".join(
        f'<button type="button" data-stock-period="{month}" aria-pressed="{str(month == 1).lower()}">{month}개월</button>'
        for month in MONTHS
    )
    asof = escape(snapshot["asof"])
    neutral = f"{NEUTRAL_TENTHS / 10:g}%"
    return tab + f"""
<section id="stock-flow-card" class="card">
  <p class="stock-flow-meta">KRX {asof} 기준 · <span id="stock-flow-count"></span></p>
  <div class="stock-flow-controls">
    <div class="stock-flow-options" role="group" aria-label="누적 기간">{periods}</div>
    <div class="stock-flow-options" role="group" aria-label="투자자">
      <button type="button" data-stock-investor="combined" aria-pressed="true">외국인+기관</button>
      <button type="button" data-stock-investor="foreign" aria-pressed="false">외국인</button>
      <button type="button" data-stock-investor="institution" aria-pressed="false">기관</button>
    </div>
    <select id="stock-flow-order" aria-label="종목 목록 정렬">
      <option value="long">누적 순매수 높은 순</option>
      <option value="recent">5거래일 순매수 높은 순</option>
    </select>
  </div>
  <div id="stock-flow-chart" role="img" aria-label="누적 순매수와 최근 5거래일 순매수 산점도"></div>
  <div class="stock-flow-key" aria-label="선택 기간 누적 순매수 기준">
    <span><i style="background:var(--flow-red)"></i>+{neutral} 초과</span>
    <span><i style="background:var(--flow-gray)"></i>−{neutral} ~ +{neutral}</span>
    <span><i style="background:var(--flow-blue)"></i>−{neutral} 미만</span>
  </div>
  <p class="stock-flow-note">두 축 모두 순매수 대금을 시가총액으로 나눈 값입니다. 점 색과 표의 구분은 선택 기간의 누적 비율을 기준으로 표시합니다.</p>
  <details class="stock-flow-rows" open>
    <summary>종목별 순매수</summary>
    <div class="stock-flow-table-scroll" tabindex="0" role="region" aria-label="종목별 순매수 표">
      <table><thead><tr>
        <th scope="col">종목</th><th scope="col">시가총액 (억 원)</th>
        <th scope="col" id="stock-flow-long-amount">누적 순매수 (억 원)</th>
        <th scope="col" id="stock-flow-long">누적 ÷ 시총</th>
        <th scope="col">5거래일 순매수 (억 원)</th><th scope="col">5거래일 ÷ 시총</th><th scope="col" id="stock-flow-verdict-heading">구분</th>
      </tr></thead><tbody id="stock-flow-rows"></tbody></table>
    </div>
  </details>
</section></div>"""


SCRIPT = r"""
const STOCK_FLOW_NEUTRAL_TENTHS = __NEUTRAL_TENTHS__;
const stockFlowNeutralPercent = STOCK_FLOW_NEUTRAL_TENTHS / 10;
function stockFlowTone(netPurchase, cap) {
  const amount = BigInt(netPurchase) * 1000n;
  const limit = BigInt(cap) * BigInt(STOCK_FLOW_NEUTRAL_TENTHS);
  return amount > limit ? 'red' : amount < -limit ? 'blue' : 'gray';
}
const drawStockFlowMap = (() => {
  const {snapshot, holdings} = __DATA__;
  const card = document.getElementById('stock-flow-card');
  const container = card.querySelector('#stock-flow-chart');
  if (!container) return view => {
    const target = document.getElementById(view);
    if (target) target.append(card);
  };
  const periodButtons = [...card.querySelectorAll('[data-stock-period]')];
  const investorButtons = [...card.querySelectorAll('[data-stock-investor]')];
  const order = card.querySelector('#stock-flow-order');
  const percent = new Intl.NumberFormat('ko-KR', {minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero'});
  const won = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 1, signDisplay: 'exceptZero'});
  const capWon = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: 1});
  const amountTone = value => value > 0 ? 'red' : value < 0 ? 'blue' : 'gray';
  const verdictName = {red: '매수 우위', gray: '중립', blue: '매도 우위'};
  const fmt = value => percent.format(value) + '%';
  const fmtWon = value => won.format(value / 1e8) + '억 원';
  let period = '1', investor = 'combined', view = 'stock-flow-all', chart;

  const net = amounts => investor === 'foreign' ? amounts[0] :
    investor === 'institution' ? amounts[1] : amounts[0] + amounts[1];
  function draw() {
    const scope = view === 'stock-flow-all' ? null : new Set(holdings[view] || []);
    const universe = snapshot.rows.filter(row => !scope || scope.has(row.ticker));
    const points = universe.filter(row => row.sums[period] && row.sums.recent).map(row => {
      const long = net(row.sums[period]), recent = net(row.sums.recent);
      return {...row, value: [long / row.cap * 100, recent / row.cap * 100], long, recent,
              tone: stockFlowTone(long, row.cap)};
    });
    const index = order.value === 'recent' ? 1 : 0;
    points.sort((a, b) => b.value[index] - a.value[index] || a.name.localeCompare(b.name, 'ko'));
    card.querySelector('#stock-flow-count').textContent = points.length + '개 표시 / ' + universe.length + '개';
    card.querySelector('#stock-flow-long-amount').textContent = period + '개월 누적 (억 원)';
    card.querySelector('#stock-flow-long').textContent = period + '개월 ÷ 시총';
    card.querySelector('#stock-flow-verdict-heading').textContent = period + '개월 구분';
    card.querySelector('#stock-flow-rows').replaceChildren(...points.map(row => {
      const tr = document.createElement('tr');
      const columns = [
        [row.name + ' · ' + row.ticker], [capWon.format(row.cap / 1e8)],
        [won.format(row.long / 1e8), amountTone(row.long)], [fmt(row.value[0]), amountTone(row.long)],
        [won.format(row.recent / 1e8), amountTone(row.recent)], [fmt(row.value[1]), amountTone(row.recent)],
      ];
      for (const [text, tone] of columns) {
        const cell = tr.insertCell();
        cell.textContent = text;
        if (tone) cell.dataset.flowTone = tone;
      }
      const badge = document.createElement('span');
      badge.className = 'stock-flow-tag';
      badge.dataset.flowTone = row.tone;
      badge.textContent = verdictName[row.tone];
      tr.insertCell().append(badge);
      return tr;
    }));

    if (typeof echarts === 'undefined') {
      container.textContent = '차트를 불러오지 못했습니다. 아래 종목별 순매수 표에서 수치를 확인할 수 있습니다.';
      return;
    }
    if (!chart) {
      chart = echarts.init(container);
      new ResizeObserver(() => { if (container.clientWidth) chart.resize(); }).observe(container);
    }
    const style = getComputedStyle(container);
    const color = variable => style.getPropertyValue(variable).trim();
    const compact = container.clientWidth < 480;
    const axis = (label, index) => {
      const values = points.map(point => point.value[index]);
      const min = Math.min(index === 0 ? -stockFlowNeutralPercent : 0, ...values);
      const max = Math.max(index === 0 ? stockFlowNeutralPercent : 0, ...values);
      const pad = Math.max((max - min) * .1, .1);
      return {type: 'value', name: label, nameLocation: 'middle', nameGap: index ? 47 : 30,
              min: min - pad, max: max + pad, splitNumber: compact ? 4 : 8,
              nameTextStyle: {color: color('--text-2')},
              axisLabel: {color: color('--text-2'), formatter: value => Number(value.toFixed(2)) + '%'},
              splitLine: {lineStyle: {color: color('--grid')}}};
    };
    chart.setOption({
      animation: false,
      grid: {left: compact ? 64 : 78, right: compact ? 13 : 30, top: 24, bottom: 57},
      title: points.length ? [] : {text: '이 범위에 표시할 종목이 없습니다.', left: 'center', top: 'center',
                                     textStyle: {color: color('--text-2'), fontSize: 13, fontWeight: 400}},
      tooltip: {
        trigger: 'item', confine: true, renderMode: 'richText',
        backgroundColor: color('--surface'), borderColor: color('--border'),
        textStyle: {color: color('--text')},
        formatter({data: row}) {
          return row.name + ' · ' + row.ticker + '\n' +
            period + '개월: ' + fmt(row.value[0]) + ' (' + fmtWon(row.long) + ')\n' +
            '최근 5거래일: ' + fmt(row.value[1]) + ' (' + fmtWon(row.recent) + ')';
        },
      },
      xAxis: axis(period + '개월 누적 순매수 / 시총', 0),
      yAxis: axis('최근 5거래일 순매수 / 시총', 1),
      series: [{
        type: 'scatter', symbol: 'circle', data: points, symbolSize: points.length > 40 ? 8 : 10,
        itemStyle: {color: ({data}) => color('--flow-' + data.tone), opacity: .95},
        label: {show: points.length <= 36, color: color('--text'), position: 'top',
                fontSize: compact ? 10 : 12, formatter: ({data}) => data.name},
        labelLayout: {hideOverlap: true},
        markLine: {silent: true, symbol: 'none', label: {show: false},
                   lineStyle: {type: 'solid', color: color('--text-3')}, data: [{xAxis: 0}, {yAxis: 0}]},
        markArea: {silent: true, itemStyle: {color: color('--flow-gray'), opacity: .07},
                   data: [[{xAxis: -stockFlowNeutralPercent}, {xAxis: stockFlowNeutralPercent}]]},
      }],
    }, true);
    chart.resize();
  }
  periodButtons.forEach(button => button.onclick = () => {
    period = button.dataset.stockPeriod;
    periodButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    draw();
  });
  investorButtons.forEach(button => button.onclick = () => {
    investor = button.dataset.stockInvestor;
    investorButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    draw();
  });
  order.onchange = draw;
  return selected => {
    const target = document.getElementById(selected);
    if (!target) return;
    view = selected;
    target.append(card);
    draw();
  };
})();
"""


def script(snapshot, sectors):
    holdings = {
        f'flow-{row["ticker"]}': [item["ticker"] for item in row.get("holdings", [])]
        for row in sectors.get("rows", [])
    }
    data = json.dumps({"snapshot": snapshot, "holdings": holdings}, ensure_ascii=False, allow_nan=False)
    script = SCRIPT.replace("__NEUTRAL_TENTHS__", str(NEUTRAL_TENTHS))
    return "<script>" + script.replace("__DATA__", data.replace("<", "\\u003c")) + "</script>"
