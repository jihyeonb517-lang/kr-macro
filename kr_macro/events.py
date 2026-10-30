"""다가오는 일정: 경제지표 · 중앙은행 · 기업 실적 · 국내 시장 규칙 일정.

출처마다 따로 받고, 한 곳이 실패해도 나머지 일정은 보여 준다.
- Yahoo 경제 일정표: 미국·한국 지표 (발표 시각, 직전 값·예상치). 약 4주 앞까지만 준다.
- 연준·한국은행·일본은행 공식 일정 페이지: 금리 결정 (다음 해 일정을 올리면 자동으로 들어온다).
- Yahoo 종목 정보: 다음 실적 발표일.
- 규칙: 옵션 만기, 관세청 10일·20일 수출, 산업부 월간 수출입, 정기보고서 제출 기한.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

KST = ZoneInfo("Asia/Seoul")
EASTERN = ZoneInfo("America/New_York")
JAPAN = ZoneInfo("Asia/Tokyo")
HORIZON_DAYS = 100
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130"}

YAHOO_URL = "https://finance.yahoo.com/calendar/economic/"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
BOK_URL = "https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do?mtgSe=A&menuNo=200755&pYear={year}"

# 구분: 미국 · 한국 · 일본 · 기업 · 시장(국내 시장 규칙 일정)
REGIONS = {"us": "미국", "kr": "한국", "jp": "일본", "co": "기업", "mk": "시장"}

# Yahoo 지표 이름(끝의 * 와 공백 제거) → (kind, 제목, 구분, 중요도 1~3). 같은 발표의 세부 항목은 대표 하나만 쓴다.
YAHOO_EVENTS = {
    ("US", "CPI YY, NSA"): ("us_cpi", "미국 소비자물가(CPI)", "us", 3),
    ("US", "Non-Farm Payrolls"): ("us_jobs", "미국 고용보고서 (비농업 고용)", "us", 3),
    ("US", "PPI Final Demand YY"): ("us_ppi", "미국 생산자물가(PPI)", "us", 2),
    ("US", "PCE Price Index YY"): ("us_pce", "미국 PCE 물가", "us", 2),
    ("US", "GDP Advance"): ("us_gdp", "미국 GDP (속보치)", "us", 2),
    ("US", "ISM Manufacturing PMI"): ("us_ism", "미국 ISM 제조업지수", "us", 2),
    ("US", "Retail Sales MM"): ("us_retail", "미국 소매판매", "us", 1),
    ("KR", "Export Growth Prelim"): ("kr_export_m", "한국 월간 수출입 동향", "kr", 3),
    ("KR", "CPI Growth YY"): ("kr_cpi", "한국 소비자물가", "kr", 2),
    ("KR", "Industrial Output YY"): ("kr_ip", "한국 산업활동동향", "kr", 1),
    ("KR", "Current Account Bal NSA"): ("kr_ca", "한국 경상수지", "kr", 1),
    ("KR", "Unemployment Rate"): ("kr_jobs", "한국 고용동향", "kr", 1),
}

MONTHS = "January February March April May June July August September October November December".split()
MONTH_NO = {m[:3].lower(): i + 1 for i, m in enumerate(MONTHS)}


def _event(kind, title, region, stars, when, url, time_known=True, prev=None, expected=None, note=None):
    return {"kind": kind, "title": title, "region": region, "stars": stars, "at": when.astimezone(KST).isoformat(),
            "time_known": time_known, "url": url, "prev": prev, "expected": expected, "note": note}


def _page(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "replace")


def _text(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text))


def _fmt(v):
    if v is None or pd.isna(v):
        return None
    return f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"


def yahoo_economic(start, end):
    """Yahoo 경제 일정표에서 고른 미국·한국 지표."""
    cal = yf.Calendars(start=start.isoformat(), end=end.isoformat())
    frames = []
    for offset in range(0, 5000, 100):
        f = cal.get_economic_events_calendar(start=start.isoformat(), end=end.isoformat(), limit=100, offset=offset)
        if f.empty:
            break
        frames.append(f)
        if len(f) < 100:
            break
    out = []
    for name, row in (pd.concat(frames) if frames else pd.DataFrame()).iterrows():
        key = (str(row.get("Region", "")).strip().upper(), str(name).replace("*", "").strip())
        spec = YAHOO_EVENTS.get(key)
        when = pd.Timestamp(row.get("Event Time"))
        if not spec or pd.isna(when):
            continue
        kind, title, region, stars = spec
        when = when.tz_localize("UTC") if when.tzinfo is None else when
        period = str(row.get("For") or "").strip()
        out.append(_event(kind, f"{title}{f' ({period})' if period and period != 'nan' else ''}", region, stars,
                          when.to_pydatetime(), YAHOO_URL, prev=_fmt(row.get("Last")), expected=_fmt(row.get("Expected"))))
    return out


def fomc():
    """연준 FOMC: 이틀 회의의 둘째 날 14:00(미국 동부) 결정 발표."""
    text = _text(_page(FED_URL))
    heads = list(re.finditer(r"\b(\d{4}) FOMC Meetings\b", text))
    month = "|".join(MONTHS)
    out = []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        for m in re.finditer(rf"\b({month})(?:/({month}))?\s+(\d{{1,2}})\s*-\s*(\d{{1,2}})\*?", body):
            mon = m.group(2) or m.group(1)
            when = datetime(int(h.group(1)), MONTH_NO[mon[:3].lower()], int(m.group(4)), 14, 0, tzinfo=EASTERN)
            out.append(_event("fomc", "FOMC 금리 결정", "us", 3, when, FED_URL))
    return out


def boj():
    """일본은행 금융정책결정회의: 둘째 날 결정 (발표 시각은 정해져 있지 않다)."""
    html_text = _page(BOJ_URL)
    heads = list(re.finditer(r"<h2\b[^>]*>\s*(\d{4})\s*</h2>", html_text, re.I))
    mon = "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS) + "|Sept"
    pat = re.compile(rf"^({mon})\.?\s+\d{{1,2}}\s*\([^)]*\)\s*,\s*(?:({mon})\.?\s+)?(\d{{1,2}})\s*\(", re.I)
    out = []
    for i, h in enumerate(heads):
        chunk = html_text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(html_text)]
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", chunk, re.I | re.S):
            cells = [_text(c).strip() for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.I | re.S)]
            m = pat.match(cells[0]) if cells else None
            if m:
                when = datetime(int(h.group(1)), MONTH_NO[(m.group(2) or m.group(1))[:3].lower()], int(m.group(3)), 12, 0, tzinfo=JAPAN)
                out.append(_event("boj", "일본은행 금리 결정", "jp", 2, when, BOJ_URL, time_known=False))
    return out


def bok(years):
    """한국은행 통화정책방향 결정회의 (금통위). 연도별 페이지에 회의일이 있다."""
    out = []
    for year in years:
        text = _text(_page(BOK_URL.format(year=year)))
        for mon, day in re.findall(r"(\d{1,2})월\s?(\d{1,2})일\s?\([월화수목금토일]\)", text):
            when = datetime(year, int(mon), int(day), 9, 0, tzinfo=KST)
            out.append(_event("bok", "한국은행 기준금리 결정 (금통위)", "kr", 3, when, BOK_URL.format(year=year), time_known=False))
    return out


EARNINGS = [  # (Yahoo 코드, 제목, 중요도, 미국 종목이면 장 마감 후 발표가 일반적)
    ("005930.KS", "삼성전자 실적 발표", 3, False),
    ("000660.KS", "SK하이닉스 실적 발표", 3, False),
    ("MU", "마이크론 실적 발표", 2, True),
    ("NVDA", "엔비디아 실적 발표", 2, True),
]


def earnings():
    out = []
    for code, title, stars, us in EARNINGS:
        cal = yf.Ticker(code).calendar or {}
        for d in (cal.get("Earnings Date") or [])[:1]:
            zone = EASTERN if us else KST
            when = datetime.combine(d, time(16, 0) if us else time(9, 0), tzinfo=zone)
            out.append(_event(f"earn_{code}", title, "co", stars, when,
                              f"https://finance.yahoo.com/quote/{code}", time_known=False,
                              note="미국 장 마감 후 발표가 일반적 · 날짜는 Yahoo 예정일" if us else "날짜는 Yahoo 예정일"))
    return out


def _next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rule_events(start, end):
    """규칙으로 정해지는 국내 일정. 공휴일은 반영하지 않고 주말만 다음 평일로 미룬다."""
    out = []
    month = date(start.year, start.month, 1)
    while month <= end:
        y, m = month.year, month.month
        # 코스피200 옵션 만기: 매월 둘째 목요일. 3·6·9·12월은 선물·옵션 동시만기.
        first_thu = month + timedelta(days=(3 - month.weekday()) % 7)
        expiry = first_thu + timedelta(days=7)
        if m in (3, 6, 9, 12):
            out.append(_event("expiry", "선물·옵션 동시만기", "mk", 2, datetime.combine(expiry, time(15, 20), KST),
                              "https://www.krx.co.kr", note="장 마감 무렵 변동성 확대 주의"))
        else:
            out.append(_event("expiry", "코스피200 옵션 만기", "mk", 1, datetime.combine(expiry, time(15, 20), KST),
                              "https://www.krx.co.kr"))
        # 관세청 1~10일 · 1~20일 수출 잠정치 (오전 9시). 반도체 수출을 가장 먼저 볼 수 있는 숫자.
        out.append(_event("kr_export_10", "한국 1~10일 수출 (관세청)", "kr", 2,
                          datetime.combine(_next_weekday(date(y, m, 11)), time(9, 0), KST), "https://www.customs.go.kr"))
        out.append(_event("kr_export_20", "한국 1~20일 수출 (관세청)", "kr", 3,
                          datetime.combine(_next_weekday(date(y, m, 21)), time(9, 0), KST), "https://www.customs.go.kr"))
        # 산업부 월간 수출입 동향: 매월 1일 오전 9시 (휴일이어도 발표하는 경우가 많아 그대로 둔다)
        out.append(_event("kr_export_m", "한국 월간 수출입 동향", "kr", 3, datetime.combine(month, time(9, 0), KST),
                          "https://www.motie.go.kr"))
        # 정기보고서 제출 기한: 분기·반기 45일, 사업보고서 90일. 잉여현금흐름 카드가 이때쯤 갱신된다.
        for mm, dd, name in [(3, 31, "사업보고서"), (5, 15, "1분기 보고서"), (8, 14, "반기 보고서"), (11, 14, "3분기 보고서")]:
            if m == mm:
                out.append(_event("filing", f"{name} 제출 기한", "co", 1,
                                  datetime.combine(_next_weekday(date(y, mm, dd)), time(18, 0), KST),
                                  "https://dart.fss.or.kr", time_known=False, note="삼성전자·하이닉스 잉여현금흐름 카드 갱신"))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def collect(today=None):
    """오늘부터 HORIZON_DAYS일 안의 일정. 같은 일정이 여러 출처에 있으면 Yahoo(직전 값 있음)를 남긴다."""
    now = datetime.now(KST)
    today = today or now.date()
    end = today + timedelta(days=HORIZON_DAYS)
    errors = []
    events = []
    sources = [
        ("Yahoo 경제 일정표", lambda: yahoo_economic(today, end + timedelta(days=1))),
        ("연준 FOMC 일정", fomc),
        ("한국은행 금통위 일정", lambda: bok(sorted({today.year, end.year}))),
        ("일본은행 회의 일정", boj),
        ("실적 발표일", earnings),
        ("규칙 일정", lambda: rule_events(today, end)),
    ]
    for name, fetch in sources:
        try:
            events += fetch()
        except Exception as e:  # 한 출처가 실패해도 나머지는 보여 준다
            errors.append(f"{name}을(를) 받지 못했습니다 ({e.__class__.__name__})")
    keep = {}
    for e in events:
        d = datetime.fromisoformat(e["at"]).date()
        if not today <= d <= end:
            continue
        key = (e["kind"], d)
        if key not in keep or (e["url"] == YAHOO_URL and keep[key]["url"] != YAHOO_URL):
            keep[key] = e
    out = sorted(keep.values(), key=lambda e: (e["at"], -e["stars"]))
    return {"events": out, "errors": errors, "today": today.isoformat(), "horizon": HORIZON_DAYS}
