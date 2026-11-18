"""Interactive area charts for memory prices and market balances."""

CSS = """
.trend-controls{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;
  gap:6px 12px;margin:8px 4px 4px}
.trend-caption{font-size:12px;color:var(--text-3);margin:0}
.trend-buttons{display:flex;flex-wrap:wrap;gap:6px}
.trend-controls .igroup button{padding:4px 8px}
.cchart.dram-chart{min-width:0;height:clamp(280px,36vw,405px)}
"""


def controls(*, memory=False):
    periods = (
        '<div class="igroup" role="group" aria-label="메모리 차트 기간">'
        '<button type="button" data-range="all" class="on" aria-pressed="true">전체</button>'
        '<button type="button" data-range="recent" aria-pressed="false">최근 12주</button></div>'
    ) if memory else ""
    return (
        '<div class="trend-controls"><p class="trend-caption">시작일 대비 변화율</p>'
        f'<div class="trend-buttons">{periods}'
        '<div class="igroup" role="group" aria-label="차트 표시 단위">'
        '<button type="button" data-mode="raw" aria-pressed="false">실제 값</button>'
        '<button type="button" data-mode="change" aria-pressed="true" class="on">변화율</button>'
        '</div></div></div>'
    )


def memory_chart():
    return (
        controls(memory=True)
        + '<div id="dram-trend" class="cchart dram-chart" data-start="2025-12-31" '
          'role="img" aria-label="DDR5 현물가격과 시작일 대비 변화율"></div>'
    )


SCRIPT = r"""
const CREDIT = __DATA__;
const creditCharts = [];
let memoryTrend = null;
const trendDate = value => new Date(value).toISOString().slice(0, 10);
const trendPercent = (value, digits) => Number(value.toFixed(digits)).toLocaleString('ko-KR') + '%';
const indexNumber = value => value.toLocaleString('ko-KR', {maximumFractionDigits: 2});

function trendOption(view, mode) {
  const relative = mode === 'change';
  const series = [view.primary];
  if (view.overlay?.line.length) series.push(view.overlay);
  const secondaryAxis = !relative && series.length === 2;
  const seriesOptions = series.map((item, i) => {
    const baseline = item.line[0][1];
    const data = item.line.map(([date, price, approximate]) => [
      date, relative ? (price / baseline - 1) * 100 : price, price, approximate ? 1 : 0,
    ]);
    return {
      name: item.name, type: 'line', data, encode: {x: 0, y: 1},
      yAxisIndex: secondaryAxis && i === 1 ? 1 : 0,
      showSymbol: false, symbolSize: 5, z: i === 0 ? 3 : 2,
      lineStyle: {color: item.color, width: i === 0 ? 2.1 : 1.25},
      itemStyle: {color: item.color},
      ...(i === 0 ? {areaStyle: {
        origin: relative ? 0 : 'start',
        opacity: 0.23,
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          {offset: 0, color: item.color}, {offset: 1, color: 'transparent'},
        ]),
      }} : {}),
    };
  });
  const axisText = plotColor('text-3');
  const axes = [{
    type: 'value', position: 'left', scale: true,
    splitLine: {lineStyle: {color: plotColor('grid')}},
    axisLabel: {color: axisText, fontSize: 11,
      formatter: relative ? v => trendPercent(v, 1) : view.primary.axis},
  }];
  if (secondaryAxis) axes.push({
    type: 'value', position: 'right', scale: true, splitLine: {show: false},
    axisLabel: {color: axisText, fontSize: 11, formatter: v => Math.round(v).toLocaleString('ko-KR')},
  });
  return {
    animation: false, useUTC: true, backgroundColor: 'transparent',
    grid: {left: 3, right: secondaryAxis ? 6 : 12, top: 15, bottom: 6, containLabel: true},
    tooltip: {
      trigger: 'axis', confine: true,
      axisPointer: {type: 'line', snap: true},
      backgroundColor: plotColor('surface'), borderColor: plotColor('border'),
      textStyle: {color: plotColor('text'), fontSize: 12},
      formatter: points => {
        if (!points.length) return '';
        const day = points[0].data[0];
        return day + '<br>' + points.map(point => {
          const item = series[point.seriesIndex];
          const raw = (point.data[3] ? '약 ' : '') + item.format(point.data[2]);
          const value = relative ? trendPercent(point.data[1], 2) + ' (' + raw + ')' : raw;
          const observation = point.data[0] === day ? '' : ' · ' + point.data[0];
          return point.marker + item.name + ' <b>' + value + '</b>' + observation;
        }).join('<br>');
      },
    },
    xAxis: {
      type: 'time', min: view.primary.line[0][0], max: view.primary.line.at(-1)[0],
      axisLine: {lineStyle: {color: plotColor('border')}},
      axisLabel: {color: axisText, fontSize: 11, hideOverlap: true,
        formatter: {year: '{yyyy}년', month: '{M}월', day: '{M}/{d}'}},
      splitLine: {show: false},
    },
    yAxis: axes,
    series: seriesOptions,
  };
}

function mountTrend(container, getView) {
  const parent = container.parentElement;
  const controls = parent.querySelector('.trend-controls');
  const caption = controls.querySelector('.trend-caption');
  const chart = echarts.init(container, null, {renderer: 'canvas'});
  let mode = 'change';
  let range = 'all';
  function render() {
    const view = getView(range);
    caption.textContent = mode === 'change' ? '시작일 대비 변화율' : view.primary.unit;
    chart.setOption(trendOption(view, mode), true);
  }
  controls.addEventListener('click', event => {
    const button = event.target.closest('[data-mode],[data-range]');
    if (!button) return;
    if (button.dataset.mode) mode = button.dataset.mode;
    if (button.dataset.range) range = button.dataset.range;
    for (const option of controls.querySelectorAll('[data-mode],[data-range]')) {
      const active = option.dataset.mode === mode || option.dataset.range === range;
      option.classList.toggle('on', active);
      option.setAttribute('aria-pressed', String(active));
    }
    render();
  });
  render();
  return chart;
}

function drawCredit() {
  if (creditCharts.length) return;
  if (typeof echarts === 'undefined') { chartUnavailable('t4'); return; }
  const colors = {'코스피': 'series-1', '코스닥': 'series-2', '투자자예탁금': 'good'};
  for (const container of document.querySelectorAll('[data-credit]')) {
    const [key, kind] = container.dataset.credit.split(':');
    const panel = key === 'deposit' ? CREDIT.deposit : CREDIT.panels[+key];
    const amount = panel?.[kind];
    if (!amount?.line.length) continue;
    const deposit = key === 'deposit';
    const primary = {
      name: deposit ? panel.name : panel.name + ' 신용',
      line: amount.line, color: plotColor(colors[panel.name]), unit: '잔고 · 조 원',
      format: v => v.toFixed(amount.decimals) + '조 원',
      axis: v => Number(v.toFixed(1)).toLocaleString('ko-KR') + '조',
    };
    const overlay = {
      name: (deposit ? '코스피' : panel.name) + ' 지수', line: panel.index ?? [],
      format: indexNumber, color: plotColor('text-3'),
    };
    creditCharts.push(mountTrend(container, () => ({primary, overlay})));
  }
}

function drawMemory() {
  const container = document.getElementById('dram-trend');
  if (!container) return;
  if (memoryTrend) { memoryTrend.resize(); return; }
  if (typeof echarts === 'undefined') { chartUnavailable('memory'); return; }
  const rows = [...container.parentElement.querySelectorAll('tbody tr')].reverse();
  const prices = rows.map(row => {
    const date = row.querySelector('th').textContent.trim();
    const text = row.querySelector('td').textContent.trim();
    const approximate = text.startsWith('약 ');
    return [date, Number(text.replace(/^약\s+/, '')), approximate];
  }).filter(([date, price]) => date >= container.dataset.start && Number.isFinite(price) && price > 0);
  if (!prices.length) return;
  const firstRecent = trendDate(Date.parse(prices.at(-1)[0]) - 77 * 86400000);
  const recent = prices.filter(([date]) => date >= firstRecent);
  const usd = v => v.toLocaleString('en-US', {maximumFractionDigits: 3});
  memoryTrend = mountTrend(container, range => ({
    primary: {
      name: 'DDR5 16Gb 현물가', line: range === 'recent' ? recent : prices,
      unit: '미국 달러 / 칩', color: plotColor('series-1'),
      format: v => '$' + usd(v), axis: v => '$' + usd(v),
    },
  }));
}

window.addEventListener('resize', () => {
  for (const chart of [...creditCharts, memoryTrend]) {
    if (chart?.getDom().clientWidth) chart.resize();
  }
});
"""
