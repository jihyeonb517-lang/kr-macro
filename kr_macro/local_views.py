"""로컬 대시보드의 장세 판단 화면, 분기 비교 및 실행 상태."""
import html
import json
import os
from pathlib import Path

import pandas as pd
import apr_ir


def apr_comparison(customs):
    reports, note = apr_ir.reports()
    totals = {}
    if customs:
        for item in customs["items"]:
            if item["id"] in ("skincare", "homebeauty"):
                months = pd.PeriodIndex(customs["months"], freq="M")
                values = pd.Series(item["values"], index=months, dtype=float)
                totals[item["id"]] = values.groupby(months.asfreq("Q")).sum(min_count=3)
    periods = set(pd.Period(q, freq="Q") for q in reports)
    for series in totals.values():
        periods.update(series.dropna().index)
    first = pd.Period(min(reports), freq="Q")
    rows = []
    for q in sorted((p for p in periods if p >= first), reverse=True):
        record = reports.get(str(q))
        growth = amount = "—"
        if record:
            growth = f'{100*(record["current"]/record["prior"]-1):+.1f}%'
            amount = f'<a href="{html.escape(record["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{record["current"]/100:,.1f}</a>'
        cells = []
        for key in ("homebeauty", "skincare"):
            series = totals.get(key, {})
            current, prior = series.get(q), series.get(q-4)
            cells.append(f'{100*(current/prior-1):+.1f}%' if pd.notna(current) and pd.notna(prior) and prior > 0 else '—')
        rows.append(f'<tr><th scope="row">{q.year}년 {q.quarter}분기</th><td>{amount}</td><td>{growth}</td><td>{cells[0]}</td><td>{cells[1]}</td></tr>')
    return ('<section class="card cmp"><h2>해외 매출과 한국 수출</h2><div class="scroll"><table><thead><tr>'
            '<th>분기</th><th>에이피알 해외 매출<br>(억원)</th><th>해외 매출<br>전년비</th>'
            '<th>전국 홈뷰티 수출<br>전년비</th><th>전국 기초화장품 수출<br>전년비</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>' + (f'<p class="note">{note}</p>' if note else '') + '<p class="legend">매출액을 누르면 해당 분기 공식 IR 원문이 열립니다. '
            '해외 매출 전년비는 같은 발표자료의 당기·전년 동기 원화 매출 기준입니다. '
            '수출 전년비는 관세청 달러 수출액을 3개월씩 합산해 계산합니다. — 는 확인된 자료 부족입니다.</p></section>')


def write_state(out, state, when):
    target = out / "refresh-status.js"
    temp = out / f".refresh-{os.getpid()}.tmp"
    payload = json.dumps({"state": state, "at": when.isoformat()})
    temp.write_text("window.localRefresh = " + payload + ";", encoding="utf-8")
    temp.replace(target)


STATUS_SCRIPT = """<script>
if (location.protocol === 'file:') {
  const status = document.getElementById('refresh-state');
  const script = document.createElement('script');
  script.src = './refresh-status.js?t=' + Date.now();
  script.onload = () => {
    const latest = window.localRefresh;
    if (!latest || Date.parse(latest.at) < Date.parse(status.dataset.at)) return;
    if (latest.state === 'failed' || latest.state === 'running') {
      status.textContent = (latest.state === 'failed' ? '최근 갱신 실패' : '갱신 완료 대기')
        + ' · 이전 보관본 표시 중 · 시도 ' + latest.at.slice(0,16).replace('T',' ') + ' KST';
    }
  };
  document.head.appendChild(script);
}
</script>"""

CSS = """
.cmp h2{font-size:18px}.cmp table{border-collapse:collapse;width:100%;min-width:480px}
.cmp th,.cmp td{text-align:right;padding:8px;border-bottom:1px solid var(--border)}
.cmp tr>:first-child{text-align:left}.cmp a{text-decoration:underline}
"""


MARKET_CSS = """
#t1 [hidden]{display:none!important}
.market-summary,.market-evidence{container-type:inline-size}
.market-summary h2,.market-evidence h2,.market-section h3,.market-label h4{margin:0}
.market-summary h2{font-size:14px;color:var(--text-2);font-weight:650}
.market-headline{font-size:clamp(18px,2.7cqw,26px);font-weight:750;line-height:1.45;margin:5px 0 17px;letter-spacing:-.025em;word-break:keep-all;overflow-wrap:anywhere}
.market-groups{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.market-group-card{font:inherit;text-align:left;min-width:0;display:grid;align-content:start;gap:4px;padding:11px 13px;border:1px solid var(--border);border-radius:12px;background:var(--bg);color:var(--text);cursor:pointer;transition:background-color .15s,border-color .15s}
.market-group-card:hover,.market-group-card[aria-pressed=true]{background:var(--surface-2)}
.market-group-card[aria-pressed=true]{border-color:var(--text-3)}
.market-group-card strong{font-size:19px;line-height:1.25;margin:2px 0}
.market-group-card small{color:var(--text-3);font-size:12px}
.market-group-name{display:flex;justify-content:space-between;gap:6px;align-items:baseline;font-weight:650}
.market-group-name span{color:var(--text-3);font-size:15px;line-height:1}
.market-group-reason{color:var(--text);font-size:13px;line-height:1.5;margin:2px 0 4px;overflow-wrap:anywhere}
.market-recent{display:flex;flex-wrap:wrap;align-items:baseline;gap:5px 12px;margin-top:14px;padding:9px 12px;
  border-left:3px solid var(--accent);background:var(--bg);border-radius:6px;font-size:13px}
.market-recent ul{margin:0;padding:0;list-style:none}
.market-recent li + li{margin-top:4px}
.market-recent a{color:var(--text);text-decoration:underline;text-underline-offset:3px;overflow-wrap:anywhere}
.market-recent time{color:var(--text-2);white-space:nowrap}
.market-kpis{margin-top:13px;color:var(--text-2)}
.market-kpis summary,.market-guide summary{cursor:pointer;width:fit-content}
.market-kpis .kpis{margin-top:8px;line-height:1.75;overflow-wrap:anywhere}
.market-evidence-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px}
.market-evidence h2{font-size:19px;scroll-margin-top:16px;width:fit-content}
.market-evidence h2:focus-visible,.market-row:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.market-filters{display:flex;flex-wrap:wrap;gap:6px;margin-top:11px}
.market-evidence button{min-height:36px;font:inherit;cursor:pointer;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text-2);padding:5px 11px}
.market-evidence button:hover,.market-evidence button[aria-pressed=true]{background:var(--surface-2);color:var(--text)}
.market-evidence button[aria-pressed=true]{border-color:var(--text-3);font-weight:700}
.market-result{font-size:12px;color:var(--text-3);margin:7px 0 12px}
.market-empty{background:var(--bg);border:1px dashed var(--border);border-radius:10px;color:var(--text-2);padding:13px;margin:10px 0}
.market-section + .market-section{margin-top:16px}
.market-section-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 9px;padding:8px 10px;background:var(--surface-2);border-radius:9px}
.market-section-head h3{font-size:15px}
.market-section-head span{font-weight:700}
.market-section-head small{color:var(--text-2);font-size:11px}
.market-row{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) minmax(0,1.5fr) minmax(86px,.5fr);gap:12px;align-items:start;padding:12px 8px;border-bottom:1px solid var(--border);overflow-wrap:anywhere;scroll-margin-top:16px}
.market-section .market-row:last-child{border-bottom:0}
.market-row:target,.market-row:focus{background:var(--surface-2)}
.market-row small{display:block;font-size:12px;color:var(--text-3);line-height:1.45}
.market-label h4{font-size:14px;color:var(--text);font-weight:700}
.market-priority{color:var(--text-2)}
.market-reading,.market-rank{font-variant-numeric:tabular-nums}
.market-reading>b{font-size:19px;font-weight:750;color:var(--text);line-height:1.22}
.market-reading time{white-space:nowrap}
.market-reading small+small{margin-top:3px}
.market-delta{font-size:12px;color:var(--text-2)}
.market-delta b{color:var(--text)}
.market-verdict,.market-unavailable{border-radius:8px;background:var(--bg);padding:8px 9px}
.market-verdict strong,.market-unavailable strong{font-size:13px}
.market-direction{font-size:11px;margin-left:6px;color:var(--text-2);white-space:nowrap}
.market-transition{font-size:11px;color:var(--text-3);border-top:1px solid var(--border);margin-top:5px;padding-top:5px}
.market-transition b{color:var(--text)}
.market-transition-date{display:inline}
.market-zone-rule{font-size:12px;color:var(--text-2);margin-top:4px;line-height:1.5}
.market-row-details{min-width:0}
.market-row-details[open]{grid-column:1/-1}
.market-row-details summary{box-sizing:border-box;width:fit-content;margin-left:auto;padding:5px 8px;
  border:1px solid var(--border);border-radius:7px;color:var(--text-2);cursor:pointer}
.market-row-details summary:hover,.market-row-details[open] summary{background:var(--surface-2);color:var(--text)}
.market-detail-body{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:12px;margin-top:7px;
  padding:10px 12px;background:var(--bg);border-radius:8px}
.market-trend,.market-trend-unavailable{grid-column:1/-1;min-width:0;margin:0}
.market-trend{border-bottom:1px solid var(--border);padding-bottom:8px}
.market-trend figcaption{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px;margin-bottom:2px}
.market-trend figcaption span,.market-trend-unavailable{color:var(--text-2);font-size:12px}
.market-trend-chart{height:clamp(220px,28cqw,300px);width:100%;min-width:0}
.market-detail-body small + small{margin-top:4px}
.market-sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.market-unavailable{grid-column:2/-1}
.market-rank b{font-size:16px}
.market-rank-track{height:3px;background:var(--grid);border-radius:4px;margin:3px 0 5px}
.market-rank-track i{display:block;height:100%;background:var(--text-2);border-radius:inherit}
.market-guide{color:var(--text-2);margin-top:12px;padding-top:9px;border-top:1px solid var(--border);font-size:12px}
.market-guide ul{padding-left:18px;margin:8px 0 0}
.market-guide li+li{margin-top:5px}
@container (max-width:650px){
  .market-groups{grid-template-columns:repeat(2,minmax(0,1fr))}
  .market-row{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;padding:12px 2px}
  .market-verdict,.market-row-details{grid-column:1/-1}
  .market-row-details summary{min-height:44px;padding:11px 10px}
  .market-transition-date{display:block}
  .market-unavailable{grid-column:1/-1}
  .market-filters button,.market-scope{min-height:44px}
}
@media (prefers-reduced-motion:reduce){.market-group-card{transition:none}}
"""


MARKET_SCRIPT = """
<script>
(() => {
  const market = document.getElementById('t1');
  const cards = [...market.querySelectorAll('[data-market-group]')];
  const sections = [...market.querySelectorAll('[data-market-section]')];
  const filters = [...market.querySelectorAll('[data-market-filter]')];
  const filterLabels = new Map(filters.map(button => [button, button.textContent]));
  const reset = market.querySelector('[data-market-reset]');
  const title = market.querySelector('#market-evidence-title');
  const status = market.querySelector('.market-result');
  const empty = market.querySelector('.market-empty');
  let group = '';
  let filter = 'all';

  function update() {
    let count = 0;
    let unavailable = 0;
    const counts = {all: 0, negative: 0, changed: 0};
    for (const section of sections) {
      const inGroup = !group || group === section.dataset.marketSection;
      let visible = 0;
      for (const row of section.querySelectorAll('.market-row')) {
        const {state, change} = row.dataset;
        const current = state === 'negative' || state === 'current';
        if (inGroup && (state === 'unavailable' || (current && change === 'unknown'))) unavailable++;
        if (inGroup) {
          counts.all++;
          if (state === 'negative') counts.negative++;
          if (change === 'yes') counts.changed++;
        }
        const matches = filter === 'all' || (filter === 'negative' && state === 'negative') ||
          (filter === 'changed' && change === 'yes');
        row.hidden = !inGroup || !matches;
        if (!row.hidden) visible++;
      }
      section.hidden = visible === 0;
      count += visible;
    }
    for (const card of cards) card.setAttribute('aria-pressed', card.dataset.marketGroup === group);
    for (const button of filters) {
      const key = button.dataset.marketFilter;
      button.setAttribute('aria-pressed', String(key === filter));
      button.textContent = filterLabels.get(button) + ' ' + counts[key];
    }
    reset.hidden = !group;
    const selected = cards.find(card => card.dataset.marketGroup === group);
    const scope = selected ? selected.querySelector('.market-group-name').firstChild.textContent + ' · ' : '';
    const missing = filter === 'changed' && unavailable ? ' · 비교 자료 없음 ' + unavailable + '개' : '';
    status.textContent = scope + count + '개 지표' + missing;
    empty.hidden = count !== 0;
    if (!count) {
      empty.textContent = filter === 'negative' ? '이 범위에 현재 부정 판정이 없습니다.' :
        filter === 'changed' ? '비교 가능한 지표에서 확인된 판정 변경이 없습니다.' : '표시할 지표가 없습니다.';
    }
  }

  function jump(target) {
    target.focus({preventScroll: true});
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({behavior: reducedMotion ? 'instant' : 'smooth', block: 'start'});
  }

  const charts = new WeakMap();
  const trendObserver = new ResizeObserver(entries => {
    for (const {target, contentRect} of entries) {
      if (contentRect.width) charts.get(target)?.resize();
    }
  });

  function plotTrend(container, history) {
    const {points, boundaries, units, decimals} = history;
    const palette = getComputedStyle(container);
    const tone = name => palette.getPropertyValue(name).trim();
    const number = new Intl.NumberFormat('ko-KR', {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
    const boundaryNumber = new Intl.NumberFormat('ko-KR', {maximumFractionDigits: Math.max(4, decimals)});
    const showValue = value => number.format(value) + units;
    const values = points.map(point => point[1]).concat(boundaries);
    const lower = Math.min(...values);
    const upper = Math.max(...values);
    const padding = (upper - lower) * 0.13 || Math.pow(10, -decimals);
    const horizontal = tone('--grid');
    const ink = tone('--text-2');
    const accent = tone('--accent');
    const dates = points.map(point => point[0]);
    const transitions = points.flatMap(([date, value, label, previous], index) => {
      if (previous === null) return [];
      return [{
        value: [date, value],
        label: index === points.length - 1 ? {
          show: true, position: 'left', distance: 7, color: tone('--text'),
          backgroundColor: tone('--surface'), borderRadius: 4, padding: [4, 6],
          formatter: showValue(value) + ' · ' + previous + ' → ' + label,
        } : {show: false},
      }];
    });

    const chart = echarts.init(container);
    chart.setOption({
      animation: false,
      grid: {left: 10, right: 24, top: 19, bottom: 6, containLabel: true},
      tooltip: {
        trigger: 'axis', triggerOn: 'mousemove|click', confine: true, renderMode: 'richText',
        backgroundColor: tone('--surface'), borderColor: tone('--border'),
        textStyle: {color: tone('--text'), fontSize: 12},
        axisPointer: {type: 'line', lineStyle: {color: ink, type: 'dotted'}},
        formatter(params) {
          const point = points[dates.indexOf(params[0]?.axisValue)];
          if (!point) return '';
          const [date, value, label, previous] = point;
          return date + '\\n' + showValue(value) + ' · ' + (previous ? previous + ' → ' : '') + label;
        },
      },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false, axisTick: {show: false},
        axisLine: {lineStyle: {color: horizontal}}, axisLabel: {color: ink, hideOverlap: true},
      },
      yAxis: {
        type: 'value', min: lower - padding, max: upper + padding, splitNumber: 4,
        axisLabel: {color: ink, formatter: showValue}, splitLine: {lineStyle: {color: horizontal}},
      },
      series: [{
        name: '관측값', type: 'line', data: points.map(point => point[1]), showSymbol: false,
        lineStyle: {color: accent, width: 2}, itemStyle: {color: accent},
        markLine: {
          silent: true, symbol: 'none', lineStyle: {color: ink, type: 'dashed', opacity: 0.65},
          label: {position: 'insideEndTop', color: ink, formatter: item => boundaryNumber.format(item.value) + units},
          data: boundaries.map(value => ({yAxis: value})),
        },
      }, {
        name: '판정 변경', type: 'scatter', data: transitions, symbolSize: 8, z: 4,
        itemStyle: {color: tone('--warn'), borderColor: tone('--surface'), borderWidth: 1, opacity: 1},
      }],
    });
    charts.set(container, chart);
    trendObserver.observe(container);
  }

  function showTrend(detail) {
    if (!detail.open) return;
    const container = detail.querySelector('.market-trend-chart');
    if (!container?.clientWidth) return;
    const existing = charts.get(container);
    if (existing) {
      existing.resize();
      return;
    }
    if (typeof echarts === 'undefined') {
      container.textContent = '차트 구성 요소를 불러오지 못했습니다.';
      return;
    }
    container.textContent = '';
    const payload = detail.querySelector('.market-trend-data');
    plotTrend(container, JSON.parse(payload.textContent));
  }

  market.addEventListener('toggle', event => {
    if (event.target.matches('.market-row-details')) showTrend(event.target);
  }, true);
  if (document.readyState !== 'complete') {
    window.addEventListener('load', () => {
      for (const detail of market.querySelectorAll('.market-row-details[open]')) showTrend(detail);
    }, {once: true});
  }

  market.addEventListener('click', event => {
    const card = event.target.closest('[data-market-group]');
    const button = event.target.closest('[data-market-filter]');
    const link = event.target.closest('a[href^="#ind-"]');
    if (card) {
      group = card.dataset.marketGroup;
      filter = 'all';
      update();
      jump(title);
    } else if (button) {
      filter = button.dataset.marketFilter;
      update();
    } else if (event.target === reset) {
      group = '';
      update();
      title.focus({preventScroll: true});
    } else if (link && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
      const row = document.getElementById(link.hash.slice(1));
      if (!row) return;
      event.preventDefault();
      group = row.closest('[data-market-section]').dataset.marketSection;
      filter = 'all';
      update();
      jump(row);
    }
  });
  update();
})();
</script>
"""
