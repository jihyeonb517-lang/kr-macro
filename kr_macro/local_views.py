"""로컬 대시보드의 판정 근거, 분기 비교 및 실행 상태."""
import html
import json
import os
from pathlib import Path

import pandas as pd
import apr_ir


def evidence(rows, group):
    lines = []
    for row in rows:
        ind = row["ind"]
        if ind["group"] != group:
            continue
        res = row.get("res")
        direction, date = "미연결 · 제외", "—"
        if res:
            signal = res["zone"]["signal"]
            direction = {1: "긍정 +1", 0: "중립 0", -1: "부정 −1"}[signal]
            if res["stale"]:
                direction = "갱신 지연 · 제외"
            date = res["date"].strftime("%Y-%m-%d" if ind["freq"] == "daily" else "%Y-%m")
        lines.append(f'<li>{html.escape(ind["name"])} <b>{direction}</b> <time>{date}</time></li>')
    return '<details class="basis"><summary>판정 근거</summary><ul>' + ''.join(lines) + '</ul></details>'


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
.basis{margin-top:9px;font-size:13px;font-weight:400}.basis summary{cursor:pointer}
.basis ul{padding-left:16px}.basis li{margin-top:7px}.basis time{color:var(--mu);white-space:nowrap}
.cmp h2{font-size:18px}.cmp table{border-collapse:collapse;width:100%;min-width:480px}
.cmp th,.cmp td{text-align:right;padding:8px;border-bottom:1px solid var(--border)}
.cmp tr>:first-child{text-align:left}.cmp a{text-decoration:underline}
"""
