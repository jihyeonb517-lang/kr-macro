"""다가오는 일정: 경제지표 · 중앙은행 · 기업 실적 · 국내 시장 규칙 일정.

출처마다 따로 받고, 한 곳이 실패해도 나머지 일정은 보여 준다.
- Yahoo 경제 일정표: 미국·한국·중국·대만·일본·유럽 지표 (발표 시각, 결과·직전 값). 약 4주 앞까지만 준다.
- 연준·한국은행·일본은행 공식 일정 페이지: 금리 결정 (다음 해 일정을 올리면 자동으로 들어온다).
- Yahoo 종목 정보: 다음 실적 발표일·추정치, 이번 달에 지난 실적.
- 규칙: 옵션 만기, 지수 변경, 휴장일, 관세청 10일·20일 수출, 산업부 월간 수출입, 정기보고서 제출 기한.
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

# 구분: 미국 · 한국 · 중국대만 · 일본 · 유럽 · 기업 · 시장(국내외 시장 규칙 일정)
REGIONS = {"us": "미국", "kr": "한국", "cn": "중국·대만", "jp": "일본", "eu": "유럽", "co": "기업", "mk": "시장"}

# Yahoo 지표 이름(끝의 * 와 공백 제거) → (kind, 제목, 구분, 중요도 1~3, 단위). 같은 발표의 세부 항목은 YAHOO_DETAILS로 묶는다.
YAHOO_EVENTS = {
    ("US", "FOMC Decision Fed Funds Tgt Rate"): ("fomc", "FOMC 금리 결정", "us", 3, "%"),
    ("US", "CPI YY, NSA"): ("us_cpi", "미국 소비자물가(CPI)", "us", 3, "%"),
    ("US", "Non-Farm Payrolls"): ("us_jobs", "미국 고용보고서 (비농업 고용)", "us", 3, " 천 명"),
    ("US", "PPI Final Demand YY"): ("us_ppi", "미국 생산자물가(PPI)", "us", 2, "%"),
    ("US", "PCE Price Index YY"): ("us_pce", "미국 PCE 물가", "us", 2, "%"),
    ("US", "GDP Advance"): ("us_gdp", "미국 GDP (속보치)", "us", 2, "%"),
    ("US", "GDP Final"): ("us_gdp_f", "미국 GDP (확정치)", "us", 1, "%"),
    ("US", "ISM Manufacturing PMI"): ("us_ism", "미국 ISM 제조업지수", "us", 2, ""),
    ("US", "ISM N-Mfg PMI"): ("us_ism_s", "미국 ISM 서비스업지수", "us", 2, ""),
    ("US", "ADP National Employment"): ("us_adp", "미국 ADP 민간 고용", "us", 1, " 천 명"),
    ("US", "Initial Jobless Clm"): ("us_claims", "미국 신규 실업수당 청구", "us", 1, " 천 건"),
    ("US", "Retail Sales MM"): ("us_retail", "미국 소매판매", "us", 2, "%"),
    ("US", "Consumer Confidence"): ("us_conf", "미국 컨퍼런스보드 소비자신뢰", "us", 1, ""),
    ("US", "U Mich Sentiment Prelim"): ("us_umich", "미국 미시간대 소비심리 (예비)", "us", 1, ""),
    ("US", "Durable Goods"): ("us_durable", "미국 내구재 주문", "us", 1, "%"),
    ("US", "Industrial Production MM"): ("us_ip", "미국 산업생산", "us", 1, "%"),
    ("KR", "Bank of Korea Base Rate"): ("bok", "한국은행 기준금리 결정 (금통위)", "kr", 3, "%"),
    ("KR", "Export Growth Prelim"): ("kr_export_m", "한국 월간 수출입 동향", "kr", 3, "%"),
    ("KR", "GDP Growth QQ Advance"): ("kr_gdp", "한국 GDP (속보치)", "kr", 2, "%"),
    ("KR", "CPI Growth YY"): ("kr_cpi", "한국 소비자물가", "kr", 2, "%"),
    ("KR", "PPI Growth YY"): ("kr_ppi", "한국 생산자물가", "kr", 1, "%"),
    ("KR", "Consumer Sentiment Ind"): ("kr_csi", "한국 소비자심리지수", "kr", 1, ""),
    ("KR", "Industrial Output YY"): ("kr_ip", "한국 산업활동동향", "kr", 1, "%"),
    ("KR", "Current Account Bal NSA"): ("kr_ca", "한국 경상수지", "kr", 1, " 십억 달러"),
    ("KR", "FX Reserves"): ("kr_fx", "한국 외환보유액", "kr", 1, " 십억 달러"),
    ("KR", "Unemployment Rate"): ("kr_jobs", "한국 고용동향", "kr", 1, "%"),
    ("CN", "NBS Manufacturing PMI"): ("cn_pmi", "중국 제조업 PMI", "cn", 2, ""),
    ("CN", "Exports YY"): ("cn_trade", "중국 수출입", "cn", 2, "%"),
    ("CN", "CPI YY"): ("cn_cpi", "중국 소비자물가", "cn", 1, "%"),
    ("CN", "GDP YY"): ("cn_gdp", "중국 GDP", "cn", 2, "%"),
    ("CN", "Industrial Output YY"): ("cn_act", "중국 산업생산·소매판매", "cn", 1, "%"),
    ("TW", "Exports of Goods"): ("tw_export", "대만 수출", "cn", 2, " 십억 달러"),
    ("TW", "Export Orders YY"): ("tw_orders", "대만 수출주문", "cn", 2, "%"),
    ("JP", "CPI, Core Nationwide YY"): ("jp_cpi", "일본 근원 소비자물가", "jp", 1, "%"),
    ("JP", "GDP QQ"): ("jp_gdp", "일본 GDP", "jp", 1, "%"),
    ("JP", "Exports YY"): ("jp_trade", "일본 수출", "jp", 1, "%"),
    ("JP", "Tankan Big Mf Idx"): ("jp_tankan", "일본 단칸 (대기업 제조업)", "jp", 2, ""),
    ("EU", "ECB Refinancing Rate"): ("ecb", "ECB 금리 결정", "eu", 2, "%"),
    ("EU", "HICP Flash YY"): ("eu_cpi", "유로존 소비자물가 (속보)", "eu", 1, "%"),
}

# 같은 발표에 함께 나오는 세부 수치 → (묶일 kind, 표시 이름, 단위)
YAHOO_DETAILS = {
    ("US", "Core CPI YY, NSA"): ("us_cpi", "근원 CPI 전년비", "%"),
    ("US", "CPI MM, SA"): ("us_cpi", "CPI 전월비", "%"),
    ("US", "Core CPI MM, SA"): ("us_cpi", "근원 CPI 전월비", "%"),
    ("US", "Unemployment Rate"): ("us_jobs", "실업률", "%"),
    ("US", "Average Earnings MM"): ("us_jobs", "시간당 임금 전월비", "%"),
    ("US", "Core PCE Price Index YY"): ("us_pce", "근원 PCE 전년비", "%"),
    ("US", "Core PCE Price Index MM"): ("us_pce", "근원 PCE 전월비", "%"),
    ("US", "PPI exFood/Energy YY"): ("us_ppi", "근원 PPI 전년비", "%"),
    ("US", "ISM Mfg Prices Paid"): ("us_ism", "지불가격", ""),
    ("US", "ISM Manuf New Orders Idx"): ("us_ism", "신규주문", ""),
    ("US", "ISM N-Mfg Price Paid Idx"): ("us_ism_s", "지불가격", ""),
    ("US", "ISM N-Mfg New Orders Idx"): ("us_ism_s", "신규주문", ""),
    ("US", "Retail Control"): ("us_retail", "핵심 소매판매(컨트롤그룹)", "%"),
    ("US", "Nondefe Cap Ex-Air"): ("us_durable", "핵심 자본재 주문", "%"),
    ("US", "U Mich 1Yr Inf Prelim"): ("us_umich", "1년 기대인플레", "%"),
    ("US", "U Mich 5-Yr Inf Prelim"): ("us_umich", "5년 기대인플레", "%"),
    ("KR", "Import Growth Prelim"): ("kr_export_m", "수입 증가율", "%"),
    ("KR", "Trade Balance Prelim"): ("kr_export_m", "무역수지", " 십억 달러"),
    ("KR", "GDP Growth YY Advance"): ("kr_gdp", "전년비", "%"),
    ("KR", "CPI Growth MM"): ("kr_cpi", "전월비", "%"),
    ("KR", "Industrial Output Growth"): ("kr_ip", "전월비", "%"),
    ("CN", "NBS Non-Mfg PMI"): ("cn_pmi", "비제조업 PMI", ""),
    ("CN", "Imports YY"): ("cn_trade", "수입 증가율", "%"),
    ("CN", "Trade Balance USD"): ("cn_trade", "무역수지", " 십억 달러"),
    ("CN", "PPI YY"): ("cn_cpi", "생산자물가", "%"),
    ("CN", "GDP QQ SA"): ("cn_gdp", "전분기비", "%"),
    ("CN", "Retail Sales YY"): ("cn_act", "소매판매", "%"),
    ("TW", "Imports of Goods"): ("tw_export", "수입", " 십억 달러"),
    ("JP", "GDP QQ Annualised"): ("jp_gdp", "연율", "%"),
    ("JP", "Imports YY"): ("jp_trade", "수입 증가율", "%"),
}

# 왜 보는 일정인지 한 줄로. 기업 실적은 EARNINGS에 따로 적는다.
WHY = {
    "fomc": "미국 금리 경로의 기준. 달러·원화와 외국인 수급 방향을 바꾼다.",
    "fomc_min": "결정 3주 뒤 공개되는 회의 내용. 위원들의 금리 인하·인상 온도를 본다.",
    "us_cpi": "금리 기대를 가장 크게 흔드는 물가 지표. 근원 물가를 먼저 본다.",
    "us_jobs": "미국 경기와 금리 기대를 함께 움직이는 월간 최대 지표.",
    "us_ppi": "CPI보다 앞서는 기업 쪽 물가. 일부 항목은 PCE 계산에도 들어간다.",
    "us_pce": "연준이 목표로 삼는 물가 지표.",
    "us_gdp": "미국 성장률의 첫 숫자. 소비와 설비투자 구성을 함께 본다.",
    "us_gdp_f": "분기 성장률 최종 수정치. 시장 반응은 대체로 작다.",
    "us_ism": "미국 제조업 경기. 신규주문과 지불가격이 함께 중요하다.",
    "us_ism_s": "미국 경제의 대부분인 서비스업 경기.",
    "us_adp": "고용보고서 이틀 전에 나오는 민간 고용 참고치.",
    "us_claims": "매주 나오는 고용 온도계. 빠르게 늘면 경기 둔화 신호.",
    "us_retail": "미국 소비의 강도. 컨트롤그룹이 GDP 소비와 가장 가깝다.",
    "us_conf": "소비자가 느끼는 고용·경기 체감.",
    "us_umich": "소비 심리와 기대인플레. 연준이 기대인플레를 지켜본다.",
    "us_durable": "설비투자 선행 지표. 핵심 자본재 주문을 본다.",
    "us_ip": "미국 공장·광공업 생산 수준.",
    "bok": "국내 기준금리. 원화와 채권, 내수 업종에 바로 영향을 준다.",
    "boj": "엔화와 엔캐리 자금 흐름에 영향을 준다. 시간은 회의 뒤 정해진다.",
    "ecb": "유럽 금리 경로. 달러 방향에 간접적으로 영향을 준다.",
    "kr_export_m": "한국 경기와 코스피 이익의 가장 빠른 월간 지표. 반도체 수출을 본다.",
    "kr_export_10": "그달 초 수출의 첫 숫자. 조업일수가 달라 일평균을 함께 본다.",
    "kr_export_20": "그달 수출 흐름을 가늠하는 숫자. 반도체 증가율이 시장의 관심사.",
    "kr_gdp": "한국 분기 성장률의 첫 숫자. 수출과 설비투자 기여를 본다.",
    "kr_cpi": "금통위 판단의 기준이 되는 국내 물가.",
    "kr_ppi": "소비자물가에 앞서 나타나는 생산 단계 물가.",
    "kr_csi": "가계 체감 경기. 100이 장기 평균.",
    "kr_ip": "생산·소비·투자를 한 번에 보는 월간 실물 지표. 반도체 생산을 본다.",
    "kr_ca": "달러 유입의 크기. 원화 흐름의 바탕이 된다.",
    "kr_fx": "외환 방어 여력.",
    "kr_jobs": "내수 체력을 보는 국내 고용 지표.",
    "cn_pmi": "한국 최대 수출 시장의 경기. 50이 확장과 위축의 경계.",
    "cn_trade": "중국 교역 흐름. 대중 수출과 소재 업종 업황을 가늠한다.",
    "cn_cpi": "중국 내수 수요와 디플레이션 압력.",
    "cn_gdp": "중국 분기 성장률.",
    "cn_act": "중국 공장 가동과 소비 강도.",
    "tw_export": "TSMC가 있는 대만의 수출. 반도체 업황 비교 지표.",
    "tw_orders": "대만 전자 수요의 선행 지표.",
    "jp_cpi": "일본은행 금리 판단의 기준.",
    "jp_gdp": "일본 분기 성장률.",
    "jp_trade": "엔화와 일본 제조업 흐름.",
    "jp_tankan": "일본 대기업 체감 경기. 일본은행이 중시한다.",
    "eu_cpi": "ECB 판단의 기준이 되는 유로존 물가.",
    "expiry": "옵션 만기일. 장 마감 무렵 프로그램 매매로 지수가 흔들릴 수 있다.",
    "us_expiry": "미국 옵션 만기. 다음 거래일 국내 증시 변동성에 영향을 줄 수 있다.",
    "k200_change": "구성 종목 교체가 반영되는 날. 지수 추종 자금이 편입·편출 종목을 사고판다.",
    "tsmc_sales": "AI 반도체 수요를 매달 확인할 수 있는 숫자. 삼성전자·하이닉스 흐름과 함께 본다.",
    "filing": "기업 실적 공시 마감. 잉여현금흐름 카드가 이때쯤 갱신된다.",
    "kr_closed": "국내 증시가 열리지 않는다. 해외 시장 변화는 다음 거래일에 반영된다.",
    "us_closed": "미국 증시가 열리지 않는다. 그날 밤 해외 수급 재료가 줄어든다.",
}

# 휴장일. 음력 공휴일과 대체공휴일이 있어 표로 둔다. 해가 바뀌면 다음 해 일정을 더한다.
KR_CLOSED = {
    "2026-12-25": "크리스마스", "2026-12-31": "연말 휴장",
    "2027-01-01": "신정", "2027-02-08": "설 연휴", "2027-02-09": "설 대체공휴일", "2027-03-01": "삼일절",
    "2027-05-05": "어린이날", "2027-05-13": "부처님오신날", "2027-08-16": "광복절 대체공휴일",
    "2027-09-14": "추석 연휴", "2027-09-15": "추석", "2027-09-16": "추석 연휴",
    "2027-10-04": "개천절 대체공휴일", "2027-10-11": "한글날 대체공휴일", "2027-12-31": "연말 휴장",
}
US_CLOSED = {
    "2026-11-26": "추수감사절", "2026-12-25": "크리스마스",
    "2027-01-01": "신정", "2027-01-18": "마틴 루터 킹 데이", "2027-02-15": "대통령의 날", "2027-03-26": "성금요일",
    "2027-05-31": "메모리얼 데이", "2027-06-18": "준틴스 대체휴일", "2027-07-05": "독립기념일 대체휴일",
    "2027-09-06": "노동절", "2027-11-25": "추수감사절", "2027-12-24": "크리스마스 대체휴일",
}

MONTHS = "January February March April May June July August September October November December".split()
MONTH_NO = {m[:3].lower(): i + 1 for i, m in enumerate(MONTHS)}
PERIODS = {m[:3]: f"{i + 1}월" for i, m in enumerate(MONTHS)} | {f"Q{q}": f"{q}분기" for q in range(1, 5)}


def _event(kind, title, region, stars, when, url, time_known=True, prev=None, expected=None, note=None,
           actual=None, unit="", items=None, why=None):
    return {"kind": kind, "title": title, "region": region, "stars": stars, "at": when.astimezone(KST).isoformat(),
            "time_known": time_known, "url": url, "prev": prev, "expected": expected, "note": note,
            "actual": actual, "unit": unit, "items": items or [], "why": why or WHY.get(kind)}


def _page(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "replace")


def _text(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text))


def _fmt(v):
    if v is None or pd.isna(v):
        return None
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    s = f"{v:,.2f}"
    return s[:-1] if s.endswith("0") else s


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
    rows = []
    for name, row in (pd.concat(frames) if frames else pd.DataFrame()).iterrows():
        when = pd.Timestamp(row.get("Event Time"))
        if pd.isna(when):
            continue
        when = when.tz_localize("UTC") if when.tzinfo is None else when
        key = (str(row.get("Region", "")).strip().upper(), str(name).replace("*", "").strip())
        rows.append((key, row, when.to_pydatetime()))
    out = {}
    for key, row, when in rows:
        spec = YAHOO_EVENTS.get(key)
        if not spec:
            continue
        kind, title, region, stars, unit = spec
        period = str(row.get("For") or "").strip()
        period = PERIODS.get(period, period)
        label = f" ({period})" if period and period not in ("nan", "None", "N/A") else ""
        day = when.astimezone(KST).date()
        out[(kind, day)] = _event(kind, f"{title}{label}", region, stars, when, YAHOO_URL, prev=_fmt(row.get("Last")),
                                  expected=_fmt(row.get("Expected")), actual=_fmt(row.get("Actual")), unit=unit)
        if kind == "fomc":  # Yahoo의 직전 값은 목표 범위 중간값이라 결과(상단)와 기준이 달라 뺀다
            out[(kind, day)].update(prev=None, note="결과는 목표 범위 상단")
    for key, row, when in rows:
        spec = YAHOO_DETAILS.get(key)
        event = out.get((spec[0], when.astimezone(KST).date())) if spec else None
        if event:
            event["items"].append({"label": spec[1], "unit": spec[2], "actual": _fmt(row.get("Actual")), "prev": _fmt(row.get("Last"))})
    return list(out.values())


def fomc():
    """연준 FOMC: 이틀 회의의 둘째 날 14:00(미국 동부) 결정 발표."""
    text = _text(_page(FED_URL))
    heads = list(re.finditer(r"\b(\d{4}) FOMC Meetings\b", text))
    month = "|".join(MONTHS)
    out = []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        for m in re.finditer(rf"\b({month})(?:/({month}))?\s+(\d{{1,2}})\s*-\s*(\d{{1,2}})(\*?)", body):
            mon = m.group(2) or m.group(1)
            when = datetime(int(h.group(1)), MONTH_NO[mon[:3].lower()], int(m.group(4)), 14, 0, tzinfo=EASTERN)
            out.append(_event("fomc", "FOMC 금리 결정", "us", 3, when, FED_URL,
                              note="점도표(경제전망) 함께 발표" if m.group(5) else None))
            out.append(_event("fomc_min", "FOMC 의사록", "us", 2, when + timedelta(days=21), FED_URL))
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


EARNINGS = [  # (Yahoo 코드, 제목, 중요도, 미국 장 마감 뒤 발표 여부, 매출 통화, 왜 보나)
    ("005930.KS", "삼성전자 실적 발표", 3, False, "원", "메모리 가격과 파운드리 흐름이 코스피 이익 전망을 좌우한다."),
    ("000660.KS", "SK하이닉스 실적 발표", 3, False, "원", "HBM과 메모리 수익성을 확인하는 핵심 실적."),
    ("TSM", "TSMC 실적 발표", 3, False, None, "AI 반도체 수요와 설비투자 계획의 기준."),
    ("ASML", "ASML 실적 발표", 2, False, None, "장비 수주로 반도체 설비투자 사이클을 본다."),
    ("MU", "마이크론 실적 발표", 3, True, "$", "메모리 업체 중 가장 먼저 나오는 실적. 삼성전자·하이닉스의 선행 지표."),
    ("NVDA", "엔비디아 실적 발표", 3, True, "$", "HBM 수요의 최종 고객. 데이터센터 매출과 전망을 본다."),
    ("AVGO", "브로드컴 실적 발표", 2, True, "$", "맞춤형 AI 칩 수요를 확인한다."),
    ("AMD", "AMD 실적 발표", 2, True, "$", "서버·AI 칩 수요를 확인한다."),
    ("MSFT", "마이크로소프트 실적 발표", 2, True, "$", "AI 설비투자 규모가 메모리 수요로 이어진다."),
    ("GOOGL", "알파벳 실적 발표", 2, True, "$", "AI 설비투자 규모가 메모리 수요로 이어진다."),
    ("AMZN", "아마존 실적 발표", 2, True, "$", "AI 설비투자 규모가 메모리 수요로 이어진다."),
    ("META", "메타 실적 발표", 2, True, "$", "AI 설비투자 규모가 메모리 수요로 이어진다."),
    ("AAPL", "애플 실적 발표", 1, True, "$", "스마트폰 수요와 메모리 탑재량을 본다."),
]


def _money(v, cur):
    if v is None or pd.isna(v) or not cur:
        return None
    return f"{v / 1e12:,.1f}조 원" if cur == "원" else f"${v / 1e9:,.1f}B"


def earnings(start, today):
    out = []
    for code, title, stars, us, cur, why in EARNINGS:
        ticker = yf.Ticker(code)
        zone = EASTERN if us else KST
        made = dict(kind=f"earn_{code}", title=title, region="co", stars=stars,
                    url=f"https://finance.yahoo.com/quote/{code}", why=why)
        clock = time(16, 0) if us else time(9, 0)
        cal = ticker.calendar or {}
        for d in [d for d in (cal.get("Earnings Date") or [])[:1] if d >= today]:  # 지난 실적은 아래 실제 발표일로 넣는다
            items = [{"label": "주당순이익(EPS) 추정", "unit": "", "actual": _fmt(cal.get("Earnings Average")), "prev": None}]
            sales = _money(cal.get("Revenue Average"), cur)
            if sales:
                items.append({"label": "매출 추정", "unit": "", "actual": sales, "prev": None})
            out.append(dict(_event(when=datetime.combine(d, clock, tzinfo=zone), items=items, time_known=False,
                              note="미국 현지 예정일 기준 · 한국 날짜는 장 마감 후 발표 가정" if us else "날짜는 Yahoo 예정일", **made), earnings_status="estimate"))
        try:
            past = ticker.get_earnings_dates(limit=6)
        except Exception:  # 지난 실적은 덧붙이는 정보라 없어도 예정일은 보여 준다
            continue
        for at, row in (past if past is not None else pd.DataFrame()).iterrows():
            day = at.tz_convert(zone).date()
            if day < start or pd.isna(row.get("Reported EPS")):
                continue
            items = [{"label": "주당순이익(EPS)", "unit": "", "actual": _fmt(row.get("Reported EPS")), "prev": None},
                     {"label": "추정치", "unit": "", "actual": _fmt(row.get("EPS Estimate")), "prev": None},
                     {"label": "추정 대비", "unit": "%", "actual": _fmt(row.get("Surprise(%)")), "prev": None}]
            timestamp = at.to_pydatetime()
            known = timestamp.astimezone(zone).time() != time(0)
            out.append(dict(_event(when=timestamp if known else datetime.combine(day, clock, tzinfo=zone),
                                   time_known=known, items=items, **made), earnings_status="reported"))
    return out


def _open(d):
    return d.weekday() < 5 and d.isoformat() not in KR_CLOSED


def _next_open(d):
    while not _open(d):
        d += timedelta(days=1)
    return d


def _prev_open(d):
    while not _open(d):
        d -= timedelta(days=1)
    return d


def rule_events(start, end):
    """규칙으로 정해지는 일정. 휴장일이면 만기는 앞 거래일로, 나머지는 다음 거래일로 옮긴다."""
    out = []
    month = date(start.year, start.month, 1)
    while month <= end:
        y, m = month.year, month.month
        quarter = m in (3, 6, 9, 12)
        # 코스피200 옵션 만기: 매월 둘째 목요일. 3·6·9·12월은 선물·옵션 동시만기.
        first_thu = month + timedelta(days=(3 - month.weekday()) % 7)
        expiry = _prev_open(first_thu + timedelta(days=7))
        out.append(_event("expiry", "선물·옵션 동시만기" if quarter else "코스피200 옵션 만기", "mk", 2 if quarter else 1,
                          datetime.combine(expiry, time(15, 20), KST), "https://www.krx.co.kr"))
        # 코스피200 정기변경: 6·12월 동시만기 다음 거래일부터 적용.
        if m in (6, 12):
            out.append(_event("k200_change", "코스피200 정기변경 적용", "mk", 2,
                              datetime.combine(_next_open(expiry + timedelta(days=1)), time(9, 0), KST),
                              "https://www.krx.co.kr", time_known=False))
        # 미국 옵션 만기: 매월 셋째 금요일 (한국 시각으로는 다음 날 새벽). 3·6·9·12월은 쿼드러플 위칭.
        first_fri = month + timedelta(days=(4 - month.weekday()) % 7)
        out.append(_event("us_expiry", "미국 쿼드러플 위칭" if quarter else "미국 옵션 만기", "mk", 2 if quarter else 1,
                          datetime.combine(first_fri + timedelta(days=14), time(16, 0), EASTERN),
                          "https://www.cboe.com"))
        # 관세청 1~10일 · 1~20일 수출 잠정치 (오전 9시). 반도체 수출을 가장 먼저 볼 수 있는 숫자.
        out.append(_event("kr_export_10", "한국 1~10일 수출 (관세청)", "kr", 2,
                          datetime.combine(_next_open(date(y, m, 11)), time(9, 0), KST), "https://www.customs.go.kr"))
        out.append(_event("kr_export_20", "한국 1~20일 수출 (관세청)", "kr", 3,
                          datetime.combine(_next_open(date(y, m, 21)), time(9, 0), KST), "https://www.customs.go.kr"))
        # 산업부 월간 수출입 동향: 매월 1일 오전 9시 (휴일이어도 발표하는 경우가 많아 그대로 둔다)
        out.append(_event("kr_export_m", "한국 월간 수출입 동향", "kr", 3, datetime.combine(month, time(9, 0), KST),
                          "https://www.motie.go.kr"))
        # TSMC 월매출: 매월 10일 무렵 공시.
        out.append(_event("tsmc_sales", "TSMC 월매출", "co", 2, datetime.combine(date(y, m, 10), time(14, 0), KST),
                          "https://investor.tsmc.com", time_known=False, note="매월 10일 무렵 · 날짜 추정"))
        # 삼성전자 잠정실적: 분기 첫 달 7일 무렵.
        if m in (1, 4, 7, 10):
            out.append(_event("samsung_prelim", "삼성전자 잠정실적", "co", 3, datetime.combine(_next_open(date(y, m, 7)), time(8, 40), KST),
                              "https://dart.fss.or.kr", time_known=False, note="날짜 추정 · 회사 공시로 확정",
                              why="분기 실적의 첫 숫자. 메모리 업황 판단을 바꾸는 일정."))
        # 정기보고서 제출 기한: 분기·반기 45일, 사업보고서 90일. 잉여현금흐름 카드가 이때쯤 갱신된다.
        for mm, dd, name in [(3, 31, "사업보고서"), (5, 15, "1분기 보고서"), (8, 14, "반기 보고서"), (11, 14, "3분기 보고서")]:
            if m == mm:
                out.append(_event("filing", f"{name} 제출 기한", "co", 1,
                                  datetime.combine(_next_open(date(y, mm, dd)), time(18, 0), KST),
                                  "https://dart.fss.or.kr", time_known=False))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    for table, kind, label, url in ((KR_CLOSED, "kr_closed", "한국 증시 휴장", "https://www.krx.co.kr"),
                                    (US_CLOSED, "us_closed", "미국 증시 휴장", "https://www.nyse.com/markets/hours-calendars")):
        for day, name in table.items():
            out.append(_event(kind, f"{label} · {name}", "mk", 1, datetime.fromisoformat(day + "T09:00:00").replace(tzinfo=KST),
                              url, time_known=False))
    return out


def reconcile_earnings(entries):
    """같은 종목의 인접 날짜에 잡힌 예정·발표 자료를 하나로 묶는다."""
    priority = {"official": 0, "reported": 1, "estimate": 2}
    kept = []
    others = [e for e in entries if "earnings_status" not in e]
    for e in sorted((v for v in entries if "earnings_status" in v),
                    key=lambda v: priority[v["earnings_status"]]):
        day = datetime.fromisoformat(e["at"]).date()
        match = next((v for v in kept if v["kind"] == e["kind"]
                      and abs((datetime.fromisoformat(v["at"]).date()-day).days) <= 1), None)
        if match is None:
            kept.append(e.copy())
        elif not match["items"] and e["earnings_status"] == "reported":
            match["items"] = e["items"]
    return others + kept


def collect(today=None):
    """이달 1일부터 오늘 뒤 HORIZON_DAYS일까지의 일정. 같은 일정이 여러 출처에 있으면 Yahoo(결과·직전 값 있음)를 남긴다."""
    now = datetime.now(KST)
    today = today or now.date()
    first = today.replace(day=1)
    end = today + timedelta(days=HORIZON_DAYS)
    errors = []
    events = []
    sources = [
        ("Yahoo 경제 일정표", lambda: yahoo_economic(first, end + timedelta(days=1))),
        ("연준 FOMC 일정", fomc),
        ("한국은행 금통위 일정", lambda: bok(sorted({today.year, end.year}))),
        ("일본은행 회의 일정", boj),
        ("실적 발표일", lambda: earnings(first, today)),
        ("규칙 일정", lambda: rule_events(first, end)),
    ]
    for name, fetch in sources:
        try:
            events += fetch()
        except Exception as e:  # 한 출처가 실패해도 나머지는 보여 준다
            errors.append(f"{name}을(를) 받지 못했습니다 ({e.__class__.__name__})")
    # 공식 발표 일정. 발표 예정 시각과 실적 설명회 시각을 구별한다.
    nvda_at = datetime(2026, 11, 18, 6, 20, tzinfo=KST)
    if first <= nvda_at.date() <= end:
        events.append(dict(_event("earn_NVDA", "엔비디아 실적 발표", "co", 3, nvda_at,
                   "https://nvidianews.nvidia.com/news/nvidia-sets-conference-call-for-third-quarter-financial-results-6928873",
                   note="회사 공지 · 한국 시간 약 06:20 발표 예정, 07:00 설명회"), earnings_status="official"))
    events = reconcile_earnings(events)
    keep = {}
    for e in events:
        d = datetime.fromisoformat(e["at"]).date()
        if not first <= d <= end:
            continue
        key = (e["kind"], d)
        old = keep.get(key)
        if old is None or (e["url"] == YAHOO_URL and old["url"] != YAHOO_URL):
            keep[key] = e
            old, e = e, old
        if old and e:  # 공식 페이지에만 있는 안내(점도표 등)는 Yahoo 일정에도 남긴다
            old["note"] = old["note"] or e["note"]
    out = sorted(keep.values(), key=lambda e: (e["at"], -e["stars"]))
    return {"events": out, "errors": errors, "today": today.isoformat(), "horizon": HORIZON_DAYS}
