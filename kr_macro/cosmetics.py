"""품목별 전국 수출 자료와 공통 차트 화면."""
import hashlib
import html
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def collect(output, today, config, fetch_month):
    items = config["items"]
    groups = {item["id"]: item["hs"] for item in items}
    signature = hashlib.sha256(json.dumps(groups, sort_keys=True).encode()).hexdigest()[:12]
    cache = Path(output) / f".cosmetics_exports_{signature}.csv"
    data = pd.read_csv(cache, index_col=0) if cache.exists() else pd.DataFrame()
    # 분기별 전년 비교를 위해 시작하는 분기도 온전히 받는다.
    first = (today.to_period("M") - 48).asfreq("Q").asfreq("M", "start")
    months = pd.period_range(first, today.to_period("M") - 1, freq="M")
    columns = list(groups)
    data = data.reindex(index=months.astype(str), columns=columns)
    todo = [m for m in months if data.loc[str(m)].isna().any() or m >= months[-1] - 1]
    fetched, notes = {}, []
    for position, month in enumerate(todo, 1):
        try:
            row = fetch_month(month.strftime("%Y%m"), groups)
            if row is not None:
                values = {key: float(row[key]) for key in columns}
                if not all(np.isfinite(v) and v >= 0 for v in values.values()):
                    raise ValueError("수출금액이 유효하지 않습니다")
                fetched[str(month)] = values
        except Exception as exc:
            notes.append(f"{month} 자료 갱신 실패 ({exc.__class__.__name__}) · 저장된 자료 기준")
            break
        if position % 6 == 0 or position == len(todo):
            print(f"  뷰티·의료 수출 {position}/{len(todo)}개월 확인", flush=True)
    if fetched:
        updates = pd.DataFrame.from_dict(fetched, orient="index")
        data.update(updates)
        cache.parent.mkdir(exist_ok=True)
        temporary = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        data.to_csv(temporary)
        temporary.replace(cache)
    available = data.dropna()
    if available.empty:
        raise ValueError("화장품 수출 데이터가 비어 있습니다")
    last = available.index[-1]
    data = data.loc[:last]
    if data.isna().any(axis=None):
        notes.append("일부 월 자료가 없어 해당 구간과 전년 비교를 빈 값으로 표시합니다.")
    return {
        "months": list(data.index), "asof": last, "updated": today.strftime("%Y-%m-%d"),
        "notes": notes,
        "items": [
            {**item, "values": [None if pd.isna(v) else float(v) for v in data[item["id"]]]}
            for item in items
        ],
    }


CSS = """
.cosm-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.cosm-head h2{font-size:17px;margin:0 0 7px}.cosm-source,.cosm-updated{font-size:12px;color:var(--mu)}
.cosm-controls,.cosm-products{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
.cosm-controls .igroup{margin:0}.cosm-btn{font:inherit;font-size:12px;color:var(--text-2);cursor:pointer;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 14px}
.cosm-btn:hover{color:var(--text);border-color:var(--accent)}.cosm-btn.on{color:var(--accent);border-color:var(--accent);background:#ffad3312}
.cosm-frame{border:1px solid var(--border);border-radius:14px;padding:14px 10px 4px;background:var(--bg)}
.cosm-title{text-align:center;font-size:14px;font-weight:700;margin:0 0 4px}.cosm-description{text-align:center;font-size:11px;color:var(--mu)}
.cosm-scope{text-align:center;font-size:11px;color:var(--mu);margin-top:5px;line-height:1.6}
.cosm-chart{height:380px;width:100%}.cosm-table{width:100%;border-collapse:collapse;font-size:13px}
.cosm-table th,.cosm-table td{padding:12px 16px;text-align:right;border-bottom:1px solid var(--border)}
.cosm-table th:first-child,.cosm-table td:first-child{text-align:left}.cosm-table th{color:var(--mu);font-weight:500}
.cosm-frame [hidden]{display:none}.cosm-note{font-size:11px;color:var(--mu);margin:12px 2px 0;line-height:1.7}
@media(max-width:760px){.cosm-chart{height:330px}.cosm-btn{padding:8px 10px}.cosm-table th,.cosm-table td{padding:10px}}
"""


def render(data, note=None, *, namespace="cosmetics", title="수출 데이터"):
    if data is None:
        return f'<p class="note">{html.escape(note or "화장품 수출 자료를 받지 못했습니다.")}</p>'
    products = "".join(
        f'<button class="cosm-btn{" on" if i == 0 else ""}" data-cosm-product="{html.escape(item["id"])}" '
        f'aria-pressed="{"true" if i == 0 else "false"}">{html.escape(item["name"])}</button>'
        for i, item in enumerate(data["items"]))
    notes = "".join(f'<p class="note">{html.escape(n)}</p>' for n in data["notes"])
    return f"""
<div class="card" id="{html.escape(namespace)}-panel">
  <div class="cosm-head">
    <div><h2>{html.escape(title)}</h2><div class="cosm-source">관세청 품목별 수출 · 전국 · FOB 달러</div></div>
    <div class="cosm-updated">업데이트 {data["updated"]} · 월별 자료 {data["asof"]}까지</div>
  </div>
  {notes}
  <div class="cosm-controls">
    <div class="igroup">
      <button class="cosm-btn on" data-cosm-period="monthly" aria-pressed="true">월별</button>
      <button class="cosm-btn" data-cosm-period="quarterly" aria-pressed="false">분기별</button>
    </div>
    <div class="igroup">
      <button class="cosm-btn on" data-cosm-view="chart" aria-pressed="true">차트</button>
      <button class="cosm-btn" data-cosm-view="table" aria-pressed="false">표</button>
    </div>
    <div class="igroup">
      <button class="cosm-btn on" data-cosm-years="1" aria-pressed="true">1년</button>
      <button class="cosm-btn" data-cosm-years="2" aria-pressed="false">2년</button>
      <button class="cosm-btn" data-cosm-years="3" aria-pressed="false">3년</button>
    </div>
  </div>
  <div class="cosm-products">{products}</div>
  <div class="cosm-frame">
    <div class="cosm-title" id="{html.escape(namespace)}-title" data-cosm-role="title" aria-live="polite"></div>
    <div class="cosm-description" id="{html.escape(namespace)}-description" data-cosm-role="description"></div>
    <div class="cosm-scope" id="{html.escape(namespace)}-scope" data-cosm-role="scope"></div>
    <div class="cosm-chart" id="{html.escape(namespace)}-chart" data-cosm-role="chart" role="img" aria-label="품목별 수출액 및 전년 대비 증감률"></div>
    <div class="scroll" id="{html.escape(namespace)}-table" data-cosm-role="table" hidden></div>
  </div>
  <p class="cosm-note">수출액은 백만 달러 · 전년 대비 = (수출액 ÷ 전년 같은 기간 수출액 − 1) × 100<br>
    분기별은 3개월 자료가 모두 있는 분기의 합계 · 전년 금액이 0이거나 자료가 없으면 증감률은 —<br>
    선택한 HS 분류에 속하는 한국 전체 수출액 · 품목별 집계 범위는 차트 위에 표시합니다.</p>
</div>
"""


JS = r"""
const COSMETICS = __DATA__;
const cosmeticsRoot = document.getElementById(__ROOT_ID__);
const cosmeticsState = {product: COSMETICS.items[0].id, period: 'monthly', view: 'chart', years: 1, chart: null, ready: false};
function cosmeticsPoints(item, period, years) {
  const values = new Map(COSMETICS.months.map((month, i) => [month, item.values[i]]));
  let points;
  if (period === 'quarterly') {
    const quarters = new Map();
    values.forEach((value, month) => {
      const key = month.slice(0, 4) + '-Q' + Math.ceil(Number(month.slice(5)) / 3);
      if (!quarters.has(key)) quarters.set(key, []);
      quarters.get(key).push(value);
    });
    const totals = new Map();
    quarters.forEach((quarterValues, key) => {
      if (quarterValues.length === 3) {
        const total = quarterValues.every(value => value !== null) ? quarterValues.reduce((sum, value) => sum + value, 0) : null;
        totals.set(key, total);
      }
    });
    points = [...totals].map(([key, value]) => ({key, value, prior: totals.get((Number(key.slice(0, 4)) - 1) + key.slice(4))}));
  } else {
    points = [...values].map(([key, value]) => ({key, value, prior: values.get((Number(key.slice(0, 4)) - 1) + key.slice(4))}));
  }
  return points.slice(-years * (period === 'quarterly' ? 4 : 12)).map(point => ({
    key: point.key,
    label: point.key.slice(2).replace('-', '.'),
    amount: point.value === null ? null : point.value / 1e6,
    yoy: point.value !== null && point.prior > 0 ? (point.value / point.prior - 1) * 100 : null,
  }));
}
function cosmeticsAmount(value) {
  return value === null ? '—' : value.toLocaleString('ko-KR', {minimumFractionDigits: 1, maximumFractionDigits: 1});
}
function cosmeticsPercent(value) {
  return value === null ? '—' : (value > 0 ? '+' : '') + value.toFixed(1) + '%';
}
function cosmeticsOption(points) {
  return {
    animation: false, backgroundColor: 'transparent',
    grid: {left: 76, right: 72, top: 65, bottom: 42},
    legend: {top: 16, data: ['전년 대비 (%)', '수출액 (백만 달러)'], textStyle: {color: '#81939b', fontSize: 11}},
    tooltip: {
      trigger: 'axis', backgroundColor: '#262626', borderColor: '#353535',
      textStyle: {color: '#edf2f0', fontSize: 12},
      formatter: rows => {
        const point = points[rows[0].dataIndex];
        return point.key + '<br>수출액 <b>' + cosmeticsAmount(point.amount) + ' 백만 달러</b><br>전년 대비 <b>' + cosmeticsPercent(point.yoy) + '</b>';
      },
    },
    xAxis: {type: 'category', data: points.map(point => point.label),
      axisLine: {lineStyle: {color: '#353535'}}, axisTick: {alignWithLabel: true},
      axisLabel: {color: '#81939b', fontSize: 11, hideOverlap: true}},
    yAxis: [
      {type: 'value', name: '백만 달러', min: 0, nameTextStyle: {color: '#81939b'},
        axisLabel: {color: '#81939b', formatter: value => value.toLocaleString('ko-KR')},
        splitLine: {lineStyle: {color: '#2c2c2c'}}},
      {type: 'value', name: '전년 대비', nameTextStyle: {color: '#81939b'},
        axisLabel: {color: '#81939b', formatter: value => (value > 0 ? '+' : '') + value + '%'},
        splitLine: {show: false}},
    ],
    series: [
      {name: '수출액 (백만 달러)', type: 'bar', data: points.map(point => point.amount), barMaxWidth: 60,
        itemStyle: {color: 'rgba(255,173,51,0.30)', borderColor: '#b6812f', borderWidth: 1}},
      {name: '전년 대비 (%)', type: 'line', yAxisIndex: 1, data: points.map(point => point.yoy),
        connectNulls: false, symbol: 'circle', symbolSize: 7, smooth: false,
        lineStyle: {color: '#81939b', width: 2}, itemStyle: {color: '#ff7770'}},
    ],
  };
}
function renderCosmetics() {
  const state = cosmeticsState;
  const item = COSMETICS.items.find(item => item.id === state.product);
  const points = cosmeticsPoints(item, state.period, state.years);
  cosmeticsRoot.querySelector('[data-cosm-role="title"]').textContent = item.name + ' · 전국';
  cosmeticsRoot.querySelector('[data-cosm-role="description"]').textContent = 'HS ' + item.hs.join(', ') + ' · ' + (state.period === 'monthly' ? '월별' : '분기별') + ' · 최근 ' + state.years + '년';
  cosmeticsRoot.querySelector('[data-cosm-role="scope"]').textContent = item.scope || '';
  const chartElement = cosmeticsRoot.querySelector('[data-cosm-role="chart"]');
  const tableElement = cosmeticsRoot.querySelector('[data-cosm-role="table"]');
  chartElement.hidden = state.view !== 'chart';
  tableElement.hidden = state.view !== 'table';
  const table = document.createElement('table');
  table.className = 'cosm-table';
  table.innerHTML = '<thead><tr><th>기간</th><th>수출액 (백만 달러)</th><th>전년 대비</th></tr></thead>';
  const body = document.createElement('tbody');
  [...points].reverse().forEach(point => {
    const row = document.createElement('tr');
    [point.key, cosmeticsAmount(point.amount), cosmeticsPercent(point.yoy)].forEach(text => {
      const cell = document.createElement('td');
      cell.textContent = text;
      row.append(cell);
    });
    body.append(row);
  });
  table.append(body);
  tableElement.replaceChildren(table);
  if (state.view === 'chart' && typeof echarts !== 'undefined') {
    if (!state.chart) state.chart = echarts.init(chartElement, null, {renderer: 'canvas'});
    state.chart.setOption(cosmeticsOption(points), true);
    state.chart.resize();
  } else if (state.view === 'chart') {
    chartElement.hidden = true;
    tableElement.hidden = false;
    chartUnavailable(cosmeticsRoot.id);
  }
  cosmeticsRoot.querySelectorAll('[data-cosm-product], [data-cosm-period], [data-cosm-view], [data-cosm-years]').forEach(button => {
    const selected = Object.entries(button.dataset).every(([key, value]) => {
      const field = key.replace('cosm', '').toLowerCase();
      return String(state[field]) === value;
    });
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}
function drawCosmetics() {
  if (!cosmeticsState.ready) {
    cosmeticsRoot.querySelectorAll('[data-cosm-product], [data-cosm-period], [data-cosm-view], [data-cosm-years]').forEach(button => {
      button.addEventListener('click', () => {
        Object.entries(button.dataset).forEach(([key, value]) => {
          const field = key.replace('cosm', '').toLowerCase();
          cosmeticsState[field] = field === 'years' ? Number(value) : value;
        });
        renderCosmetics();
      });
    });
    cosmeticsState.ready = true;
  }
  renderCosmetics();
}
window.addEventListener('resize', () => cosmeticsState.chart && cosmeticsState.chart.resize());
"""


def script(data, *, namespace="cosmetics"):
    if data is None:
        return ""
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("</", r"<\/")
    body = JS.replace("__DATA__", encoded).replace("__ROOT_ID__", json.dumps(namespace + "-panel"))
    name = "draw" + namespace[0].upper() + namespace[1:]
    return "(() => {\n" + body + "\nwindow[" + json.dumps(name) + "] = drawCosmetics;\n})();"
