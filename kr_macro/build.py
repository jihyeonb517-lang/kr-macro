"""국장 매크로 대시보드.

indicators.json을 읽어 데이터를 받고, output/ 폴더에 HTML 대시보드를 만든다.
실행: python build.py
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests.adapters
import yfinance as yf

import events
import cosmetics
import dashboard_theme
import dashboard_layout
import company_catalog
import sector_map
import trend_charts
import local_views
import inflation
import stock_flow_map
import industry_exports

warnings.filterwarnings("ignore", category=DeprecationWarning,
                        message="The 'generic' unit for NumPy timedelta is deprecated.*")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
CFG = json.loads((ROOT / "indicators.json").read_text(encoding="utf-8"))
TODAY = pd.Timestamp.now(tz="Asia/Seoul").normalize().tz_localize(None)


def load_env(path):
    """KEY=value lines from .env (API keys live here, never in indicators.json)."""
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV = load_env(ROOT / ".env")

_http_send = requests.adapters.HTTPAdapter.send


def _http_send_with_timeout(self, request, stream=False, timeout=None, **kwargs):
    """pykrx는 응답 대기 시간을 정하지 않아 KRX가 답을 안 하면 끝없이 기다린다. 기본 60초로 끊는다."""
    return _http_send(self, request, stream=stream, timeout=timeout or 60, **kwargs)


requests.adapters.HTTPAdapter.send = _http_send_with_timeout


class MissingKey(Exception):
    """A source needs a key that .env does not have yet; shown as a setup note, not an error."""

KST = "Asia/Seoul"


# ---------------------------------------------------------------- 데이터 받기

def yahoo(tickers, start):
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return pd.DataFrame()
    cache_id = hashlib.sha256(",".join(sorted(tickers)).encode()).hexdigest()[:16]
    cache = OUT / f".yahoo_{cache_id}.csv"
    try:
        d = yf.download(tickers, start=start, progress=False, auto_adjust=True, threads=True)["Close"]
        if d.empty:
            raise ValueError("시세 응답이 비어 있습니다")
    except Exception:
        if not cache.exists():
            raise
        print("  ! Yahoo 수신 실패: 마지막 저장본 사용 (지표별 기준일 확인)")
        d = pd.read_csv(cache, index_col=0, parse_dates=True)
    if isinstance(d, pd.Series):
        d = d.to_frame(tickers[0])
    d.index = pd.to_datetime(d.index)
    if d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    d = d.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    # Futures such as WTI can legitimately trade below zero.
    d = d.dropna(how="all").sort_index()
    d = d[~d.index.duplicated(keep="last")]
    if cache.exists():
        previous = pd.read_csv(cache, index_col=0, parse_dates=True)
        d = d.combine_first(previous).sort_index()
    d = d.reindex(columns=tickers)
    OUT.mkdir(exist_ok=True)
    d.to_csv(cache)
    return d.loc[pd.Timestamp(start):]


_fred_cache = {}
FRED_STARTS = {
    "SOFR": "2021-07-29", "IORB": "2021-07-29",
    "NFCI": "1971-01-01", "STLFSI4": "1993-12-31", "DRTSCILM": "1990-01-01",
}


def fred(sid):
    if sid not in _fred_cache:
        cache = OUT / f".fred_{sid}.csv"
        params = {"id": sid}
        if sid in FRED_STARTS:
            params.update(cosd=FRED_STARTS[sid], coed=TODAY.strftime("%Y-%m-%d"))
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(params)
        try:
            raw = urllib.request.urlopen(url, timeout=60).read().decode()
            df = pd.read_csv(io.StringIO(raw))
            s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(df.iloc[:, 0])).dropna().sort_index()
            if s.empty:
                raise ValueError("FRED 응답이 비어 있습니다")
            OUT.mkdir(exist_ok=True)
            s.to_csv(cache)
        except Exception:
            if not cache.exists():
                raise
            print(f"  ! FRED {sid} 수신 실패: 마지막 저장본 사용")
            s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0].dropna()
        _fred_cache[sid] = s
    return _fred_cache[sid]


@lru_cache(maxsize=None)
def krx_flows(market="KOSPI", first_year=2010):
    """일별 외국인 순매수와 시장 전체 매수 거래대금 (원). output/.krx_flows_*.csv에 저장해 두고
    새로 생긴 날만 받는다. 장중에는 오늘 값이 미완성이라 16시 전에는 오늘을 뺀다."""
    if not (ENV.get("KRX_ID") and ENV.get("KRX_PW")):
        raise MissingKey("KRX 로그인 정보가 .env에 없습니다 (KRX_ID, KRX_PW). 넣으면 다음 실행부터 연결됩니다.")
    os.environ["KRX_ID"], os.environ["KRX_PW"] = ENV["KRX_ID"], ENV["KRX_PW"]
    with contextlib.redirect_stdout(io.StringIO()):  # pykrx의 로그인 안내 문구(계정 ID 포함)를 숨긴다
        from pykrx import stock

    cache = OUT / f".krx_flows_{market}.csv"
    flows = pd.read_csv(cache, index_col=0, parse_dates=True) if cache.exists() else pd.DataFrame(columns=["net", "buy"])
    start = (flows.index.max() - pd.Timedelta(days=10)) if len(flows) else pd.Timestamp(first_year, 1, 1)
    now_kst = pd.Timestamp.now(tz=KST)
    end = now_kst.normalize().tz_localize(None) - (pd.Timedelta(days=0) if now_kst.hour >= 16 else pd.Timedelta(days=1))
    parts = []
    with contextlib.redirect_stdout(io.StringIO()):
        for year in range(start.year, end.year + 1):
            a = max(start, pd.Timestamp(year, 1, 1)).strftime("%Y%m%d")
            b = min(end, pd.Timestamp(year, 12, 31)).strftime("%Y%m%d")
            if a > b:
                continue
            net = stock.get_market_trading_value_by_date(a, b, market)["외국인합계"]
            buy = stock.get_market_trading_value_by_date(a, b, market, on="매수")["전체"]
            parts.append(pd.DataFrame({"net": net, "buy": buy}))
    if parts:
        new = pd.concat(parts)
        flows = pd.concat([flows, new]) if len(flows) else new
        flows = flows[~flows.index.duplicated(keep="last")].sort_index()
        flows.to_csv(cache)
    if flows.empty:
        raise ValueError("KRX에서 투자자별 거래 데이터를 받지 못했습니다")
    return flows.astype(float)


KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
# 금융투자협회 통계(FreeSIS) 표. 값은 백만원 단위.
#   STATSCU0100000060BO 증시자금 추이: TMPV2 투자자예탁금, …, TMPV5 위탁매매 미수금
#   STATSCU0100000070BO 신용공여 잔고: TMPV2 신용거래융자 전체, TMPV3 유가증권, TMPV4 코스닥


def _kofia_chunk(table, a, b):
    body = json.dumps({"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                                    "tmpV45": a, "tmpV46": b, "OBJ_NM": table}}).encode()
    for attempt in range(4):
        # 응답이 가끔 중간에 끊겨 오므로 반년 단위로 받고, 읽지 못하면 다시 받는다.
        try:
            request = urllib.request.Request(KOFIA_URL, data=body, headers={
                "Content-Type": "application/json; charset=UTF-8", "User-Agent": "Mozilla/5.0",
                "Referer": "https://freesis.kofia.or.kr/"})
            return json.loads(urllib.request.urlopen(request, timeout=60).read())["ds1"]
        except (ValueError, OSError):
            time.sleep(1 + attempt)
    raise ValueError(f"금융투자협회 데이터를 받지 못했습니다 ({table} {a}~{b})")


@lru_cache(maxsize=None)
def kofia(table, first_year=2010):
    """금융투자협회 일별 표 (원 단위). output/.kofia_*.csv에 저장해 두고 새로 생긴 날만 받는다."""
    cache = OUT / f".kofia_{table}.csv"
    data = pd.read_csv(cache, index_col=0, parse_dates=True) if cache.exists() else pd.DataFrame()
    start = (data.index.max() - pd.Timedelta(days=10)) if len(data) else pd.Timestamp(first_year, 1, 1)
    rows = []
    for half_start in pd.date_range(start.replace(month=1 if start.month <= 6 else 7, day=1), TODAY, freq="6MS"):
        a = max(start, half_start)
        b = min(TODAY, half_start + pd.DateOffset(months=6) - pd.Timedelta(days=1))
        try:
            rows += _kofia_chunk(table, a.strftime("%Y%m%d"), b.strftime("%Y%m%d"))
        except Exception:
            if data.empty:
                raise
            print("  ! 금융투자협회 수신 실패: 저장된 자료 사용 (기준일 확인)")
            break
    if rows:
        new = pd.DataFrame(rows)
        new = new.set_index(pd.to_datetime(new.pop("TMPV1"), format="%Y%m%d")).astype(float) * 1e6
        data = pd.concat([data, new]) if len(data) else new
        data = data[~data.index.duplicated(keep="last")].sort_index()
        data.to_csv(cache)
    if data.empty:
        raise ValueError(f"금융투자협회 데이터가 비어 있습니다 ({table})")
    return data


def krx_index(code, first="20100101"):
    """KRX 지수 일별 종가·상장시가총액 (code: 1001 코스피, 2001 코스닥). output/.krx_index_*.csv에 저장."""
    if not (ENV.get("KRX_ID") and ENV.get("KRX_PW")):
        raise MissingKey("KRX 로그인 정보가 .env에 없습니다 (KRX_ID, KRX_PW).")
    os.environ["KRX_ID"], os.environ["KRX_PW"] = ENV["KRX_ID"], ENV["KRX_PW"]
    with contextlib.redirect_stdout(io.StringIO()):
        from pykrx import stock
    cache = OUT / f".krx_index_{code}.csv"
    data = pd.read_csv(cache, index_col=0, parse_dates=True) if cache.exists() else pd.DataFrame()
    start = (data.index.max() - pd.Timedelta(days=10)).strftime("%Y%m%d") if len(data) else first
    with contextlib.redirect_stdout(io.StringIO()):
        new = stock.get_index_ohlcv(start, TODAY.strftime("%Y%m%d"), code, name_display=False)
    if len(new):
        new = new.rename(columns={"종가": "close", "상장시가총액": "cap"})[["close", "cap"]].astype(float)
        data = pd.concat([data, new]) if len(data) else new
        data = data[~data.index.duplicated(keep="last")].sort_index()
        data.to_csv(cache)
    return data


def stock_ohlcv(symbol, start="2016-01-01"):
    """일별 시가·고가·저가·종가·거래량 (Yahoo). 실제 거래 가격 — 액면분할만 반영하고 배당 조정은 하지 않는다."""
    cache = OUT / f".stock_{symbol}.csv"
    cached = False
    try:
        d = yf.download(symbol, start=start, progress=False, auto_adjust=False)
        if d.empty:
            raise ValueError("시세가 비어 있습니다")
    except Exception:
        if not cache.exists():
            raise
        d = pd.read_csv(cache, index_col=0, parse_dates=True)
        cached = True
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.index = pd.to_datetime(d.index).tz_localize(None) if d.index.tz is not None else pd.to_datetime(d.index)
    d = validate_stock_prices(d)
    d.attrs["cached"] = cached
    OUT.mkdir(exist_ok=True)
    d.to_csv(cache)
    return d


def validate_stock_prices(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    valid = (np.isfinite(d).all(axis=1) & (d[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
             & (d["Volume"] >= 0) & (d["High"] >= d[["Open", "Low", "Close"]].max(axis=1))
             & (d["Low"] <= d[["Open", "High", "Close"]].min(axis=1)))
    rejected = int((~valid).sum())
    d = d.loc[valid]
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d.attrs["rejected_rows"] = rejected
    if len(d) < 2:
        raise ValueError("검증된 시세가 2거래일 미만입니다")
    return d


FLOW_PERIODS = [1, 5, 30, 60, 120]  # 거래일


def krx_flow(ticker):
    """종목 수급 (KRX): 투자자별 순매수(원·주), 공매도 거래 비중·잔고 비중(%), 최근 120거래일 이상."""
    if not (ENV.get("KRX_ID") and ENV.get("KRX_PW")):
        raise MissingKey("KRX 로그인 정보가 .env에 없습니다 (KRX_ID, KRX_PW).")
    os.environ["KRX_ID"], os.environ["KRX_PW"] = ENV["KRX_ID"], ENV["KRX_PW"]
    with contextlib.redirect_stdout(io.StringIO()):
        from pykrx import stock
        a, b = (TODAY - pd.Timedelta(days=270)).strftime("%Y%m%d"), TODAY.strftime("%Y%m%d")
        net = stock.get_market_trading_value_by_date(a, b, ticker)
        vol = stock.get_market_trading_volume_by_date(a, b, ticker)
        short = stock.get_shorting_volume_by_date(a, b, ticker)
        cap = stock.get_market_cap_by_date(a, b, ticker)["시가총액"].astype(float)
        try:
            bal = stock.get_shorting_balance_by_date(a, b, ticker)
        except Exception:  # 잔고는 보조 정보라 실패해도 나머지 수급은 보여 준다
            bal = pd.DataFrame()
    net = net[(net[["기관합계", "외국인합계", "개인"]] != 0).any(axis=1)]  # 장 마감 전 빈 줄 제외
    vol = vol.reindex(net.index).fillna(0)
    # 공매도 잔고 비중: KRX '비중'은 소수 둘째 자리까지라 잔고 ÷ 상장주식수로 직접 계산한다. 잔고는 2거래일 늦게 공시된다.
    bal_ratio = pd.Series(dtype=float)
    if len(bal):
        b2 = bal[bal["공매도잔고"] > 0]
        bal_ratio = (b2["공매도잔고"].astype(float) / b2["상장주식수"].astype(float) * 100)
    cols = {"외국인": "외국인합계", "기관": "기관합계", "개인": "개인"}
    daily = {
        "dates": [d.strftime("%Y-%m-%d") for d in net.index],
        "value": {k: [round(float(v) / 1e8, 1) for v in net[c]] for k, c in cols.items()},  # 억원
        "volume": {k: [int(v) for v in vol[c]] for k, c in cols.items()},  # 주
        "short_bal": [None if pd.isna(v) else round(float(v), 4) for v in bal_ratio.reindex(net.index)],  # %
    }
    inst, frgn, indiv = net["기관합계"], net["외국인합계"], net["개인"]
    smart = inst + frgn
    cap_now = float(cap.dropna().iloc[-1])
    # 누적 순매수와 현재 시가총액 대비 비율(%): 두 회사의 크기가 달라 금액만으로는 비교가 어렵다.
    sums = lambda x: {n: {"won": float(x.tail(n).sum()), "pct": float(x.tail(n).sum()) / cap_now * 100}
                      if len(x) >= n else None for n in FLOW_PERIODS}

    streak, sign = 0, (smart.iloc[-1] > 0)
    for v in reversed(smart.tolist()):
        if (v > 0) != sign:
            break
        streak += 1
    ratio = short["비중"].astype(float)
    ratio = ratio[ratio > 0]
    # 거래 비중: 1일은 당일 값, 나머지는 기간 평균. 잔고 비중: 현재 값과 n거래일 전 대비 변화(%p).
    short_trade = {n: float(ratio.tail(n).mean()) if len(ratio) >= n else None for n in FLOW_PERIODS}
    short_bal = None
    if len(bal_ratio):
        short_bal = {"now": float(bal_ratio.iloc[-1]), "date": bal_ratio.index[-1].strftime("%Y-%m-%d"),
                     "chg": {n: float(bal_ratio.iloc[-1] - bal_ratio.iloc[-1 - n]) if len(bal_ratio) > n else None
                             for n in FLOW_PERIODS}}
    return {
        "date": net.index[-1].strftime("%Y-%m-%d"), "cap": cap_now,
        "rows": {"외국인": sums(frgn), "기관": sums(inst), "기관+외국인": sums(smart), "개인": sums(indiv)},
        "buy_days": int((smart.tail(30) > 0).sum()), "streak": streak, "streak_buy": bool(sign),
        "short_trade": short_trade, "short_bal": short_bal,
        "daily": daily,
    }


RSI_N = 14
RSI_HI, RSI_LO = 70, 30  # 과매수 · 과매도 기준 (가장 널리 쓰는 값)


def rsi(close, n=RSI_N):
    """와일더 RSI: 상승폭·하락폭을 1/n 지수평균한 비율."""
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + up / down)).dropna()


def rsi_zone(v):
    if v >= RSI_HI:
        return "과매수", "hot"
    if v <= RSI_LO:
        return "과매도", "cold"
    return "중립", ""


def company_price_fields(px):
    close = px["Close"]
    last_d = close.index[-1]
    year = px[px.index > last_d - pd.Timedelta(weeks=52)]
    oscillation = rsi(close)
    return {
        "price": float(close.iloc[-1]), "prev": float(close.iloc[-2]), "date": last_d.strftime("%Y-%m-%d"),
        "rsi": float(oscillation.iloc[-1]) if len(oscillation) else None,
        "rsi_prev": float(oscillation.iloc[-6]) if len(oscillation) >= 6 else None,
        "hi52": float(year["High"].max()), "hi52_date": year["High"].idxmax().strftime("%Y-%m-%d"),
        "lo52": float(year["Low"].min()), "lo52_date": year["Low"].idxmin().strftime("%Y-%m-%d"),
        "ohlcv": [[d.strftime("%Y-%m-%d"), round(opn), round(high), round(low), round(cl), int(vol)]
                  for d, opn, high, low, cl, vol in px[company_catalog.PRICE_COLUMNS].itertuples(name=None)],
    }


def company_tabs():
    """삼성전자·하이닉스 탭 데이터: 기업 개요 + 차트용 일별 시세."""
    out = []
    for c in CFG["companies"]:
        try:
            px = stock_ohlcv(c["yahoo"])
        except Exception as e:
            out.append({**c, "error": f"시세를 받지 못했습니다 ({e.__class__.__name__})"})
            continue
        close = px["Close"]
        try:
            flow, note = krx_flow(c["ticker"]), None
        except MissingKey as e:
            flow, note = None, str(e)
        except Exception as e:
            flow, note = None, f"KRX 수급 데이터를 받지 못했습니다 ({e.__class__.__name__})"
            print(f"  ! {c['name']} 수급: {e.__class__.__name__}")
        try:
            fcf, fcf_note = fcf_analysis(c["dart"]), None
        except MissingKey as e:
            fcf, fcf_note = None, str(e)
        except Exception as e:
            fcf, fcf_note = None, f"DART 재무제표를 받지 못했습니다 ({e.__class__.__name__})"
            print(f"  ! {c['name']} 잉여현금흐름: {e.__class__.__name__}")
        try:
            reports, reports_note = report_summary(c["ticker"], float(close.iloc[-1])), None
        except Exception as e:
            reports, reports_note = None, f"증권사 리포트를 받지 못했습니다 ({e.__class__.__name__})"
            print(f"  ! {c['name']} 리포트: {e.__class__.__name__}")
        rejected = px.attrs.get("rejected_rows", 0)
        if rejected:
            note = (note + " · " if note else "") + f"고가·저가 범위를 벗어나는 등 비정상 시세 {rejected}행 제외"
        if px.attrs.get("cached"):
            note = (note + " · " if note else "") + "시세 수신 실패로 마지막 저장본 사용 · 종가 기준일 확인"
        out.append({
            **c, "flow": flow, "note": note, "fcf": fcf, "fcf_note": fcf_note, "reports": reports, "reports_note": reports_note,
            **company_price_fields(px),
        })
    return out


DART_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DART_FIRST_YEAR = 2015  # 전체 재무제표 API가 제공하는 첫 해
DART_REPORTS = [("11013", 3), ("11012", 6), ("11014", 9), ("11011", 12)]  # 1분기·반기·3분기·사업보고서 (누적 개월)
# 연결재무제표 표준계정(접두어 ifrs_/ifrs-full_/dart_ 제외) → 열 이름. 현금흐름표는 연초부터 누적 금액이다.
DART_CF = {
    "CashFlowsFromUsedInOperatingActivities": "ocf",                # 영업활동현금흐름
    "CashFlowsFromUsedInOperations": "ops",                         # 영업에서 창출된 현금 (이자·배당·법인세 전)
    "NetCashflowsFromUsedInOperations": "ops",                      # 같은 항목의 DART 표준계정명 (에이피알 등)
    "AdjustmentsForReconcileProfitLoss": "adj",                     # 비현금 조정 (감가상각비 등) — 삼성전자만 본문에 있음
    "AdjustmentsForAssetsLiabilitiesOfOperatingActivities": "wc",   # 운전자본 변동 — 삼성전자만 본문에 있음
    "PurchaseOfPropertyPlantAndEquipment": "ppe",                   # 유형자산의 취득
    "PurchaseOfIntangibleAssets": "intang",                         # 무형자산의 취득
}
DART_IS = {"ProfitLoss": "ni", "Revenue": "rev"}


def _dart_amount(text):
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _dart_report(corp, year, code):
    """한 보고서의 누적 금액(원). 아직 제출 전이면 None."""
    query = urllib.parse.urlencode({"crtfc_key": ENV["DART_API_KEY"], "corp_code": corp, "bsns_year": year,
                                    "reprt_code": code, "fs_div": "CFS"})
    payload = json.loads(urllib.request.urlopen(f"{DART_URL}?{query}", timeout=60).read())
    if payload.get("status") == "013":  # 조회된 데이터 없음
        return None
    if payload.get("status") != "000":
        raise ValueError(f"DART {payload.get('status')} {payload.get('message', '')}")
    out = {}
    for r in payload["list"]:
        name = r["account_id"].split("_", 1)[-1]
        if r["sj_div"] == "CF":
            key = next((v for k, v in DART_CF.items() if name.startswith(k)), None)
            # 하이닉스 2015년 보고서는 '영업에서 창출된 현금'에 조정 태그를 잘못 붙여 이름으로 한 번 더 거른다.
            if key == "adj" and "조정" not in r["account_nm"]:
                key = None
            amount = r["thstrm_amount"]
        elif r["sj_div"] in ("IS", "CIS"):
            key = DART_IS.get(name)
            amount = r.get("thstrm_add_amount") or r["thstrm_amount"]  # 분기보고서 손익은 3개월 값과 누적 값을 함께 준다
        else:
            continue
        if key and key not in out:
            v = _dart_amount(amount)
            if v is not None:
                out[key] = abs(v) if key in ("ppe", "intang") else v  # 회사마다 투자 지출 부호 표기가 달라 크기만 쓴다
    return out or None


def dart_financials(corp):
    """분기 말 기준 연초 누적 금액 표. output/.dart_<corp>.csv에 저장해 두고 새로 제출된 보고서만 받는다."""
    if not ENV.get("DART_API_KEY"):
        raise MissingKey("DART 키가 .env에 없습니다 (DART_API_KEY). 넣으면 다음 실행부터 연결됩니다.")
    cache = OUT / f".dart_{corp}.csv"
    data = pd.read_csv(cache, index_col=0) if cache.exists() else pd.DataFrame()
    fetched = {}
    for year in range(DART_FIRST_YEAR, TODAY.year + 1):
        for code, months in DART_REPORTS:
            end = pd.Period(f"{year}-{months:02d}", freq="M")
            if str(end) in data.index or end >= TODAY.to_period("M"):
                continue
            row = _dart_report(corp, year, code)
            if row is not None:
                fetched[str(end)] = row
    if fetched:
        data = pd.concat([data, pd.DataFrame.from_dict(fetched, orient="index")]).sort_index()
        data.to_csv(cache)
    if data.empty:
        raise ValueError("DART 재무제표가 비어 있습니다")
    return data


def fcf_analysis(corp):
    """잉여현금흐름 = 영업활동현금흐름 − (유형+무형자산 취득). 연간 막대, 분기별 TTM 막대, 최근 TTM 구성."""
    ytd = dart_financials(corp)
    ytd.index = pd.PeriodIndex(ytd.index, freq="M")
    ytd["capex"] = ytd["ppe"] + ytd.get("intang", 0).fillna(0)
    # 누적 → 분기 값: 직전 분기 누적을 뺀다 (1분기는 그대로). 빠진 보고서는 빈칸으로 두어 TTM에서 빠진다.
    ytd = ytd.reindex(pd.period_range(ytd.index.min(), ytd.index.max(), freq="Q").asfreq("M"))
    quarter = ytd - ytd.shift(1)
    q1 = ytd.index.month == 3
    quarter[q1] = ytd[q1]
    ttm = quarter.rolling(4, min_periods=4).sum()
    fy = ytd[ytd.index.month == 12]

    def fcf(df):
        return (df["ocf"] - df["capex"]).dropna()

    annual = fcf(fy).tail(10)
    ttm_fcf = fcf(ttm)
    latest_end = ttm_fcf.index[-1]
    last = ttm.loc[latest_end]
    is_fy = latest_end.month == 12
    label = f"FY{latest_end.year}" if is_fy else f"TTM · {latest_end.to_timestamp(how='end'):%Y-%m-%d}"
    v = lambda k: None if k not in last or pd.isna(last[k]) else float(last[k])
    ni, ocf, ops, capex = v("ni"), v("ocf"), v("ops"), v("capex")
    rows = [("순이익", ni)]
    if v("adj") is not None and v("wc") is not None:
        rows += [("비현금 조정<small>감가상각비 등</small>", v("adj")), ("운전자본 변동", v("wc"))]
    elif ops is not None and ni is not None:
        rows += [("비현금 조정·운전자본<small>영업창출현금 − 순이익</small>", ops - ni)]
    if ops is not None:
        rows += [("이자·배당·법인세", ocf - ops)]
    rows += [("영업활동현금흐름", ocf, "sub"), ("자본적지출<small>유형+무형자산 취득</small>", -capex), ("잉여현금흐름", ocf - capex, "total")]
    last3 = annual.tail(3)
    rev = v("rev")
    out_fcf = ocf - capex
    return {
        "annual": [[f"FY{p.year}", float(x)] for p, x in annual.items()],
        "ttm": [[p.strftime("%y.%m"), float(x)] for p, x in ttm_fcf.tail(40).items()],
        "last": out_fcf, "label": label,
        "avg3": float(last3.mean()) if len(last3) == 3 else None,
        "sum3": float(last3.sum()) if len(last3) == 3 else None,
        "avg3_label": " · ".join(f"FY{p.year}" for p in last3.index),
        "margin": out_fcf / rev if rev and rev > 0 else None,
        "conversion": out_fcf / ni if ni and ni > 0 else None,
        "rows": [list(r) + [None] * (3 - len(r)) for r in rows],
    }


REPORT_URL = "https://consensus.hankyung.com/analysis/list"
REPORT_DAYS = 90  # 컨센서스에 넣는 목표가의 최대 나이 (오래된 목표가가 평균을 끌어내리지 않게)
OPINIONS = [  # 증권사마다 표기가 달라 매수·중립·매도 세 가지로 묶는다
    ("매수", ("strong buy", "buy", "매수", "outperform", "overweight", "비중확대", "적극")),
    ("중립", ("hold", "중립", "neutral", "market perform", "marketperform", "보유")),
    ("매도", ("sell", "매도", "underperform", "비중축소", "reduce")),
]


def _opinion(text):
    low = text.lower()
    # "Trading Buy"처럼 매수 계열 단어가 들어간 경우를 위해 매도부터 확인하지 않고 위 순서대로 찾는다.
    return next((label for label, keys in OPINIONS if any(k in low for k in keys)), None)


def analyst_reports(ticker):
    """최근 1년 증권사 기업 리포트 (한경 컨센서스). output/.reports_<ticker>.csv에 저장해 두고 실패하면 저장본을 쓴다."""
    cache = OUT / f".reports_{ticker}.csv"
    query = urllib.parse.urlencode({"sdate": (TODAY - pd.Timedelta(days=365)).strftime("%Y-%m-%d"),
                                    "edate": TODAY.strftime("%Y-%m-%d"), "report_type": "CO",
                                    "search_text": ticker, "pagenum": 300})
    try:
        req = urllib.request.Request(f"{REPORT_URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            link = re.search(r'href="(/analysis/downpdf\?report_idx=\d+)"[^>]*>(.*?)</a>', tr, re.S)
            if len(cells) < 6 or not link:
                continue
            text = [html.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip() for c in cells]
            target = re.sub(r"\D", "", text[2])
            title = html.unescape(re.sub(r"\s+", " ", link.group(2))).strip()
            title = re.sub(rf"^.*?\({ticker}\)\s*", "", title)  # 앞의 "회사명(코드)" 반복 제거
            rows.append({"date": text[0], "title": title, "target": int(target) if target and int(target) > 0 else None,
                         "opinion": text[3], "author": text[4], "broker": text[5],
                         "url": "https://consensus.hankyung.com" + link.group(1)})
        if not rows and not re.search(r">\s*결과가 없습니다\.?\s*<", page):
            raise ValueError("한경 컨센서스 응답의 리포트 목록을 확인할 수 없습니다")
        df = pd.DataFrame(rows, columns=["date", "title", "target", "opinion", "author", "broker", "url"])
        df.to_csv(cache, index=False)
        df.attrs["cached"] = False
    except Exception:
        if not cache.exists():
            raise
        df = pd.read_csv(cache, dtype={"target": "Int64"})
        df.attrs["cached"] = True
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date", ascending=False, kind="stable").reset_index(drop=True)


def report_summary(ticker, price):
    """증권사별 최신 리포트 + 최근 90일 목표가 컨센서스."""
    df = analyst_reports(ticker)
    if df.empty:
        return None
    text_columns = ["title", "opinion", "author", "broker"]
    df[text_columns] = df[text_columns].fillna("")
    df["view"] = df["opinion"].map(_opinion)
    rated = df[df["target"].notna()]
    latest = []
    for broker, g in rated.groupby("broker", sort=False):
        last = g.iloc[0]
        prev = g[g["target"] != last["target"]]
        prev_target = None if prev.empty else int(prev.iloc[0]["target"])
        # 직전 목표가: 같은 증권사가 이번과 다른 목표가를 낸 가장 최근 리포트. 같은 값만 있었으면 '유지'.
        latest.append({
            "date": last["date"].strftime("%Y-%m-%d"), "broker": broker, "author": last["author"],
            "opinion": last["opinion"], "view": last["view"], "target": int(last["target"]),
            "prev": prev_target if len(g) > 1 else None, "repeat": len(g) > 1,
            "upside": (int(last["target"]) / price - 1) * 100 if price else None,
            "title": last["title"], "url": last["url"],
            "fresh": (TODAY - last["date"]).days <= REPORT_DAYS,
        })
    fresh = [r for r in latest if r["fresh"]]
    targets = [r["target"] for r in fresh]
    views = {label: sum(r["view"] == label for r in fresh) for label, _ in OPINIONS}
    recent = df[df["date"] > TODAY - pd.Timedelta(days=REPORT_DAYS)]
    return {
        "latest": latest, "price": price, "cached": df.attrs.get("cached"),
        "n_reports": len(recent), "n_brokers": len(fresh),
        "avg": float(np.mean(targets)) if targets else None, "median": float(np.median(targets)) if targets else None,
        "high": max(fresh, key=lambda r: r["target"]) if fresh else None,
        "low": min(fresh, key=lambda r: r["target"]) if fresh else None,
        "views": views, "last_date": df["date"].iloc[0].strftime("%Y-%m-%d"),
    }


CUSTOMS_URL ="https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"


MOTIR_URL = "https://www.motir.go.kr"
MOTIR_LIST = "/kor/article/ATCL3f49a5a8c?searchKeyword=" + urllib.parse.quote("수출입 동향")


@lru_cache(maxsize=None)
def _customs_rows(month):
    """한 번 받은 월별 원자료를 수출 탭들과 함께 사용한다."""
    query = urllib.parse.urlencode({"serviceKey": ENV["DATA_GO_KR_KEY"], "strtYymm": month, "endYymm": month})
    root = ET.fromstring(urllib.request.urlopen(f"{CUSTOMS_URL}?{query}", timeout=120).read())
    if root.findtext(".//resultCode") != "00":
        raise ValueError(f"관세청 API: {root.findtext('.//resultMsg')}")
    rows = [(it.findtext("hsCode") or "", float(it.findtext("expDlr") or 0)) for it in root.iter("item")]
    total = next((v for code, v in rows if code in ("-", "")), None)
    if not total:
        return None
    return rows


def _customs_month(month, groups=None):
    """한 달 전체 품목 수출(달러): 총계 + HS 묶음별 합계. 아직 발표 전이면 None."""
    rows = _customs_rows(month)
    if rows is None:
        return None
    total = next(v for code, v in rows if code in ("-", ""))
    if groups is None:
        cfg = CFG["exports"]
        groups = {"semis": cfg["semis_hs"], **{item["name"]: item["hs"] for item in cfg["items"]}}
    out = {"total": total}
    for name, prefixes in groups.items():
        out[name] = sum(v for code, v in rows if code[:1].isdigit() and code.startswith(tuple(prefixes)))
    return out


def customs_exports():
    """월별 수출 (달러). output/.customs_exports.csv에 저장해 두고 새로 발표된 달만 받는다."""
    if not ENV.get("DATA_GO_KR_KEY"):
        raise MissingKey("공공데이터포털 키가 .env에 없습니다 (DATA_GO_KR_KEY). 넣으면 다음 실행부터 연결됩니다.")
    cache = OUT / ".customs_exports.csv"
    data = pd.read_csv(cache, index_col=0) if cache.exists() else pd.DataFrame()
    expected = ["total", "semis"] + [i["name"] for i in CFG["exports"]["items"]]
    if list(data.columns) != expected:
        data = pd.DataFrame()  # 품목 설정이 바뀌면 처음부터 다시 받는다
    months = pd.period_range(CFG["exports"]["first_month"], TODAY.to_period("M") - 1, freq="M")
    # 최근 두 달은 정정될 수 있어 다시 받는다 (관세청은 매월 15일경 전월까지 갱신).
    todo = [m for m in months if str(m) not in data.index or m >= months[-1] - 1]
    fetched = {}
    for m in todo:
        row = _customs_month(m.strftime("%Y%m"))
        if row is not None:
            fetched[str(m)] = row
    if fetched:
        new = pd.DataFrame.from_dict(fetched, orient="index")[expected]
        data = pd.concat([data.drop(index=[i for i in new.index if i in data.index]), new]).sort_index()
        data.to_csv(cache)
    if data.empty:
        raise ValueError("관세청 수출 데이터가 비어 있습니다")
    return data


def motir_flash():
    """산업통상부 월간 수출입 동향(매월 1일 잠정치). 관세청 품목별 자료보다 2주가량 빠르다."""
    cache = OUT / ".motir_exports.json"
    saved = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None

    def page(path):
        req = urllib.request.Request(MOTIR_URL + path, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

    def plain(html_text):
        s = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html_text)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s)))

    try:
        found = re.search(r'/kor/article/ATCL3f49a5a8c/(\d+)/view[^>]*>\s*<i>(\d{4})년 (\d{1,2})월 수출입 동향</i>', page(MOTIR_LIST))
        if not found:
            raise ValueError("목록에서 수출입 동향을 찾지 못했습니다")
        article, year, month = found.group(1), int(found.group(2)), int(found.group(3))
        body = plain(page(f"/kor/article/ATCL3f49a5a8c/{article}/view"))
        total = re.search(r"수출은 전년 동월 대비 ([\d.]+)% (증가|감소)한 ([\d,.]+) ?억 달러", body)
        semis = re.search(r"반도체 수출 \(([\d,.]+) ?억 달러 ?, ?([+△-]) ?([\d.]+)%?\)", body)
        posted = re.search(r"등록일 (\d{4}-\d{2}-\d{2})", body)
        if not (total and semis and posted):
            raise ValueError("보도자료 문구가 달라 수치를 읽지 못했습니다")
        out = {"month": f"{year}-{month:02d}", "posted": posted.group(1),
               "total": float(total.group(3).replace(",", "")),
               "total_y1": float(total.group(1)) * (1 if total.group(2) == "증가" else -1),
               "semis": float(semis.group(1).replace(",", "")),
               "semis_y1": float(semis.group(3)) * (1 if semis.group(2) == "+" else -1)}
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out
    except Exception as e:
        print(f"  ! 산업부 수출입 동향: {e.__class__.__name__} {e}")
        return saved


@lru_cache(maxsize=None)
def kospi_market_cap():
    """코스피 시가총액 (원, 일별) — 한국은행 ECOS."""
    return ecos("802Y001", "0183000", cycle="D", start="20100101") * 1e8


ECOS_DATE_FORMAT = {"D": "%Y%m%d", "M": "%Y%m"}


def ecos(stat, item, cycle="M", start=None):
    """Bank of Korea ECOS series (monthly by default; "D" for daily), indexed by period start."""
    key = ENV.get("ECOS_API_KEY")
    if not key:
        raise MissingKey("한국은행 ECOS API 키가 .env에 없습니다 (ECOS_API_KEY). 키를 넣으면 다음 실행부터 연결됩니다.")
    fmt = ECOS_DATE_FORMAT[cycle]
    start = start or pd.Timestamp(1990, 1, 1).strftime(fmt)
    end = TODAY.strftime(fmt)
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100000/{stat}/{cycle}/{start}/{end}/{item}"
    payload = json.loads(urllib.request.urlopen(url, timeout=60).read().decode())
    rows = payload.get("StatisticSearch", {}).get("row")
    if not rows:
        # ECOS reports problems (wrong key, no data) as {"RESULT": {"CODE": ..., "MESSAGE": ...}}.
        result = payload.get("RESULT", {})
        raise ValueError(f"ECOS {result.get('CODE', '')} {result.get('MESSAGE', '응답 없음')}".strip())
    s = pd.Series(
        pd.to_numeric([r["DATA_VALUE"] for r in rows], errors="coerce"),
        index=pd.to_datetime([r["TIME"] for r in rows], format=fmt),
    )
    return s.dropna().sort_index()


def ecos_spread(calc):
    stat, long_term, short_term = (calc[key] for key in ("stat", "long", "short"))
    cycle = calc.get("cycle", "D")
    cache = OUT / f".ecos_spread_{stat}_{long_term}_{short_term}_{cycle}.csv"
    try:
        long_rate = ecos(stat, long_term, cycle=cycle, start=calc.get("start"))
        short_rate = ecos(stat, short_term, cycle=cycle, start=calc.get("start"))
        spread = (long_rate - short_rate).dropna().round(3)
        if spread.empty:
            raise ValueError("같은 관측일의 두 금리가 없습니다")
    except (OSError, ValueError, KeyError, MissingKey):
        if not cache.is_file():
            raise
        spread = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0].dropna()
        spread.attrs["source_note"] = "한국은행 ECOS · 마지막 저장 자료"
        return spread
    company_catalog.write_atomic(cache, spread.to_csv())
    return spread


def compute(ind, px, uni):
    c = ind["calc"]
    src = c["src"]
    if src == "yahoo":
        s = px[c["ticker"]].dropna() if c["ticker"] in px else pd.Series(dtype=float)
        if s.empty and c.get("fallback_series"):
            s = fred(c["fallback_series"]).copy()
            s.attrs["source_note"] = f'Yahoo 수신 실패 · FRED {c["fallback_series"]} 종가 사용'
        return s
    if src == "fred":
        series = fred(c["series"])
        scale = c.get("scale")
        return (series * scale).round(10) if scale is not None else series
    if src == "fred_yoy":
        series = fred(c["series"])
        previous_year = series.copy()
        previous_year.index += pd.DateOffset(years=1)
        return ((series / previous_year.where(previous_year > 0) - 1) * 100).dropna()
    if src == "ecos_spread":
        return ecos_spread(c)
    if src == "ecos":
        return ecos(c["stat"], c["item"], c.get("cycle", "M"))
    if src == "kofia_cap_ratio":
        # 금액은 시장 규모에 따라 커지므로 코스피 시가총액 대비 비율(%)로 본다.
        amount = kofia(c["table"])[c["column"]]
        cap = kospi_market_cap()
        return (amount / cap * 100).dropna()
    if src == "krx_foreign_ratio":
        # 시장 규모가 해마다 달라 조원 금액 대신 같은 기간 거래대금 대비 비율(%)로 본다.
        f = krx_flows(c.get("market", "KOSPI"))
        w = c.get("window", 20)
        return (f["net"].rolling(w).sum() / f["buy"].rolling(w).sum() * 100).dropna()
    if src == "fred_spread":
        series = (fred(c["a"]) - fred(c["b"])).dropna()
        scale = c.get("scale")
        return (series * scale).round(10) if scale is not None else series
    if src in ("fred_yoy3m", "customs_yoy3m"):
        note = None
        if src == "customs_yoy3m":
            # 관세청 원계열(계절조정 없음)이라 전년 같은 3개월과 비교해 계절성을 상쇄한다.
            try:
                monthly = customs_exports()[c.get("column", "total")]
                monthly.index = pd.PeriodIndex(monthly.index, freq="M").to_timestamp()
            except Exception as e:
                if not c.get("fallback_series"):
                    raise
                monthly = fred(c["fallback_series"])
                note = f'관세청 수신 실패({e.__class__.__name__}) · OECD {c["fallback_series"]} 사용'
        else:
            monthly = fred(c["series"])
        monthly = monthly.reindex(pd.date_range(monthly.index.min(), monthly.index.max(), freq="MS"))
        s3 = monthly.rolling(3, min_periods=3).sum()
        s = ((s3 / s3.shift(12) - 1) * 100).dropna()
        if note:
            s.attrs["source_note"] = note
        return s
    if src == "yahoo_ma_gap":
        s = px[c["ticker"]].dropna()
        return ((s / s.rolling(c["window"]).mean() - 1) * 100).dropna()
    if src == "yahoo_rv":
        s = px[c["ticker"]].dropna()
        return (np.log(s).diff().rolling(c["window"]).std() * np.sqrt(252) * 100).dropna()
    if src == "breadth":
        ma = uni.rolling(c["window"]).mean()
        valid = ma.notna() & uni.notna()
        cnt = valid.sum(axis=1)
        return ((uni > ma) & valid).sum(axis=1).div(cnt).mul(100)[cnt >= 30]
    raise ValueError(f"알 수 없는 calc.src: {src}")


# ---------------------------------------------------------------- 계산

def zone_of(ind, v):
    for i, z in enumerate(ind["zones"]):
        if z["max"] is None or v < z["max"]:
            return i


def marker_pos(ind, v):
    zs = ind["zones"]
    i = zone_of(ind, v)
    lo = ind["range"][0] if i == 0 else zs[i - 1]["max"]
    hi = ind["range"][1] if zs[i]["max"] is None else zs[i]["max"]
    f = min(max((v - lo) / (hi - lo), 0), 1) if hi > lo else 0.5
    return (i + f) / len(zs) * 100


def market_history(ind, series):
    freq = ind["freq"]
    period = {"daily": "최근 60거래일", "weekly": "최근 52주", "monthly": "최근 24개월", "quarterly": "최근 20분기"}[freq]
    if freq in ("daily", "weekly"):
        recent = series.tail(52 if freq == "weekly" else 60)
    else:
        months = 60 if freq == "quarterly" else 24
        recent = series.loc[series.index > series.index[-1] - pd.DateOffset(months=months)]

    zones = ind["zones"]
    offset = len(series) - len(recent)
    previous = zones[zone_of(ind, series.iloc[offset - 1])]["label"] if offset else None
    points = []
    for date, value in recent.items():
        label = zones[zone_of(ind, value)]["label"]
        changed_from = previous if previous != label else None
        points.append([market_observation_date(ind, date), float(value), label, changed_from])
        previous = label

    current = zone_of(ind, recent.iloc[-1])
    lower = zones[current - 1]["max"] if current else None
    upper = zones[current]["max"]
    boundaries = [limit for limit in (lower, upper) if limit is not None]
    return {"points": points, "boundaries": boundaries, "units": ind["unit"],
            "decimals": ind["decimals"], "period": period}


def analyze(ind, s, kospi):
    source_note = s.attrs.get("source_note")
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if s.empty:
        raise ValueError("유효한 지표 데이터가 없습니다")
    last = s.index[-1]
    v = s.iloc[-1]
    observations = ind.get("comparison_observations")
    if observations:
        benchmark = s.iloc[-observations - 1:-observations]
    elif ind["freq"] == "weekly":
        benchmark = s.reindex([last - pd.DateOffset(weeks=4)]).dropna()
    elif ind["freq"] == "daily":
        benchmark = s.loc[:last - pd.Timedelta(days=7)].tail(1)
    else:
        months = ind.get("comparison_months", 3 if ind["freq"] == "quarterly" else 1)
        benchmark = s.reindex([last - pd.DateOffset(months=months)]).dropna()
    pv = benchmark.iloc[-1] if len(benchmark) else np.nan
    prev_date = benchmark.index[-1] if len(benchmark) else None
    previous = None
    if len(s) > 1:
        previous = {"date": s.index[-2], "zone": ind["zones"][zone_of(ind, s.iloc[-2])]}
    since = ind["pct_since"]
    hist = s[s.index >= last - pd.DateOffset(years=1)] if since == "1y" else s[s.index >= since]
    if hist.empty:
        raise ValueError("백분위 기준 기간에 데이터가 없습니다")
    actual_since = hist.index[0].strftime("%Y-%m")
    default_stale_days = {"daily": 7, "weekly": 18, "monthly": 100, "quarterly": 310}[ind["freq"]]
    stale_days = ind.get("stale_days", default_stale_days)
    stale = (TODAY - last).days > stale_days
    z = ind["zones"][zone_of(ind, v)]
    return {
        "value": v, "prev": pv, "prev_date": prev_date, "previous": previous,
        "history": market_history(ind, s),
        "date": last, "zone": z, "source_note": source_note,
        "pos": marker_pos(ind, v),
        "prev_pos": marker_pos(ind, pv) if pd.notna(pv) else None,
        "pct": (hist <= v).mean() * 100, "since": f"{actual_since}~ · {len(hist):,}개", "stale": stale,
    }


def group_verdict(group, rows):
    sig = [r["res"]["zone"]["signal"] for r in rows if r.get("res") and not r["res"].get("stale")]
    counts = {"pos": sig.count(1), "neu": sig.count(0), "neg": sig.count(-1)}
    total = sum(sig)
    key = "pos" if total > 0 else "neg" if total < 0 else "neu"
    coverage = len(sig)
    label = group["labels"][key] if coverage else "판정 보류"
    if coverage and coverage < len(rows):
        label += " (일부 자료)"
    return {"key": key, "label": label, "counts": counts, "score": total,
            "coverage": coverage, "expected": len(rows)}


def summary_sentence(v):
    if any(g.get("coverage", 1) == 0 for g in v.values()):
        return "필수 그룹 자료가 부족해 종합 판정을 보류합니다"
    e, i, s, m = v["economy"]["score"], v["internals"]["score"], v["sentiment"]["score"], v["money"]["score"]
    if e >= 0 and i < 0:
        economy = "양호" if e > 0 else "중립"
        text = f"경기·업황 {economy} · 추세·수급 약세"
    elif e < 0 and i < 0:
        text = "경기·업황 약화 · 추세·수급 약세"
    elif e < 0 and i >= 0:
        trend = "강함" if i > 0 else "보통"
        text = f"경기·업황 약화 · 추세·수급 {trend}"
    elif e > 0 and i > 0:
        text = "경기·업황 양호 · 추세·수급 강함"
    else:
        text = "뚜렷한 방향 없이 혼재된 조합"
    if s < 0:
        text += " · 변동성·투자심리 불안"
    if m < 0:
        text += " · 금리·환율 부담"
    return text


# ---------------------------------------------------------------- 표시 형식

def num(v, d, unit="", sign=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return f"{v:+,.{d}f}{unit}" if sign else f"{v:,.{d}f}{unit}"


def market_verdict_tone(signal):
    """긍정 판정은 빨강, 부정 판정은 파랑, 중립은 회색."""
    return "var(--bad)" if signal > 0 else "var(--accent)" if signal < 0 else "var(--text-2)"


def delta_unit(unit):
    return "%p" if unit in ("%", "%p") else unit


esc = html.escape


# ---------------------------------------------------------------- HTML

def market_observation_date(ind, date):
    return date.strftime("%Y-%m" if ind["freq"] == "monthly" else "%Y-%m-%d")


def market_change(res):
    previous = res.get("previous")
    return not res["stale"] and previous is not None and previous["zone"]["label"] != res["zone"]["label"]


def market_zone_rule(ind, res):
    zones = ind["zones"]
    index = zones.index(res["zone"])
    lower = zones[index - 1]["max"] if index else None
    upper = zones[index]["max"]
    unit = ind["unit"]
    bounds = []
    for limit, relation in ((lower, "이상"), (upper, "미만")):
        if limit is not None:
            bounds.append((limit, f"{limit:,g}{unit} {relation}"))
    if not bounds:
        return ""

    value = res["value"]
    nearest, _ = min(bounds, key=lambda bound: abs(value - bound[0]))
    distance = abs(value - nearest)
    decimals = ind["decimals"]
    gap = num(distance, decimals, delta_unit(unit))
    if distance == 0:
        distance_text = f"{nearest:,g}{unit} 경계에 위치"
    elif round(distance, decimals) == 0:
        distance_text = f"{nearest:,g}{unit} 경계까지 {num(10 ** -decimals, decimals, delta_unit(unit))} 미만"
    else:
        distance_text = f"{nearest:,g}{unit} 경계까지 {gap}"
    interval = " ~ ".join(label for _, label in bounds)
    return (f'<div class="market-zone-rule"><span>기준 {esc(interval)}</span>'
            f'<small>{esc(distance_text)}</small></div>')


def market_group_reason(group, rows):
    active = [row for row in rows if row.get("res") and not row["res"]["stale"]]
    if not active:
        return "판정에 반영된 지표가 없습니다."
    active.sort(key=lambda row: not row["ind"].get("star", False))
    by_signal = {sign: [row for row in active if row["res"]["zone"]["signal"] == sign]
                 for sign in (1, 0, -1)}

    for signal, key in ((1, "pos"), (0, "neu"), (-1, "neg")):
        if len(by_signal[signal]) == len(active):
            lead = active[0]
            ind, res = lead["ind"], lead["res"]
            reading = num(res["value"], ind["decimals"], ind["unit"])
            return (f'{len(active)}개 지표 모두 {group["labels"][key]}'
                    f' · {ind["name"].removesuffix(" %")} {reading}')

    parts = []
    for signal, key in ((1, "pos"), (-1, "neg")):
        members = by_signal[signal]
        if members:
            names = "·".join(row["ind"]["name"].removesuffix(" %") for row in members[:2])
            extra = f" 외 {len(members) - 2}개" if len(members) > 2 else ""
            parts.append(f'{group["labels"][key]} {names}{extra}')
    return " / ".join(parts)


def render_market_changes(rows):
    changes = sorted(
        (row for row in rows if row.get("res") and market_change(row["res"])),
        key=lambda row: row["res"]["date"], reverse=True,
    )
    if not changes:
        return ""
    links = []
    for row in changes:
        ind, res = row["ind"], row["res"]
        date = market_observation_date(ind, res["date"])
        old, new = res["previous"]["zone"]["label"], res["zone"]["label"]
        reading = num(res["value"], ind["decimals"], ind["unit"])
        links.append(
            f'<a href="#ind-{esc(ind["id"], quote=True)}">'
            f'<time datetime="{date}">{date}</time> · {esc(ind["name"])} {esc(reading)}'
            f' · {esc(old)} → {esc(new)}</a>'
        )
    return (f'<div class="market-recent"><b>최근 판정 변경 {len(changes)}건</b>'
            f'<ul>{"".join(f"<li>{link}</li>" for link in links)}</ul></div>')


def render_market_row(row):
    ind = row["ind"]
    name = f'<div class="market-label"><h4>{esc(ind["name"])}</h4></div>'
    res = row.get("res")
    attrs = f'class="market-row" id="ind-{ind["id"]}" tabindex="-1"'
    if res is None:
        note = row.get("note") or ind["calc"].get("note") or f'데이터 오류 ({row.get("err", "원인 미확인")})'
        state = "미연결" if row.get("note") or ind["calc"]["src"] == "pending" else "수신 실패"
        return (f'<article {attrs} data-state="unavailable">{name}<div class="market-unavailable">'
                f'<strong>{state} · 판정 제외</strong><small>{esc(note)}</small></div></article>')

    unit, decimals = ind["unit"], ind["decimals"]
    val = res["value"]
    date = market_observation_date(ind, res["date"])
    stale = res["stale"]
    signed = ind["id"] in ("kr_curve", "us_curve", "exports", "kospi_gap", "foreign_net")
    reading = f'<b>{num(val, decimals, unit, sign=signed)}</b>'

    months = ind.get("comparison_months")
    observations = ind.get("comparison_observations")
    if observations:
        period = f"{observations}관측일 전 대비"
    elif ind["freq"] == "weekly":
        period = "4주 전 대비"
    elif months:
        period = {1: "전월", 12: "1년 전"}.get(months, f"{months}개월 전") + " 대비"
    else:
        period = "1주 변화" if ind["freq"] == "daily" else "전월 변화"
    prev = res["prev"]
    if pd.notna(prev):
        prev_date = res["prev_date"]
        benchmark_date = f' · {market_observation_date(ind, prev_date)}' if prev_date is not None else ""
        change_decimals = ind.get("change_decimals", decimals)
        delta = round(val - prev, change_decimals)
        movement = "증가" if delta > 0 else "감소" if delta < 0 else "변화 없음"
        change = f'<b>{num(delta, change_decimals, delta_unit(unit), sign=True)}</b> ' if delta else ""
        comparison = f'<div class="market-delta">{period} · {change}{movement}</div>'
        benchmark = f'<small>비교값 {num(prev, decimals, unit)}{benchmark_date}</small>'
    else:
        comparison = f'<div class="market-delta">{period} 비교 자료 없음</div>'
        benchmark = ""

    zone = res["zone"]
    previous = res.get("previous")
    changed = market_change(res)
    state = "delayed" if stale else "negative" if zone["signal"] < 0 else "current"
    change_state = "yes" if changed else "no" if previous is not None else "unknown"
    change_line = ""
    if stale:
        verdict = (f'<strong>갱신 지연 · 합산 제외</strong>'
                   f'<small>마지막 관측의 판정 · {esc(zone["label"])}</small>{market_zone_rule(ind, res)}')
        transition = "판정 변경 집계 제외"
    else:
        direction = {1: "긍정 +1", 0: "중립 0", -1: "부정 −1"}[zone["signal"]]
        if zone["label"] == "중립":
            direction = "0"
        verdict = (f'<strong style="color:{market_verdict_tone(zone["signal"])}">{esc(zone["label"])}</strong>'
                   f'<span class="market-direction">{direction}</span>{market_zone_rule(ind, res)}')
        transition = "직전 관측 비교 자료 없음"
        if previous is not None:
            previous_date = market_observation_date(ind, previous["date"])
            transition = f'직전 관측 {previous_date} · {esc(previous["zone"]["label"])}'
            if changed:
                change_line = (f'<small class="market-transition"><b>판정 변경</b>'
                               f' · {esc(previous["zone"]["label"])} → {esc(zone["label"])}'
                               f' <span class="market-transition-date">({previous_date} → {date})</span></small>')

    pct = round(res["pct"])
    end = res["date"].strftime("%Y-%m")
    first, samples = res["since"].split("~ · ", maxsplit=1)
    date_labels = {"daily": "관측일", "weekly": "관측 주간 종료일", "monthly": "관측월", "quarterly": "관측분기"}
    date_label = ind.get("date_label", date_labels[ind["freq"]])
    display_date = date
    if date_label == "관측분기":
        display_date = f'{res["date"].year}년 {res["date"].quarter}분기'
    meaning = f'<small>{esc(zone["meaning"])}</small>'
    transition_note = f"<small>{transition}</small>" if transition else ""
    source = f'<small>{esc(res["source_note"])}</small>' if res.get("source_note") else ""
    source += "".join(f'<small><a href="{esc(url, quote=True)}">{esc(label)}</a></small>'
                      for label, url in ind.get("sources", {}).items())
    return (
        f'<article {attrs} data-state="{state}" data-change="{change_state}">'
        f'{name}<div class="market-reading">{reading}<small>{date_label} <time datetime="{date}">{display_date}</time></small>'
        f'{comparison}</div><div class="market-verdict">{verdict}{change_line}</div>'
        f'<details class="market-row-details"><summary>상세 보기<span class="market-sr-only"> · {esc(ind["name"])}</span></summary>'
        f'<div class="market-detail-body">{local_views.render_market_history(ind["name"], res.get("history"))}'
        f'<div><small>{esc(ind["sub"])}</small>{meaning}'
        f'{benchmark}{transition_note}{source}</div>'
        f'<div class="market-rank"><small>과거 백분위</small><b>{pct}%</b>'
        f'<div class="market-rank-track" aria-hidden="true"><i style="width:{pct}%"></i></div>'
        f'<small>{first} ~ {end}<br>{samples}</small></div></div></details></article>'
    )




def render_market_assessment(ctx):
    default_group = "money"
    default_label = next(group["name"] for group in CFG["groups"] if group["id"] == default_group)
    cards, sections = [], []
    for group in CFG["groups"]:
        group_id, name = group["id"], esc(group["name"])
        group_rows = [row for row in ctx["rows"] if row["ind"]["group"] == group_id]
        selected = group_id == default_group
        visibility = "" if selected else " hidden"
        summary = ctx["verdicts"][group_id]
        reason = esc(market_group_reason(group, group_rows))
        counts = summary["counts"]
        count_text = " · ".join(f'{esc(group["labels"][key])} {counts[key]}' for key in ("pos", "neu", "neg"))
        coverage = f'{summary["expected"]}개 중 {summary["coverage"]}개 반영'
        label = esc(summary["label"].removesuffix(" (일부 자료)"))
        color = market_verdict_tone(summary["score"])
        cards.append(
            f'<button class="market-group-card" type="button" data-market-group="{group_id}" '
            f'aria-controls="market-evidence-list" aria-pressed="{str(selected).lower()}">'
            f'<span class="market-group-name">{name}<span aria-hidden="true">›</span></span>'
            f'<strong style="color:{color}">{label}</strong>'
            f'<span class="market-group-reason">{reason}</span>'
            f'<small>{count_text} · {coverage}</small></button>')
        rows = "".join(render_market_row(row) for row in group_rows)
        sections.append(
            f'<section class="market-section" data-market-section="{group_id}" aria-label="{name}"{visibility}>'
            f'<header class="market-section-head"><span style="color:{color}">{label}</span><small>{coverage}</small></header>'
            f'{render_market_changes(group_rows)}{rows}</section>')

    inflation_position = next((i for i, group in enumerate(CFG["groups"])
                               if group["id"] == "financial_stability"), len(cards))
    cards.insert(inflation_position, inflation.market_card(ctx["inflation"]))
    sections.insert(inflation_position, inflation.render(ctx["inflation"]))
    return f"""<div class="tab on" id="t1" data-default-market-group="{default_group}">
<div class="market-workspace">
<aside class="market-groups" aria-label="장세 판단 그룹">{"".join(cards)}</aside>
<section class="card market-evidence" aria-labelledby="market-evidence-title">
  <div class="market-evidence-head">
    <h2 id="market-evidence-title" tabindex="-1">{esc(default_label)}</h2>
  </div>
  <div class="market-filters" role="group" aria-label="판정 근거 필터">
    <button type="button" data-market-filter="all" aria-controls="market-evidence-list" aria-pressed="true">전체 지표</button>
    <button type="button" data-market-filter="negative" aria-controls="market-evidence-list" aria-pressed="false">부정 판정</button>
    <button type="button" data-market-filter="changed" aria-controls="market-evidence-list" aria-pressed="false">판정 변경</button>
  </div>
  <p class="market-result" role="status">{ctx["verdicts"][default_group]["expected"]}개 지표</p>
  <p class="market-empty" hidden></p>
  <div id="market-evidence-list">{"".join(sections)}</div>
  <details class="market-guide">
    <summary>판정 및 백분위 읽는 법</summary>
    <ul>
      <li>판정은 긍정이 빨강, 부정이 파랑, 중립이 회색입니다. 각 지표의 판정 점수를 더해 그룹을 판단하며, 갱신 지연·미연결 자료는 합산에서 제외합니다.</li>
      <li>변화폭은 각 지표에 표시된 비교 기간의 값과 대조해 계산합니다. 판정 변경은 직전 관측값에 같은 기준을 적용해 비교합니다.</li>
      <li>과거 백분위는 표시 기간의 관측값 중 현재 값 이하인 비율입니다. 이동평균선 위 종목 비율은 코스피·코스닥 혼합 {ctx["uni_n"]}종목 표본입니다.</li>
    </ul>
  </details>
</section>
</div>
{local_views.MARKET_SCRIPT}
</div>"""


CSS = """
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.45 "Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif}
h1{font-size:17px;margin:0 0 4px}
.meta{color:var(--mu);font-size:12px}
.tab{display:none}.tab.on{display:block}
.card{background:var(--surface);border:1px solid var(--border);border-radius:24px;padding:18px;margin-bottom:14px;box-shadow:var(--card-shadow)}
.jump{margin:-4px 0 8px;color:var(--mu);font-size:12px}.jump a{display:inline-block;margin:2px 4px;padding:3px 10px;border:1px solid var(--line);border-radius:12px;color:var(--tx);text-decoration:none}.jump a:hover{border-color:var(--accent);color:var(--accent)}
.chead{font-size:18px;font-weight:700;margin:2px 0 6px}.chead .cdate{font-size:12px;font-weight:500;color:var(--mu);background:var(--bg);border-radius:5px;padding:2px 8px;margin-right:10px;vertical-align:middle}
.clegend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px 14px;color:var(--mu);font-size:12px;margin:8px 0 4px}
.clegend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
.cpanel{position:relative;border-top:1px solid var(--line);padding-top:4px}.cname{margin:4px 4px 0;font-size:15px;font-weight:600;color:var(--text);font-variant-numeric:tabular-nums}
.cchart{width:100%;height:270px}
.csub{color:var(--text-2);margin:-2px 0 8px}
.xchart{width:100%;height:330px}
.xgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px;margin-top:8px}
.xcard{background:var(--bg);border:1px solid var(--border);border-radius:16px;padding:12px 14px}
.xname{color:var(--text-2);font-weight:600}.xname small{color:var(--mu);font-weight:400;font-size:10.5px;margin-left:4px}
.xval{font-size:24px;font-weight:700;margin-top:2px}.xval small{font-size:12px;color:var(--mu);font-weight:400}
.xchg{display:flex;gap:6px;color:var(--mu);font-size:11px;margin:4px 0 8px}.xchg span{flex:1;background:var(--surface);border-radius:8px;padding:4px 6px}.xchg b{display:block;font-size:13px}
.hm1{font-size:14px;font-weight:600;color:var(--text-2)}
.xbars{display:flex;align-items:flex-end;gap:2px;height:64px}.xbars i{flex:1;background:var(--surface-2);border-radius:2px 2px 0 0}.xbars i:last-child{background:var(--accent)}
.xaxis{display:flex;justify-content:space-between;color:var(--mu);font-size:10.5px;margin-top:3px}
.shead{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px;border-left:3px solid var(--accent)}
.sname b{font-size:20px}.sname small{display:block;color:var(--mu);margin-top:4px;font-size:12px}
.sbadge{border:1px solid var(--accent);color:var(--accent);background:var(--accent-soft);font-weight:700;font-size:12px;padding:1px 6px;margin-left:8px;vertical-align:middle}
.smkt{color:var(--mu);font-size:12px;margin-left:6px}
.sprice{text-align:right}.sprice small{display:block;color:var(--mu);font-size:11px}.sprice b{display:block;font-size:24px}.sprice span{font-weight:700}
.schartwrap{position:relative}.schart{width:100%;height:560px}
.rsiwrap{position:relative;border-top:1px solid var(--border)}.rsichart{width:100%;height:150px}
.sright{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap}.srsi{text-align:right;border-left:1px solid var(--border);padding-left:16px}.srsi small{display:block;color:var(--mu);font-size:11px}
.srsi b{font-size:24px;margin-right:6px}.srsi span{font-weight:700;color:var(--text-2)}
.srsi.hot b,.srsi.hot span{color:var(--warn)}.srsi.cold b,.srsi.cold span{color:var(--series-1)}
@media (max-width:760px){.srsi{border-left:0;padding-left:0;text-align:left}}
.slegend{position:absolute;left:10px;top:8px;z-index:3;font-size:12.5px;color:var(--text-2);pointer-events:none;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
.smabox{position:absolute;left:10px;top:36px;z-index:3;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;color:var(--text-2);font-variant-numeric:tabular-nums;min-width:210px}
.smabox .mh{color:var(--text);font-weight:600;margin-bottom:5px}.smabox table{border-collapse:collapse;width:100%}.smabox td{padding:2px 0}.smabox td+td{text-align:right;padding-left:12px}
.smabox i{display:inline-block;width:10px;height:2px;margin:0 6px 3px 0}.smabox small{color:var(--mu);font-size:10.5px}
.slegend .lname{color:var(--text);font-size:14px;font-weight:600}.slegend b{font-weight:500;margin:0 6px 0 2px}.slegend .lma i{display:inline-block;width:10px;height:2px;margin:0 3px 3px 6px}
.ftab{width:100%;border-collapse:separate;border-spacing:0 4px}
.ftab thead th{color:var(--mu);font-weight:500;font-size:12px;text-align:left;padding:4px 14px}
.ftab tbody th{background:var(--bg);color:var(--text-2);font-weight:500;text-align:left;padding:12px 14px;border-radius:10px 0 0 10px;border-left:3px solid var(--border);width:24%}
.ftab tbody th small{display:block;color:var(--mu);font-size:10.5px;font-weight:400}.ftab tbody th b{color:var(--text)}
.ftab td{text-align:left;padding:12px 14px;font-weight:700;background:var(--bg);font-variant-numeric:tabular-nums}.ftab td:last-child{border-radius:0 10px 10px 0}
.qhead{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 16px;margin-bottom:12px}
.qhead h2{margin:0;font-size:17px;line-height:1.35}
.qhead time,.qhead small,.qtoolbar label,.qshort-context{color:var(--text-3);font-size:12px}
.qtoolbar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:8px 14px;margin-bottom:11px}
.qtoolbar label{display:grid;gap:4px}
.qtoolbar select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:9px;padding:7px 9px}
.qtoolbar select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.qtoolbar .igroup{gap:6px;border:0;overflow:visible}
.qtoolbar .igroup button{border:1px solid var(--border);border-radius:9px;min-height:34px}
.qtoolbar .igroup button[aria-pressed=true]{background:var(--selection);color:var(--selection-text);border-color:var(--selection);font-weight:700}
.qview-label{font-weight:700}
.qinvestor-controls .ictrl{gap:6px;margin-bottom:7px}
.qinvestor-controls .igroup button{padding:5px 9px}
.qinvestor-controls .ilegend{justify-content:flex-start;flex-wrap:wrap;margin-bottom:8px}
.qinvestor-controls .igroup button[aria-pressed=true],.qnet-switch button[aria-pressed=true]{background:var(--bg);color:var(--text);font-weight:700;box-shadow:inset 0 -2px var(--accent)}
.qshort-context{font-variant-numeric:tabular-nums;margin:0 0 7px}
.qtab{border-spacing:0;width:100%;font:inherit;font-variant-numeric:tabular-nums}
.qtab th,.qtab td{padding:9px 6px;border-bottom:1px solid var(--border);background:transparent;border-left:0;border-radius:0;text-align:right;white-space:nowrap}
.qtab thead th{color:var(--text-3);font-weight:600;font-size:12px;border-top:1px solid var(--border)}
.qtab tbody th,.qtab td{color:var(--text)}
.qtab th:first-child{text-align:left}
.qtab tbody tr:last-child :is(th,td){border-bottom:0}
.qnote{color:var(--text-2);font-size:12px;margin:10px 0 0}
.qnote b{color:var(--text)}
.qshort-metrics{display:flex;align-items:baseline;flex-wrap:wrap;gap:12px 30px;margin:4px 0 12px}
.qshort-metrics dt{font-size:12px;color:var(--text-3)}
.qshort-metrics dd{margin:0;font-size:24px;font-weight:700;line-height:1.35;font-variant-numeric:tabular-nums}
.qshort-metrics div+div dd{font-size:19px}
.qshort-metrics dd span{margin-left:1px;font-size:13px}
@media (max-width:480px){
  .qtab th,.qtab td{padding:8px 4px}
  .qshort-card .qtab th span{display:block}
}
@media (pointer:coarse){
  .qtoolbar select,.qtoolbar button,.qinvestor-controls button,.qnet-switch button{min-height:44px}
}
.ftab.fwide{min-width:760px}.ftab td small{display:block;font-size:10.5px;font-weight:500;opacity:.8;margin-top:2px}
.ftab td.fplain{color:var(--text)}.ftab tr.fsep th,.ftab tr.fsep td{box-shadow:inset 0 2px 0 var(--border)}
.itop{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px}.ilegend{display:flex;gap:12px;color:var(--text-2);font-size:12px}.ilegend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.ictrl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}.igroup{display:flex;border:1px solid var(--border);border-radius:10px;overflow:hidden}.igroup button{background:transparent;border:0;border-right:1px solid var(--border);color:var(--text-2);font:inherit;font-size:12px;padding:6px 12px;cursor:pointer}.igroup button:last-child{border-right:0}.igroup button:hover{background:var(--surface-2)}.igroup button.on{background:var(--bg);color:var(--text);font-weight:700;box-shadow:inset 0 -2px var(--accent)}
.ichart{width:100%;height:300px}.ilegend i.idash{width:14px;height:0;border-radius:0;border-top:2px dashed #c39bff;vertical-align:middle}
.fhead{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}.fhead>span:last-child{color:var(--mu);font-size:12px}
.fbanner{margin:0 0 12px;padding:8px 12px;border-radius:10px;font-size:12.5px}
.fbanner.good,.fkpi .good,.rtab .good{color:var(--bad)}
.fbanner.bad,.fkpi .bad,.fbreak td.bad,.rtab .bad{color:var(--accent)}
.fbanner.good,.fbanner.bad{background:color-mix(in srgb,currentColor 10%,transparent)}
.fkpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.fkpi{background:var(--bg);border:1px solid var(--border);border-radius:16px;padding:14px 10px;text-align:center}.fkpi small{display:block;color:var(--mu);font-size:11px}
.fkpi b{display:block;font-size:24px;margin:4px 0;font-variant-numeric:tabular-nums}
.fbody{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:18px;margin-top:16px}.fmode{width:fit-content;margin-bottom:6px}.fchart{width:100%;height:280px}
.fcap{color:var(--mu);font-size:12px;margin-bottom:6px}
.fbreak{border-spacing:0}.fbreak th,.fbreak td{border-top:1px solid var(--border);background:transparent;border-left:0;border-radius:0;padding:9px 4px;font-weight:400;color:var(--text-2);white-space:normal}
.fbreak th small{display:block;color:var(--mu);font-size:10.5px}.fbreak td{text-align:right;color:var(--text);font-variant-numeric:tabular-nums;white-space:nowrap}
.rtab{border-spacing:0;min-width:880px}.rtab thead th{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}
.rtab td,.rtab tbody th{text-align:left;padding:9px 10px;border-top:1px solid var(--border);background:transparent;border-left:0;border-radius:0;vertical-align:top}
.rtab tbody th{font-weight:600;color:var(--text)}.rtab small{display:block;color:var(--mu);font-size:10.5px;font-weight:400}
.rtab td.rnum{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.rtab td.rdate{color:var(--mu);font-variant-numeric:tabular-nums}
.rtab td.rtitle{white-space:normal;min-width:260px}.rtab td.rtitle a{color:var(--text-2);text-decoration:none}.rtab td.rtitle a:hover{color:var(--accent)}
.rop{border:1px solid var(--border);border-radius:6px;padding:1px 7px;font-size:12px}.rop.good,.rop.bad{border-color:color-mix(in srgb,currentColor 50%,transparent)}.rop.warn{color:var(--warn);border-color:rgba(255,191,89,.5)}
.rtab tr.old{opacity:.45}
.fbreak tr.sub th,.fbreak tr.sub td{color:var(--text);font-weight:600}.fbreak tr.total th,.fbreak tr.total td{color:var(--text);font-weight:700;border-top:1px solid var(--text-3)}
.tvcard{padding:0;overflow:hidden}.tvtitle{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)}
.tvtitle b{font-size:16px}.tvtitle span{color:var(--mu);font-size:12px}.tvtitle a{color:var(--mu);text-decoration:none}.tvtitle a:hover{color:var(--accent)}
.tvframe{margin:18px 20px 6px;background:var(--surface);border:1px solid var(--border)}
.tvbar,.tvfoot{display:flex;align-items:center;gap:2px;padding:6px 8px;background:var(--surface)}
.tvbar button,.tvfoot button{background:transparent;border:0;color:var(--text-2);font:inherit;font-size:13px;padding:5px 8px;border-radius:4px;cursor:pointer}
.tvbar button:hover,.tvfoot button:hover{background:var(--surface);color:var(--text)}.tvbar button.on,.tvfoot button.on{background:var(--grid);color:var(--text)}
.tvlbl{color:var(--mu);font-size:12px;margin:0 4px}
.tvbar{flex-wrap:wrap}
.mabtn i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}.tvbar .mabtn:not(.on){opacity:.4}.tvbar .mabtn:not(.on) i{background:transparent!important;border:1px solid var(--mu)}
.tvbar .sep{width:1px;height:20px;background:var(--grid);margin:0 6px}
.tvclock{margin-left:auto;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
.tvcredit{text-align:center;color:var(--mu);font-size:11px;padding:2px 0 14px}
.sec-title{display:inline-block;font-size:16px;font-weight:700;border:1px solid var(--border);background:var(--bg);padding:6px 14px;border-radius:12px;margin-right:10px}
.sec-head{margin-bottom:12px}.sec-head span{color:var(--mu);font-size:12px}
.scroll{overflow-x:auto}
.grid{min-width:700px}
.bar{position:relative;padding-top:15px}
.segs{display:flex;gap:2px}
.seg{flex:1;position:relative}
.seg span{position:absolute;top:-15px;left:0;right:0;text-align:center;font-size:10.5px;color:var(--mu);white-space:nowrap;overflow:hidden}
.seg i{display:block;height:9px;border-radius:2px;opacity:.85}
.marks{position:absolute;left:0;right:0;bottom:0;height:9px}
.dot{position:absolute;top:50%;transform:translate(-50%,-50%);border-radius:50%}
.dot.now{width:15px;height:15px;border:2px solid var(--text);box-shadow:0 0 0 2px #0008}
.dot.prev{width:7px;height:7px;background:var(--bg);border:1.5px solid var(--text)}
.trail{position:absolute;top:50%;border-top:1.5px dotted var(--text);opacity:.7}

.muted{color:var(--mu);font-size:11px}
.legend{color:var(--mu);font-size:11px;margin-top:10px}
table{border-collapse:separate;border-spacing:3px;width:100%}
th{font-weight:600;text-align:left;white-space:nowrap;padding:4px 8px}
thead th{text-align:center;color:var(--mu);font-size:11.5px;font-weight:500}
thead th small{display:block;font-size:10px}
td{padding:6px 8px;text-align:center;white-space:nowrap}
tbody th{background:var(--bg);border-left:3px solid var(--border);border-radius:4px}
.note{color:var(--mu);font-size:12px;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:8px 12px;margin:0 0 10px}
.evtools{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.evfilter{display:flex;flex-wrap:wrap;gap:6px}.evfilter button{background:var(--surface);border:1px solid var(--border);color:var(--text-2);border-radius:10px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
.evfilter button.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}.evonly{color:var(--text-2);font-size:12px;cursor:pointer}
.evlist{columns:2 460px;column-gap:28px}.evmonth{font-size:13px;color:var(--mu);margin:14px 0 2px;break-after:avoid}
.evrow{display:block;break-inside:avoid;padding:12px 4px;border-bottom:1px solid var(--border);color:inherit;text-decoration:none}.evrow:hover .evtitle{color:var(--accent)}
.evtop{display:flex;align-items:center;gap:8px}.evtime{color:var(--mu);font-family:ui-monospace,Consolas,monospace;font-size:13px}.evtime b{color:var(--text-2);font-weight:500}
.evd{font-size:11px;font-weight:700;border-radius:6px;padding:1px 6px;background:var(--surface-2);color:var(--text-2)}.evd:empty{display:none}.evd.today{background:var(--accent-soft);color:var(--accent)}
.evstars{margin-left:auto;color:var(--accent);letter-spacing:1px;white-space:nowrap}.evstars i{color:var(--mu);font-style:normal}
.evtitle{font-weight:700;font-size:14px;margin-top:5px}.evsub{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:5px;color:var(--mu);font-size:11.5px}
.evreg{font-weight:700}.evreg.us{color:var(--series-1)}.evreg.kr{color:var(--good)}.evreg.jp{color:var(--bad)}.evreg.co{color:var(--series-2)}.evreg.mk{color:var(--series-3)}.evreg.cn{color:#ff9f7a}.evreg.eu{color:#7fd6d0}.evreg.tw{color:var(--series-2)}
.evact{color:var(--text)}.evitems{display:flex;flex-wrap:wrap;gap:3px 14px;margin-top:6px;color:var(--text-2);font-size:12px}.evitems b{color:var(--text);font-weight:600}.evwhy{margin-top:6px;color:var(--text-2);font-size:12px;line-height:1.5}
.evrow.past{opacity:.4}.evday .evrow.past{opacity:.8}
.cal{margin:10px 0 18px}.calnav{display:flex;align-items:center;justify-content:center;gap:22px;margin-bottom:10px}.calnav b{font-size:18px;min-width:130px;text-align:center}
.calnav button{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;width:34px;height:32px;font-size:18px;cursor:pointer}
.calnav button:disabled{opacity:.3;cursor:default}.calnav button:not(:disabled):hover{border-color:var(--accent);color:var(--accent)}
.calhead,.calgrid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}.calhead span{text-align:center;color:var(--text-2);font-size:12px;font-weight:700;padding:4px 0}
.calhead span:first-child{color:var(--bad)}.calhead span:last-child{color:var(--series-1)}
.calcell{background:var(--bg);border:1px solid var(--border);border-radius:10px;min-height:112px;padding:7px 8px;min-width:0;cursor:pointer}.calcell:hover{border-color:var(--text-3)}
.calcell.blank{background:transparent;border:0;cursor:default}.calcell.today{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.calcell .cd{font-weight:700;font-size:13px;margin-bottom:4px;color:var(--text)}.calcell.we .cd{color:var(--series-1)}.calcell.sun .cd{color:var(--bad)}.calcell.today .cd{color:var(--accent)}
.calcell .ci{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.55}
.calcell .ci i{flex-shrink:0;width:6px;height:6px;border-radius:50%}.calcell .ci span{overflow:hidden;text-overflow:ellipsis}
.calcell .cmore{color:var(--mu);font-size:11px}
.ci.us i{background:var(--series-1)}.ci.kr i{background:var(--good)}.ci.jp i{background:var(--bad)}.ci.co i,.ci.tw i{background:var(--series-2)}.ci.mk i{background:var(--series-3)}.ci.cn i{background:#ff9f7a}.ci.eu i{background:#7fd6d0}.ci.s3 span{color:var(--text);font-weight:700}
.calcell.picked{border-color:var(--text-2);background:var(--surface-2)}
.evday{background:var(--bg);border:1px solid var(--border);border-radius:14px;padding:6px 16px 4px;margin:-6px 0 14px}
.evdayhead{font-weight:700;font-size:14px;padding:8px 4px 2px}.evday .evrow:last-child{border-bottom:0}.evday .muted{padding:8px 4px}
@media (max-width:760px){.calcell{min-height:52px;padding:5px}.calcell .ci{display:inline-flex}.calcell .ci span,.calcell .cmore{display:none}.calhead,.calgrid{gap:3px}}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.tvbar,.tvfoot{flex-wrap:wrap}.tvframe{margin:12px 8px 6px}.asof{font-size:10px}
@media (max-width:900px){.fbody{grid-template-columns:1fr}}
@media (max-width:760px){.fkpis{grid-template-columns:repeat(2,1fr)}.fkpi b{font-size:19px}.card{padding:12px}.tvclock{width:100%;margin-left:0}.cchart{min-width:500px}.cpanel{overflow-x:auto}}
"""


CREDIT_WINDOW_YEARS = 2
CREDIT_CHANGE_SESSIONS = (1, 5)
CREDIT_CHANGE_MONTHS = (1, 3)


def _extremes(s, half_window=40):
    """주변 ±half_window 거래일 안에서 가장 높거나 낮은 지점 (차트에 값을 적을 곳)."""
    hi = s.rolling(2 * half_window + 1, center=True, min_periods=1).max()
    lo = s.rolling(2 * half_window + 1, center=True, min_periods=1).min()
    picks = s[(s == hi) | (s == lo)]
    picks = picks[~picks.index.duplicated()]
    keep, last = [], None
    for d, v in picks.items():  # 같은 꼭지가 여러 날 이어지면 첫날만
        if last is None or (d - last).days > 20:
            keep.append((d, v))
            last = d
    return keep


def _credit_view(s, unit, decimals):
    s = s.dropna()
    if s.empty:
        return None
    return {
        "unit": unit, "decimals": decimals,
        "line": [[d.strftime("%Y-%m-%d"), round(v, 4)] for d, v in s.items()],
        "labels": [[d.strftime("%Y-%m-%d"), round(v, 4)] for d, v in _extremes(s)],
    }


def credit_changes(credit, sessions, asof):
    """거래일 수와 달력상 개월 수로 이전 공시 잔고를 비교한다."""
    dates = credit.index.union(sessions).sort_values()
    dates = dates[dates <= asof]
    compare_dates = [dates[-days - 1] if len(dates) > days else pd.NaT for days in CREDIT_CHANGE_SESSIONS]
    compare_dates.extend(dates.asof(asof - pd.DateOffset(months=months)) for months in CREDIT_CHANGE_MONTHS)
    current = credit.loc[asof]
    changes = []
    for previous in compare_dates:
        balance = credit.get(previous) if pd.notna(previous) else None
        changes.append({
            "date": previous.strftime("%Y-%m-%d") if pd.notna(previous) else None,
            "amount": int(current - balance) if pd.notna(balance) and balance > 0 else None,
        })
    return changes


def credit_panel(name, credit, index, cap, since):
    """한 시장(코스피/코스닥)의 차트 데이터: 금액(조원)과 시총 대비 비율(%) 두 가지."""
    credit = credit.where(credit > 0)
    if credit.dropna().empty:
        raise ValueError(f"{name} 신용 데이터가 비어 있습니다")
    df = pd.DataFrame({"credit": credit, "index": index}).dropna(subset=["credit"])
    df["index"] = df["index"].ffill()
    df["ratio"] = (credit / cap * 100).reindex(df.index) if cap is not None else np.nan
    df = df[df.index >= since]
    day = lambda d: d.strftime("%Y-%m-%d")

    if df.empty:
        raise ValueError(f"{name} 신용 데이터가 비어 있습니다")
    last_d = df.index[-1]
    return {
        "name": name, "asof": day(last_d),
        "changes": credit_changes(credit, index.dropna().index, last_d),
        "index": [[day(d), round(v, 2)] for d, v in df["index"].dropna().items()],
        "amount": _credit_view(df["credit"] / 1e12, "조", 1),
        "ratio": _credit_view(df["ratio"], "%", 2),
    }


def credit_charts(px):
    """신용잔고 탭 데이터. 코스피 시총은 ECOS, 코스닥 시총·지수는 KRX 로그인으로 받는다."""
    since = TODAY - pd.DateOffset(years=CREDIT_WINDOW_YEARS)
    credit = kofia("STATSCU0100000070BO")
    notes = []
    try:
        kospi_cap = kospi_market_cap()
    except Exception as e:
        kospi_cap, notes = None, notes + [f"코스피 시가총액 수신 실패 ({e.__class__.__name__}) · 금액 차트만 표시"]
    try:
        kosdaq = krx_index("2001", first=(since - pd.DateOffset(months=2)).strftime("%Y%m%d"))
        kosdaq_close, kosdaq_cap = kosdaq["close"], kosdaq["cap"]
    except Exception as e:
        kosdaq_close, kosdaq_cap, notes = px["^KQ11"].dropna(), None, notes + [f"코스닥 시가총액 수신 실패 ({e.__class__.__name__}) · Yahoo 지수와 금액 차트 표시"]

    panels = []
    for name, column, index, cap in [("코스피", "TMPV3", px["^KS11"].dropna(), kospi_cap),
                                     ("코스닥", "TMPV4", kosdaq_close, kosdaq_cap)]:
        panels.append(credit_panel(name, credit[column], index, cap, since))
    return {"panels": panels, "notes": notes}


def deposit_index(view, kospi_close):
    first, last = (pd.Timestamp(view["line"][i][0]) for i in (0, -1))
    return [[date.strftime("%Y-%m-%d"), round(value, 2)]
            for date, value in kospi_close.loc[first:last].dropna().items()]


def deposit_panel(calc, kospi_close):
    since = TODAY - pd.DateOffset(years=CREDIT_WINDOW_YEARS)
    amount = kofia(calc["table"])[calc["column"]].loc[since:]
    view = _credit_view(amount[amount > 0] / 1e12, "조", 2)
    if view is None:
        raise ValueError("투자자예탁금 데이터가 비어 있습니다")
    return {"name": "투자자예탁금", "amount": view, "index": deposit_index(view, kospi_close)}


ECHARTS_URL = "https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"
LWC_URL = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"

def render_credit_changes(panels):
    def change_cell(change):
        amount = change["amount"]
        if amount is None:
            return "<td>자료 없음</td>"
        value = abs(amount) / 1e8
        number = f"{value:,.0f}" if value >= 1 else f"{value:,.2f}"
        label = f"+{number}억 원 증가" if amount > 0 else f"−{number}억 원 감소" if amount < 0 else "0원 (보합)"
        return f'<td>{label}<br><small>{esc(change["date"])} 대비</small></td>'

    rows = "".join(
        f'<tr><th scope="row">{esc(p["name"])}</th><td>{esc(p["asof"])}</td>'
        + "".join(change_cell(change) for change in p["changes"]) + "</tr>"
        for p in panels)
    periods = (
        "".join(f'<th scope="col">{days}거래일 증감</th>' for days in CREDIT_CHANGE_SESSIONS)
        + "".join(f'<th scope="col">{months}개월 증감</th>' for months in CREDIT_CHANGE_MONTHS)
    )
    return ('<div class="scroll"><table aria-label="시장별 신용잔고 증감액"><thead><tr>'
            '<th scope="col">시장</th><th scope="col">잔고 기준일</th>'
            f'{periods}</tr></thead><tbody>{rows}</tbody></table></div>')


def credit_index_heading(panel, name):
    index = panel.get("index")
    label = esc(name)
    if not index:
        return f'<div class="cname">{label}</div>'
    date, close = index[-1]
    return f'<div class="cname" title="{esc(date)} 종가">{label} : {close:,.0f}</div>'


def credit_balance_heading(panel, label):
    date, amount = panel["amount"]["line"][-1]
    return (f'<div class="chead"><time class="cdate" datetime="{esc(date)}">{esc(date)}</time>'
            f'{esc(label)} {amount:,.2f}조 원</div>')


def render_credit(data):
    """신용잔고와 투자자예탁금 차트를 표시한다."""
    panels = data["panels"]
    deposit = data["deposit"]
    notes = "".join(f'<p class="note">{esc(n)}</p>' for n in data["notes"])
    if deposit is None:
        notes += '<p class="note">투자자예탁금 데이터를 받지 못했습니다.</p>'
    if not panels and deposit is None:
        return notes

    legend = '<div class="clegend">'
    if panels:
        legend += ('<span><i style="background:var(--series-1)"></i>코스피 신용</span>'
                   '<span><i style="background:var(--series-2)"></i>코스닥 신용</span>')
    if deposit is not None:
        legend += '<span><i style="background:var(--good)"></i>투자자예탁금</span>'
    legend += '<span><i style="background:var(--text-3)"></i>지수</span></div>'
    changes = render_credit_changes(panels) if panels else ""
    charts = "".join(
        f'<div class="cpanel">{credit_balance_heading(p, p["name"] + " 신용잔고")}'
        f'{credit_index_heading(p, p["name"])}{trend_charts.controls()}<div class="cchart" data-credit="{i}:amount"></div></div>'
        for i, p in enumerate(panels))
    if deposit is not None:
        index_heading = credit_index_heading(deposit, "코스피") if deposit.get("index") else ""
        charts += (
            f'<div class="cpanel">{credit_balance_heading(deposit, "투자자예탁금")}'
            f'{index_heading}{trend_charts.controls()}<div class="cchart" data-credit="deposit:amount"></div></div>')
    source = f'<div class="legend">※ 출처: 금융투자협회 · 최근 {CREDIT_WINDOW_YEARS}년 일별 잔고</div>'
    return f'{notes}<div class="card">{changes}{legend}{charts}{source}</div>'


STOCK_JS = r"""
// TradingView Lightweight Charts. TradingView 위젯은 KRX 종목을 막아 두어, 같은 엔진에 시세를 직접 넣고
// IV Terminal의 TradingView 카드 모양(툴바 · 정보줄 · 기간 버튼 · 시계)을 따라 만든다.
const STOCKS = __DATA__;
const TV = {};
const UP = '#26a69a', DOWN = '#ef5350';  // TradingView 기본 색 (IV Terminal 차트와 같음)
// 가격 눈금: 원 단위 정수, 거래량 공간으로 내려간 0 이하 눈금은 숨긴다.
const PRICE_FMT = {type: 'custom', minMove: 1, formatter: p => p > 0 ? Math.round(p).toLocaleString() : ''};
const MA = [[5, plotColor('text')], [20, plotColor('series-2')], [60, plotColor('series-1')], [120, plotColor('good')], [240, plotColor('series-3')]];
const IV_LABEL = {D: '1D', W: '1W', M: '1M'};
function resample(rows, iv) {
  if (iv === 'D') return rows;
  const out = [];
  for (const r of rows) {
    let key = r[0].slice(0, 8) + '01';
    if (iv === 'W') { const d = new Date(r[0] + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7)); key = d.toISOString().slice(0, 10); }
    const last = out[out.length - 1];
    if (last && last[0] === key) { last[2] = Math.max(last[2], r[2]); last[3] = Math.min(last[3], r[3]); last[4] = r[4]; last[5] += r[5]; }
    else out.push([key, r[1], r[2], r[3], r[4], r[5]]);
  }
  return out;
}
function maSeries(rows, n) {
  let sum = 0; const out = [];
  rows.forEach((r, i) => { sum += r[4]; if (i >= n) sum -= rows[i - n][4]; if (i >= n - 1) out.push({time: r[0], value: Math.round(sum / n)}); });
  return out;
}
// RSI(14, 와일더). 앞 14봉은 빈 값으로 두어 주가 차트와 봉 위치(논리 번호)를 맞춘다.
const RSI_N = 14, RSI_HI = 70, RSI_LO = 30, RSI_COLOR = plotColor('series-3');
function rsiSeries(rows) {
  const out = []; let up = 0, down = 0;
  rows.forEach((r, i) => {
    if (i === 0) { out.push({time: r[0]}); return; }
    const d = r[4] - rows[i - 1][4], g = Math.max(d, 0), l = Math.max(-d, 0);
    if (i <= RSI_N) { up += g / RSI_N; down += l / RSI_N; }
    else { up = (up * (RSI_N - 1) + g) / RSI_N; down = (down * (RSI_N - 1) + l) / RSI_N; }
    out.push(i < RSI_N ? {time: r[0]} : {time: r[0], value: down === 0 ? 100 : 100 - 100 / (1 + up / down)});
  });
  return out;
}
function rsiLegend(id, i) {
  const t = TV[id], p = t.rsiData[i], el = document.getElementById('rsilegend-' + id);
  if (!el) return;
  const v = p && p.value !== undefined ? p.value : null;
  const tag = v === null ? '' : v >= RSI_HI ? ' <b style="color:var(--warn)">과매수</b>' : v <= RSI_LO ? ' <b style="color:var(--series-1)">과매도</b>' : ' <b>중립</b>';
  el.innerHTML = '<span>RSI ' + RSI_N + '</span><b style="color:' + RSI_COLOR + '">' + (v === null ? '—' : v.toFixed(1)) + '</b>' + tag;
}
function setOn(attr, id, value) {
  const key = {range: 'r', iv: 'iv'}[attr];
  document.querySelectorAll('[data-' + attr + '-for="' + id + '"]').forEach(b => b.classList.toggle('on', b.dataset[key] === value));
}
function render(id) {
  const t = TV[id], chart = t.chart, rows = resample(t.s.ohlcv, t.iv);
  t.rows = rows; t.byTime = {}; rows.forEach((r, i) => t.byTime[r[0]] = i);
  (t.series || []).forEach(x => chart.removeSeries(x));
  const price = chart.addCandlestickSeries({upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN, borderVisible: false, priceFormat: PRICE_FMT});
  price.setData(rows.map(r => ({time: r[0], open: r[1], high: r[2], low: r[3], close: r[4]})));
  price.priceScale().applyOptions({scaleMargins: {top: 0.08, bottom: 0.2}});
  const vol = chart.addHistogramSeries({priceScaleId: 'vol', priceLineVisible: false,
    priceFormat: {type: 'custom', minMove: 1, formatter: v => v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : Math.round(v / 1e3) + 'K'}});
  vol.priceScale().applyOptions({scaleMargins: {top: 0.82, bottom: 0}});
  vol.setData(rows.map((r, i) => ({time: r[0], value: r[5], color: i && r[4] < rows[i - 1][4] ? 'rgba(239,83,80,0.5)' : 'rgba(38,166,154,0.5)'})));
  t.maAt = {};
  const mas = MA.filter(([n]) => t.ma[n]).map(([n, color]) => { const l = chart.addLineSeries({color, lineWidth: 1, priceLineVisible: false, priceFormat: PRICE_FMT,
    lastValueVisible: false, crosshairMarkerVisible: false}); const data = maSeries(rows, n); l.setData(data);
    t.maAt[n] = Object.fromEntries(data.map(p => [p.time, p.value])); return l; });
  t.series = [price, vol, ...mas];
  if (t.rsiChart) {
    if (t.rsiLine) t.rsiChart.removeSeries(t.rsiLine);
    t.rsiData = rsiSeries(rows);
    t.rsiLine = t.rsiChart.addLineSeries({color: RSI_COLOR, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true,
      priceFormat: {type: 'custom', minMove: 0.1, formatter: v => v.toFixed(1)},
      autoscaleInfoProvider: () => ({priceRange: {minValue: 0, maxValue: 100}})});
    t.rsiLine.setData(t.rsiData);
    [[RSI_HI, plotColor('warn'), '과매수'], [50, '#5d686d', ''], [RSI_LO, plotColor('series-1'), '과매도']].forEach(([price, color, title]) =>
      t.rsiLine.createPriceLine({price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: !!title, title}));
    rsiLegend(id, rows.length - 1);
  }
  showLegend(id, rows.length - 1);
}

function stockChangeTextColor(change) {
  return change > 0 ? 'var(--bad)' : change < 0 ? 'var(--accent)' : 'var(--text)';
}

function showLegend(id, i) {
  const t = TV[id], r = t.rows[i], prev = i ? t.rows[i - 1][4] : r[1], diff = r[4] - prev, pct = diff / prev * 100;
  const c = stockChangeTextColor(diff), f = v => v.toLocaleString(), sign = diff >= 0 ? '+' : '';
  const shown = MA.filter(([n]) => t.ma[n]);
  const ma = shown.length ? '<span class="lma">' + shown.map(([n, col]) => '<i style="background:' + col + '"></i>' + n).join(' ') + '</span>' : '';
  document.getElementById('legend-' + id).innerHTML =
    '<span class="lname">' + t.s.name + ' · ' + IV_LABEL[t.iv] + ' · KRX</span><span>'
    + ['O', 'H', 'L', 'C'].map((k, j) => k + '<b style="color:' + c + '">' + f(r[j + 1]) + '</b>').join(' ')
    + ' <b style="color:' + c + '">' + sign + f(diff) + ' (' + sign + pct.toFixed(2) + '%)</b></span>' + ma;
  // 이동평균 상자: 그 봉의 이동평균 가격과, 종가가 그 선보다 몇 % 위(+)·아래(−)인지.
  const box = document.getElementById('mabox-' + id);
  if (!box) return;
  const lines = shown.map(([n, col]) => {
    const v = (t.maAt[n] || {})[r[0]];
    if (v === undefined) return '<tr><td><i style="background:' + col + '"></i>' + n + '선</td><td>—</td><td>—</td></tr>';
    const gap = (r[4] - v) / v * 100, gc = stockChangeTextColor(gap);
    return '<tr><td><i style="background:' + col + '"></i>' + n + '선</td><td>' + f(v) + '</td><td style="color:' + gc + '">'
      + (gap >= 0 ? '+' : '') + gap.toFixed(2) + '%</td></tr>';
  }).join('');
  box.hidden = !shown.length;
  box.innerHTML = '<div class="mh">' + r[0] + ' · 종가 ' + f(r[4]) + '</div><table>' + lines + '</table>'
    + '<small>% = 종가가 이동평균선보다 위(+)·아래(−)</small>';
}
function tvRange(id, r) {
  const t = TV[id]; if (!t) return;
  const ts = t.chart.timeScale(), end = t.rows[t.rows.length - 1][0];
  if (r === 'All') ts.fitContent();
  else {
    const from = new Date(end + 'T00:00:00Z');
    if (r === 'YTD') { from.setUTCDate(1); from.setUTCMonth(0); }
    else {
      const day = from.getUTCDate(); from.setUTCDate(1);
      from.setUTCMonth(from.getUTCMonth() - {'1M': 1, '3M': 3, '6M': 6, '1Y': 12, '5Y': 60}[r]);
      const monthEnd = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth() + 1, 0)).getUTCDate();
      from.setUTCDate(Math.min(day, monthEnd));
    }
    const earliest = t.rows[0][0];
    if (from.toISOString().slice(0, 10) < earliest) from.setTime(new Date(earliest + 'T00:00:00Z').getTime());
    ts.setVisibleRange({from: from.toISOString().slice(0, 10), to: end});
  }
  t.range = r; setOn('range', id, r);
}
function tvInterval(id, iv) { const t = TV[id]; if (!t) return; t.iv = iv; render(id); tvRange(id, t.range); setOn('iv', id, iv); }
function tvMA(id, n) {
  const t = TV[id]; if (!t) return; t.ma[n] = !t.ma[n]; render(id); tvRange(id, t.range);
  document.querySelector('[data-ma-for="' + id + '"][data-n="' + n + '"]').classList.toggle('on', t.ma[n]);
}
function drawStock(id) {
  if (TV[id]) return;
  if (typeof LightweightCharts === 'undefined') { chartUnavailable('s-' + id); return; }
  const s = STOCKS.find(x => x.id === id), el = document.getElementById('stock-' + id);
  if (!s || !el || !s.ohlcv || !s.ohlcv.length) return;
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: {background: {color: plotColor('surface')}, textColor: plotColor('text-2'), fontSize: 11},
    grid: {vertLines: {color: plotColor('grid')}, horzLines: {color: plotColor('grid')}},
    rightPriceScale: {borderColor: plotColor('border')}, timeScale: {borderColor: plotColor('border'), rightOffset: 6},
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
    localization: {locale: 'ko-KR', dateFormat: 'yyyy-MM-dd'},
  });
  TV[id] = {s, chart, iv: 'D', ma: Object.fromEntries(MA.map(([n]) => [n, true])), range: '1Y'};
  // RSI 보조 차트: 주가 차트와 가격 눈금 폭을 같게 해 봉 위치를 맞추고, 보이는 구간을 서로 따라가게 한다.
  const rsiEl = document.getElementById('rsi-' + id);
  if (rsiEl) {
    const rsiChart = LightweightCharts.createChart(rsiEl, {
      autoSize: true,
      layout: {background: {color: plotColor('surface')}, textColor: plotColor('text-2'), fontSize: 11},
      grid: {vertLines: {color: plotColor('grid')}, horzLines: {visible: false}},
      rightPriceScale: {borderColor: plotColor('border'), minimumWidth: 80, scaleMargins: {top: 0.1, bottom: 0.1}},
      timeScale: {borderColor: plotColor('border'), rightOffset: 6, visible: false},
      crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
      localization: {locale: 'ko-KR', dateFormat: 'yyyy-MM-dd'},
      handleScale: false, handleScroll: false,
    });
    chart.applyOptions({rightPriceScale: {minimumWidth: 80}});
    TV[id].rsiChart = rsiChart;
    chart.timeScale().subscribeVisibleLogicalRangeChange(r => { if (r) rsiChart.timeScale().setVisibleLogicalRange(r); });
  }
  render(id);
  chart.subscribeCrosshairMove(param => {
    const t = TV[id], i = param.time !== undefined ? t.byTime[param.time] : undefined;
    showLegend(id, i !== undefined ? i : t.rows.length - 1);
    if (t.rsiChart) {
      rsiLegend(id, i !== undefined ? i : t.rows.length - 1);
      const p = i !== undefined ? t.rsiData[i] : null;
      if (p && p.value !== undefined) t.rsiChart.setCrosshairPosition(p.value, p.time, t.rsiLine);
      else t.rsiChart.clearCrosshairPosition();
    }
  });
  tvRange(id, '1Y'); setOn('iv', id, 'D');
  const clock = document.getElementById('clock-' + id);
  const tick = () => { clock.textContent = new Date().toLocaleTimeString('en-GB', {timeZone: 'Asia/Seoul'}) + ' UTC+9'; };
  tick(); TV[id].clockTimer = setInterval(tick, 1000);
  drawFlow(id);
  drawFcf(id);
}

// 종목 수급 차트와 순매수 표의 선택 상태는 서로 독립적이다.
const WHO = [['외국인', plotColor('series-1')], ['기관', plotColor('series-2')], ['개인', plotColor('text-2')]];
const FLOW = {};

function flowTableSet(button) {
  const card = button.closest('.qnet-card');
  for (const peer of card.querySelectorAll('[data-qnet]')) {
    peer.setAttribute('aria-pressed', String(peer === button));
  }
  for (const panel of card.querySelectorAll('[data-qnet-panel]')) {
    panel.hidden = panel.dataset.qnetPanel !== button.dataset.qnet;
  }
}

function flowSet(id, key, value) {
  const f = FLOW[id];
  if (!f) return;
  f[key] = value;
  renderFlow(id);
}

function renderFlow(id) {
  const f = FLOW[id], d = f.d;
  const start = Math.max(0, d.dates.length - Number(f.p));
  const dates = d.dates.slice(start);
  const shortOnly = f.view === 'short';
  const bars = !shortOnly && f.mode === 'day';
  const unit = f.unit === 'volume' ? '주' : '억원';
  const fmt = v => Math.round(v).toLocaleString();
  const series = shortOnly ? [] : WHO.filter(([name]) => f.who === 'all' || f.who === name).map(([name, color]) => {
    let data = d[f.unit][name].slice(start);
    if (!bars) {
      let cumulative = 0;
      data = data.map(value => (cumulative += value));
    }
    return bars
      ? {name, type: 'bar', data, itemStyle: {color}, barMaxWidth: 10}
      : {name, type: 'line', data, showSymbol: false, lineStyle: {color, width: 2}, itemStyle: {color}};
  });
  if (series.length) {
    series[0].markLine = {symbol: 'none', silent: true, label: {show: false},
      lineStyle: {color: plotColor('text-3'), type: 'dashed'}, data: [{yAxis: 0}]};
  }
  const bal = (d.short_bal || []).slice(start);
  const hasShort = bal.some(Number.isFinite);
  const shortColor = plotColor('series-3');
  const shortName = '공매도 잔고비중';
  if (hasShort) {
    series.push({name: shortName, type: 'line', yAxisIndex: shortOnly ? 0 : 1, data: bal,
      connectNulls: true, showSymbol: false, itemStyle: {color: shortColor},
      lineStyle: {color: shortColor, width: shortOnly ? 2 : 1.5, type: shortOnly ? 'solid' : 'dashed'}});
  }
  const shortAxis = {type: 'value', position: 'right', name: '잔고 비중(%)', show: hasShort,
    scale: true, nameTextStyle: {color: shortColor, fontSize: 11},
    splitLine: {show: shortOnly, lineStyle: {color: plotColor('grid')}},
    axisLabel: {color: shortColor, fontSize: 10, formatter: v => v.toFixed(2)}};
  const investorAxis = {type: 'value', position: 'left', name: '(' + unit + ')',
    nameTextStyle: {color: plotColor('text-3'), fontSize: 11}, splitLine: {lineStyle: {color: plotColor('grid')}},
    axisLabel: {color: plotColor('text-3'), fontSize: 10, formatter: fmt}};
  const card = f.card;
  card.querySelector('.qinvestor-controls').hidden = shortOnly;
  for (const label of card.querySelectorAll('[data-flow-legend]')) {
    label.hidden = f.who !== 'all' && f.who !== label.dataset.flowLegend;
  }
  const shortLegend = card.querySelector('.qshort-legend');
  if (shortLegend) shortLegend.hidden = !hasShort;
  const status = card.querySelector('.qshort-context');
  status.hidden = !shortOnly;
  if (shortOnly) {
    const last = bal.findLastIndex(Number.isFinite);
    status.textContent = hasShort ? '최근 공시 ' + dates[last] + ' · ' + bal[last].toFixed(3) + '%' : '선택한 기간에 공시된 잔고비중이 없습니다.';
  }
  f.chart.getDom().setAttribute('aria-label', shortOnly ? '공매도 잔고비중' : '투자자별 순매수와 공매도 잔고비중');
  f.chart.setOption({
    animation: false, backgroundColor: 'transparent',
    grid: {left: shortOnly ? 16 : 55, right: hasShort ? 53 : 18, top: 28, bottom: 28},
    tooltip: {trigger: 'axis', backgroundColor: plotColor('surface'), borderColor: plotColor('border'),
      textStyle: {color: plotColor('text'), fontSize: 12},
      formatter: points => {
        if (!points.length) return '';
        return points[0].axisValue + '<br>' + points.map(point => {
          const value = point.value;
          const shown = !Number.isFinite(value) ? '—' : point.seriesName === shortName
            ? value.toFixed(3) + '%' : (value > 0 ? '+' : '') + fmt(value) + unit;
          return point.marker + point.seriesName + ' <b>' + shown + '</b>';
        }).join('<br>');
      }},
    xAxis: {type: 'category', data: dates, boundaryGap: bars, axisLine: {lineStyle: {color: plotColor('border')}},
      axisTick: {show: false}, axisLabel: {color: plotColor('text-3'), fontSize: 11, formatter: v => v.slice(5).replace('-', '.')}},
    yAxis: shortOnly ? [shortAxis] : [investorAxis, shortAxis],
    series,
  }, true);
  for (const button of card.querySelectorAll('button[data-k]')) {
    button.setAttribute('aria-pressed', String(f[button.dataset.k] === button.dataset.v));
  }
}

function drawFlow(id) {
  if (FLOW[id] || typeof echarts === 'undefined') return;
  const stock = STOCKS.find(item => item.id === id);
  const el = document.getElementById('flowchart-' + id);
  if (!el || !stock?.flow?.daily) return;
  const card = el.closest('.qchart-card');
  FLOW[id] = {d: stock.flow.daily, card, view: 'investors', who: 'all', mode: 'cum', unit: 'value',
    p: card.querySelector('select').value, chart: echarts.init(el, null, {renderer: 'canvas'})};
  renderFlow(id);
}
window.addEventListener('resize', () => Object.values(FLOW).forEach(f => f.chart.resize()));

// 잉여현금흐름 막대 (ECharts): 연간 / 분기별 TTM. 플러스는 회청색, 마이너스는 빨강 (IV Terminal 카드와 같음).
const FCF = {};
function krw(v, d) { const a = Math.abs(v); return (v < 0 ? '-' : '') + (a >= 1e12 ? (a / 1e12).toFixed(d) + '조' : Math.round(a / 1e8).toLocaleString() + '억'); }
function fcfMode(id, m) {
  const f = FCF[id]; if (!f) return; f.m = m;
  const pts = f.d[m];
  f.chart.setOption({
    animation: false, backgroundColor: 'transparent',
    grid: {left: 8, right: 64, top: 16, bottom: 24},
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow', shadowStyle: {color: 'rgba(130,145,160,0.12)'}},
      backgroundColor: plotColor('surface'), borderColor: plotColor('border'), textStyle: {color: plotColor('text'), fontSize: 12},
      formatter: ps => ps[0].name + '<br>FCF <b>' + krw(ps[0].value, 1) + '원</b>'},
    xAxis: {type: 'category', data: pts.map(p => p[0]), axisLine: {lineStyle: {color: plotColor('border')}}, axisTick: {show: false},
      axisLabel: {color: plotColor('text-3'), fontSize: 10}},
    yAxis: {type: 'value', position: 'right', splitLine: {lineStyle: {color: plotColor('grid')}}, axisLabel: {color: plotColor('text-3'), fontSize: 10, formatter: v => krw(v, 0)}},
    series: [{type: 'bar', barMaxWidth: m === 'annual' ? 60 : 18, data: pts.map(p => ({value: p[1], itemStyle: {color: p[1] >= 0 ? plotColor('bar') : plotColor('bad'), borderRadius: p[1] >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2]}})),
      markLine: {symbol: 'none', silent: true, label: {show: false}, lineStyle: {color: plotColor('text-3'), type: 'solid', width: 1}, data: [{yAxis: 0}]}}],
  }, true);
  document.querySelectorAll('[data-fcf-for="' + id + '"]').forEach(b => b.classList.toggle('on', b.dataset.m === m));
}
function drawFcf(id) {
  if (FCF[id] || typeof echarts === 'undefined') return;
  const s = STOCKS.find(x => x.id === id), el = document.getElementById('fcf-' + id);
  if (!s || !el || !s.fcf) return;
  FCF[id] = {d: s.fcf, m: 'annual', chart: echarts.init(el, null, {renderer: 'canvas'})};
  fcfMode(id, 'annual');
}
window.addEventListener('resize', () => Object.values(FCF).forEach(f => f.chart.resize()));
"""


def _won(v):
    """원 → 억원/조원 (부호 포함)."""
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    a = abs(v)
    return f"{sign}{a / 1e12:,.2f}조원" if a >= 1e12 else f"{sign}{a / 1e8:,.0f}억원"


def render_flow(f, i):
    """투자자별 순매수와 공매도 차트, 기간별 순매수 표와 공매도 카드."""
    short_balance = f["short_bal"]
    short_trade = f["short_trade"]
    chart_id = f"flowchart-{i}"

    def group(key, label, options):
        buttons = "".join(
            f'<button type="button" data-k="{key}" data-v="{value}" '
            f'aria-controls="{chart_id}" aria-pressed="{str(index == 0).lower()}" '
            f"""onclick="flowSet('{i}', '{key}', '{value}')">{text}</button>"""
            for index, (value, text) in enumerate(options)
        )
        return f'<div class="igroup" role="group" aria-label="{label}">{buttons}</div>'

    if short_balance:
        views = group("view", "수급 차트 종류", [("investors", "투자자 순매수"), ("short", "공매도 잔고비중")])
    else:
        views = '<span class="qview-label">투자자 순매수</span>'
    periods = "".join(f'<option value="{n}">{n}거래일</option>' for n in (30, 60, 120))
    legend = "".join(
        f'<span data-flow-legend="{name}"><i style="background:{color}"></i>{name}</span>'
        for name, color in [("외국인", "var(--series-1)"), ("기관", "var(--series-2)"), ("개인", "var(--text-2)")]
    )
    if short_balance:
        legend += '<span class="qshort-legend"><i class="idash"></i>공매도 잔고비중 (오른쪽 축)</span>'
    filters = (
        group("who", "투자자", [("all", "전체"), ("외국인", "외국인"), ("기관", "기관"), ("개인", "개인")])
        + group("mode", "집계 방식", [("cum", "누적"), ("day", "일별")])
        + group("unit", "순매수 단위", [("value", "금액(억원)"), ("volume", "수량(주)")])
    )
    chart = f"""
<section class="card qchart-card" aria-labelledby="qchart-title-{i}">
  <header class="qhead">
    <h2 id="qchart-title-{i}">수급</h2><time datetime="{f["date"]}">{f["date"]} 기준</time>
  </header>
  <div class="qtoolbar">
    {views}
    <label>기간<select aria-controls="{chart_id}" onchange="flowSet('{i}', 'p', this.value)">{periods}</select></label>
  </div>
  <div class="qinvestor-controls">
    <div class="ictrl">{filters}</div>
    <div class="ilegend">{legend}</div>
  </div>
  <div class="qshort-context" aria-live="polite" hidden></div>
  <div id="{chart_id}" class="ichart" role="img" aria-label="투자자별 누적 순매수"></div>
  <div class="legend">누적은 선택한 기간의 첫 거래일부터 더한 값입니다. 출처는 KRX 투자자별 거래실적과 공매도입니다.</div>
</section>"""

    def number(value, spec, suffix=""):
        return "—" if value is None else f"{value:{spec}}{suffix}".replace("-", "−")

    def net_table(key, caption, spec, scale=1, suffix=""):
        head = "".join(f'<th scope="col">{n}일</th>' for n in FLOW_PERIODS)
        rows = []
        for name in ("외국인", "기관", "개인"):
            values = (f["rows"][name][n] for n in FLOW_PERIODS)
            cells = "".join(f'<td>{number(row[key] / scale if row else None, spec, suffix)}</td>' for row in values)
            rows.append(f'<tr><th scope="row">{name}</th>{cells}</tr>')
        hidden = "" if key == "won" else " hidden"
        return (f'<div class="scroll" data-qnet-panel="{key}"{hidden}>'
                f'<table class="qtab" aria-label="{caption}"><thead><tr>'
                f'<th scope="col">투자자</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')

    amount = net_table("won", "투자자별 누적 순매수 금액 (억원)", "+,.0f", 1e8)
    ratio = net_table("pct", "투자자별 누적 순매수의 시총 대비 비율 (%)", "+.2f", suffix="%")
    streak = f'{f["streak"]}일 연속 {"순매수" if f["streak_buy"] else "순매도"}'
    net = f"""
<section class="card qnet-card" aria-labelledby="qnet-title-{i}">
  <header class="qhead">
    <div><h2 id="qnet-title-{i}">투자자별 순매수</h2><small>기간별 누적 · 거래일 기준</small></div>
    <div class="igroup qnet-switch" role="group" aria-label="순매수 표의 표시 단위">
      <button type="button" aria-pressed="true" aria-controls="qnet-data-{i}" data-qnet="won" onclick="flowTableSet(this)">금액(억원)</button>
      <button type="button" aria-pressed="false" aria-controls="qnet-data-{i}" data-qnet="pct" onclick="flowTableSet(this)">시총 대비(%)</button>
    </div>
  </header>
  <div id="qnet-data-{i}">{amount}{ratio}</div>
  <div class="qnote">기관·외국인 합산 · 최근 30일 중 <b>{f["buy_days"]}일</b> 순매수 ({f["buy_days"] / 30 * 100:.0f}%) · {streak}</div>
  <div class="legend">시총 대비 비율은 현재 시가총액을 기준으로 계산합니다.</div>
</section>"""

    date = f'<time datetime="{short_balance["date"]}">잔고 기준 {short_balance["date"]}</time>' if short_balance else ""
    now = f'<div><dt>현재 잔고비중</dt><dd>{short_balance["now"]:.3f}<span>%</span></dd></div>' if short_balance else ""
    trade_now = short_trade[1]
    trade_value = f"{trade_now:.2f}<span>%</span>" if trade_now is not None else "—"
    change_header = '<th scope="col">잔고비중 변화 <span>(%p)</span></th>' if short_balance else ""
    rows = []
    for n in FLOW_PERIODS[1:]:
        change = f'<td>{number(short_balance["chg"][n], "+.3f")}</td>' if short_balance else ""
        rows.append(f'<tr><th scope="row">{n}일</th>{change}<td>{number(short_trade[n], ".2f")}</td></tr>')
    shorts = f"""
<section class="card qshort-card" aria-labelledby="qshort-title-{i}">
  <header class="qhead"><h2 id="qshort-title-{i}">공매도</h2>{date}</header>
  <dl class="qshort-metrics">{now}<div><dt>당일 거래비중</dt><dd>{trade_value}</dd></div></dl>
  <div class="scroll"><table class="qtab" aria-label="기간별 공매도 지표">
    <thead><tr><th scope="col">기간</th>{change_header}<th scope="col">평균 거래비중 <span>(%)</span></th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
  <div class="legend">잔고비중은 상장주식수 대비 공매도 잔고이며, 2거래일 늦게 공시됩니다. 거래비중은 전체 거래량 대비 비율입니다.</div>
</section>"""
    return chart + net + shorts


def _krw(v):
    """원 → 조원/억원 (음수만 부호)."""
    if v is None:
        return "—"
    a = abs(v)
    text = f"{a / 1e12:,.1f}조원" if a >= 1e12 else f"{a / 1e8:,.0f}억원"
    return ("-" if v < 0 else "") + text


def render_fcf(c):
    """잉여현금흐름 분석 카드: 3년 누적 판정 · 핵심 수치 4개 · 연간/분기별 TTM 막대 · 최근 구성."""
    head = '<div class="fhead"><span class="sec-title">잉여현금흐름 분석</span><span>DART 연결재무제표 · 원</span></div>'
    f = c.get("fcf")
    if not f:
        return f'<div class="card">{head}<p class="note">{esc(c.get("fcf_note") or "잉여현금흐름을 계산할 데이터가 부족합니다.")}</p></div>'
    banner = ""
    if f["sum3"] is not None:
        ok = f["sum3"] >= 0
        banner = (f'<p class="fbanner {"good" if ok else "bad"}">{"✓" if ok else "!"} 최근 3개 회계연도 누적 잉여현금흐름이 '
                  f'{"플러스" if ok else "마이너스"}입니다 ({_krw(f["sum3"])}).</p>')

    def pct(v):
        return ("—", "") if v is None else (f"{v * 100:.0f}%", "good" if v >= 0 else "bad")

    margin, conv = pct(f["margin"]), pct(f["conversion"])
    kpis = [("최근 값", _krw(f["last"]), "", f["label"]),
            ("3년 평균", _krw(f["avg3"]), "", f["avg3_label"]),
            ("FCF 마진", margin[0], margin[1], "잉여현금흐름 ÷ 매출"),
            ("현금 전환율", conv[0], conv[1], "잉여현금흐름 ÷ 순이익")]
    tiles = "".join(f'<div class="fkpi"><small>{a}</small><b class="{tone}">{v}</b><small>{esc(sub)}</small></div>'
                    for a, v, tone, sub in kpis)
    rows = "".join(f'<tr class="{kind or ""}"><th>{name}</th><td class="{"bad" if v is not None and v < 0 else ""}">{_krw(v)}</td></tr>'
                   for name, v, kind in f["rows"])
    i = c["id"]
    modes = "".join(f'<button data-fcf-for="{i}" data-m="{m}" onclick="fcfMode(\'{i}\', \'{m}\')">{label}</button>'
                    for m, label in [("annual", "연간"), ("ttm", "분기별 TTM")])
    return (f'<div class="card">{head}{banner}<div class="fkpis">{tiles}</div>'
            f'<div class="fbody"><div class="fleft"><div class="igroup fmode">{modes}</div><div id="fcf-{i}" class="fchart"></div></div>'
            f'<div class="fright"><div class="fcap">구성 · {esc(f["label"])}</div><table class="fbreak"><tbody>{rows}</tbody></table></div></div>'
            f'<div class="legend">잉여현금흐름 = 영업활동현금흐름 − 자본적지출(유형자산 + 무형자산 취득). 현금 전환율 = 잉여현금흐름 ÷ 순이익 (순이익이 0 이하면 —). '
            f'분기 값은 누적 공시에서 직전 분기 누적을 빼서 구하고, TTM은 최근 4개 분기 합계입니다. 출처: 금융감독원 DART 정기보고서(연결)</div></div>')


def render_reports(c):
    """증권사 리포트 카드: 최근 90일 목표가 컨센서스 + 증권사별 최신 리포트 표."""
    head = (f'<div class="fhead"><span class="sec-title">{esc(c["name"])} <small class="muted">{c["ticker"]}</small></span>'
            f'<span>한경 컨센서스 · 최근 1년 기업 리포트</span></div>')
    r = c.get("reports")
    if not r:
        message = c.get("reports_note") or c.get("error") or "리포트가 없습니다."
        return f'<div class="card">{head}<p class="note">{esc(message)}</p></div>'
    note = '<p class="note">리포트 목록을 받지 못해 마지막 저장본을 보여 줍니다.</p>' if r["cached"] else ""

    def up(v):
        if v is None:
            return "—"
        return f'<span class="{"good" if v >= 0 else "bad"}">{v:+.1f}%</span>'

    if r["avg"]:
        avg_up = (r["avg"] / r["price"] - 1) * 100 if r["price"] else None
        v = r["views"]
        kpis = [
            ("평균 목표가", f'{r["avg"]:,.0f}원', f'현재가 대비 {up(avg_up)} · 중간값 {r["median"]:,.0f}원'),
            ("최고 목표가", f'{r["high"]["target"]:,.0f}원', f'{esc(r["high"]["broker"])} · {up(r["high"]["upside"])}'),
            ("최저 목표가", f'{r["low"]["target"]:,.0f}원', f'{esc(r["low"]["broker"])} · {up(r["low"]["upside"])}'),
            ("투자의견", f'매수 {v["매수"]}', f'중립 {v["중립"]} · 매도 {v["매도"]} · 증권사 {r["n_brokers"]}곳'),
        ]
        tiles = "".join(f'<div class="fkpi"><small>{a}</small><b>{b}</b><small>{s}</small></div>' for a, b, s in kpis)
        closing = f'현재가 {r["price"]:,.0f}원' if r["price"] else "현재가 자료 없음"
        summary = (f'<div class="fkpis">{tiles}</div><div class="legend" style="margin:6px 0 14px">'
                   f'최근 {REPORT_DAYS}일 안에 목표가를 낸 증권사 {r["n_brokers"]}곳의 최신 목표가 기준 · 이 기간 리포트 {r["n_reports"]}건 · '
                   f'{closing}</div>')
    else:
        summary = f'<p class="note">최근 {REPORT_DAYS}일 안에 목표가를 낸 리포트가 없습니다.</p>'

    def change(x):
        if not x["repeat"]:
            return '<span class="muted">—</span>'
        if x["prev"] is None:
            return '<span class="muted">유지</span>'
        pct = (x["target"] / x["prev"] - 1) * 100
        arrow, cls = ("▲ 상향", "good") if pct > 0 else ("▼ 하향", "bad")
        return f'<span class="{cls}">{arrow} {pct:+.0f}%</span><small>직전 {x["prev"]:,}원</small>'

    tone = {"매수": "good", "중립": "warn", "매도": "bad"}
    rows = "".join(
        f'<tr class="{"" if x["fresh"] else "old"}"><td class="rdate">{x["date"]}</td>'
        f'<th>{esc(x["broker"])}<small>{esc(x["author"])}</small></th>'
        f'<td><span class="rop {tone.get(x["view"], "")}">{esc(x["opinion"])}</span></td>'
        f'<td class="rnum">{x["target"]:,}원</td><td class="rnum">{up(x["upside"])}</td><td class="rchg">{change(x)}</td>'
        f'<td class="rtitle"><a href="{esc(x["url"])}" target="_blank" rel="noopener noreferrer">{esc(x["title"])}</a></td></tr>'
        for x in r["latest"])
    return (f'<div class="card">{head}{note}{summary}<div class="scroll"><table class="rtab"><thead><tr>'
            f'<th>날짜</th><th>증권사</th><th>투자의견</th><th>목표가</th><th>현재가 대비</th><th>직전 대비</th><th>제목 (PDF)</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
            f'<div class="legend">증권사마다 가장 최근 리포트 한 건 · 흐린 줄은 {REPORT_DAYS}일보다 오래되어 컨센서스에서 뺀 것 · '
            f'직전 대비는 같은 증권사가 이전에 낸 다른 목표가와 비교 · 투자의견이 없거나 목표가가 없는 리포트(ESG·산업 코멘트 등)는 제외 · '
            f'한경 컨센서스에 올라온 리포트만 포함되어 일부 증권사가 빠질 수 있습니다 · 최신 리포트 {r["last_date"]}</div></div>')


def build_report_assets(directory):
    """Save one small report card per company, with prior files available on source failures."""
    rows = [entry for entry in directory["rows"] if not entry.get("builtin")]
    directory_path = OUT / "report-data"
    refresh = directory_path / ".refresh.json"
    today = TODAY.strftime("%Y-%m-%d")
    state = company_catalog.read_json(refresh)
    results = state.get("status", {}) if state.get("date") == today else {}
    todo = [row for row in rows if row["ticker"] not in results or not (directory_path / (row["ticker"] + ".js")).is_file()]

    def fetch_report(row):
        ticker = row["ticker"]
        path = directory_path / (ticker + ".js")
        stock = {"ticker": ticker, "name": row["name"]}
        try:
            stock["reports"] = report_summary(ticker, (row.get("quote") or {}).get("price"))
        except (ValueError, OSError, KeyError, IndexError, TypeError) as error:
            stock["reports_note"] = "리포트 자료를 확인하지 못했습니다."
            print(f"  ! 리포트 {ticker}: {type(error).__name__}", flush=True)
            if path.exists():
                return ticker, "cached"
        else:
            reports = stock["reports"]
            stock["reports_note"] = "최근 1년간 한경 컨센서스에 등록된 리포트가 없습니다." if not reports else None
        content = json.dumps({"html": render_reports(stock)}, ensure_ascii=False, separators=(",", ":"))
        company_catalog.write_atomic(path, f'window.dashboardReportPayload("{ticker}",{content});\n')
        if stock.get("reports"):
            return ticker, "available"
        return ticker, "failed" if "reports" not in stock else "empty"

    if todo:
        print(f"  증권사 리포트: {len(todo)}개 종목 조회", flush=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results.update(executor.map(fetch_report, todo))
        company_catalog.write_atomic(refresh, json.dumps({"date": today, "status": results}))
    return results


def render_rsi_badge(c):
    if c["rsi"] is None:
        return ""
    label, cls = rsi_zone(c["rsi"])
    previous = num(c["rsi_prev"], 1)
    return (f'<div class="srsi {cls}"><small>RSI({RSI_N}) · 일봉</small><b>{c["rsi"]:.1f}</b><span>{label}</span>'
            f'<small>1주 전 {previous} · {RSI_HI} 이상 과매수 · {RSI_LO} 이하 과매도</small></div>')


def render_company(c):
    """삼성전자·하이닉스 탭: 기업 개요 카드 + 일봉 차트."""
    if c.get("error"):
        return f'<p class="note">{esc(c["name"])}: {esc(c["error"])}</p>'
    chg = (c["price"] / c["prev"] - 1) * 100
    tone = "var(--bad)" if chg > 0 else ("var(--series-1)" if chg < 0 else "var(--mu)")  # 한국식 색 (상승 빨강)
    flow_card = render_flow(c["flow"], c["id"]) if c["flow"] else ""
    fcf_card = "" if c.get("price_only") else render_fcf(c)
    note = f'<p class="note">{esc(c["note"])}</p>' if c["note"] else ""
    i = c["id"]
    classification = f' · KRX 업종 {esc(c["sector"])}' if c.get("sector") else ""
    ranges = "".join(f'<button data-range-for="{i}" data-r="{r}" onclick="tvRange(\'{i}\', \'{r}\')">{r}</button>'
                     for r in ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"])
    intervals = "".join(f'<button data-iv-for="{i}" data-iv="{v}" onclick="tvInterval(\'{i}\', \'{v}\')">{label}</button>'
                        for v, label in [("D", "일"), ("W", "주"), ("M", "월")])
    mas = "".join(f'<button class="mabtn on" data-ma-for="{i}" data-n="{n}" onclick="tvMA(\'{i}\', {n})">'
                  f'<i style="background:{color}"></i>{n}</button>'
                  for n, color in [(5, "var(--text)"), (20, "var(--series-2)"), (60, "var(--series-1)"), (120, "var(--good)"), (240, "var(--series-3)")])
    chart_card = (
        f'<div class="card tvcard"><div class="tvtitle"><b>시장 주가 추이</b><span>{c["ticker"]} · '
        f'<a href="https://www.tradingview.com/chart/?symbol=KRX:{c["ticker"]}" target="_blank" rel="noopener noreferrer">TradingView</a></span></div>'
        f'<div class="tvframe"><div class="tvbar">{intervals}<i class="sep"></i>'
        f'<span class="tvlbl">ƒ 이동평균</span>{mas}</div>'
        f'<div class="schartwrap"><div class="slegend" id="legend-{i}"></div><div class="smabox" id="mabox-{i}"></div><div id="stock-{i}" class="schart"></div></div>'
        f'<div class="rsiwrap"><div class="slegend" id="rsilegend-{i}"></div><div id="rsi-{i}" class="rsichart"></div></div>'
        f'<div class="tvfoot">{ranges}<span class="tvclock" id="clock-{i}"></span></div></div>'
        f'<div class="tvcredit">Chart by TradingView Lightweight Charts · 시세 Yahoo Finance (KRX 종목은 TradingView 위젯이 막혀 있어 같은 엔진으로 직접 그림)</div></div>')
    return (
        f'<div class="card shead"><div class="sname"><div><b>{esc(c["name"])}</b>'
        f'<span class="sbadge">{c["ticker"]}</span><span class="smkt">{c["market"]}</span></div>'
        f'<small>{esc(c["eng"])}{classification}</small></div>'
        f'<div class="sright"><div class="sprice"><small>현재가</small><b>{c["price"]:,.0f}원</b>'
        f'<span style="color:{tone}">{c["price"] - c["prev"]:+,.0f} ({chg:+.2f}%)</span><small>종가 기준일 {c["date"]}</small></div>'
        f'{render_rsi_badge(c)}</div></div>'
        f'{note}{chart_card}{flow_card}{fcf_card}')


def prepare_company_catalog(companies, asof):
    """Prepare a single searchable list and individual local stock assets."""
    date = asof.strftime("%Y-%m-%d")
    ranked_directory = company_catalog.load_directory(OUT, date, ENV.get("KRX_API_KEY"))
    index_directory = company_catalog.read_json(OUT / company_catalog.INDEX_DIRECTORY)
    directory = index_directory or ranked_directory
    price_asof = date if index_directory else directory.get("asof", date)
    pinned = {entry["ticker"] for entry in companies}
    assets = OUT / "company-data"
    quotes = {}
    try:
        company_catalog.update_prices(OUT, {**directory, "asof": price_asof}, pinned)
        for entry in directory.get("members", []):
            ticker = entry["ticker"]
            if ticker in pinned:
                continue
            cache = OUT / ".company-prices" / (ticker + ".csv")
            try:
                px = validate_stock_prices(pd.read_csv(cache, index_col=0, parse_dates=True))
                stats = company_price_fields(px)
                note = "주가·거래량 제공"
                if stats["date"] < price_asof:
                    note += " · 마지막 저장 시세의 기준일을 확인하세요."
                stock = {**entry, **stats, "price_only": True, "note": note, "flow": None, "fcf": None}
                payload = json.dumps({"company": stock, "html": render_company(stock)}, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).replace("</", r"<\/")
                company_catalog.write_atomic(assets / (ticker + ".js"),
                                             f'window.dashboardCompanyPayload("{ticker}",{payload});\n')
                quotes[ticker] = company_catalog.stock_quote(stock)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"  ! 기업 분석 {ticker}: {type(error).__name__} · 저장 시세 확인")
    except OSError as error:
        print(f"  ! 기업 분석 파일 생성: {type(error).__name__}")
    return {
        "asof": directory.get("asof", ""),
        "count": len(directory.get("members", [])),
        "flow_directory": ranked_directory,
        "index_based": bool(index_directory),
        "rows": company_catalog.directory_rows(directory, companies, assets, quotes),
    }


EXPORT_MONTHS = 24

EXPORT_JS = r"""
const EXPORT_OVERVIEWS = __DATA__;
function drawExportOverview(tab) {
  const d = EXPORT_OVERVIEWS[tab];
  if (!d) return;
  if (typeof echarts === 'undefined') { chartUnavailable(tab); return; }
  const el = document.getElementById(tab === 't5' ? 'export-chart' : tab + '-overview-chart');
  if (!el) return;
  const existing = echarts.getInstanceByDom(el);
  if (existing) { existing.resize(); return; }
  const last = d.months.length - 1;
  const shareName = d.category + ' 비중';
  const amount = value => value.toLocaleString('ko-KR', {maximumFractionDigits: d.digits}) + '억$';
  const exportChart = echarts.init(el, null, {renderer: 'canvas'});
  exportChart.setOption({
    animation: false, backgroundColor: 'transparent',
    grid: {left: 52, right: 56, top: 30, bottom: 28},
    legend: {top: 0, right: 0, textStyle: {color: plotColor('text-2'), fontSize: 12}, itemWidth: 12, itemHeight: 10,
             data: [d.label, '그 밖', shareName]},
    tooltip: {trigger: 'axis', backgroundColor: plotColor('surface'), borderColor: plotColor('border'), textStyle: {color: plotColor('text'), fontSize: 12},
      formatter: ps => {
        const total = d.total[ps[0].dataIndex];
        return ps[0].axisValue + '<br>' + ps.map(p => p.marker + p.seriesName + ' <b>' +
          (typeof p.value !== 'number' ? '–' : p.seriesName === shareName ? p.value.toFixed(d.digits || 1) + '%' : amount(p.value)) +
          '</b>').join('<br>') + '<br>전체 수출 <b>' + (total == null ? '–' : amount(total)) + '</b>';
      }},
    xAxis: {type: 'category', data: d.months, axisLine: {lineStyle: {color: plotColor('border')}}, axisTick: {show: false},
      axisLabel: {color: plotColor('text-3'), fontSize: 11, interval: 0, hideOverlap: true,
        formatter: m => m.endsWith('-01') ? m.slice(0, 4) + '년' : (m.endsWith('-07') ? '7월' : '')}},
    yAxis: [
      {type: 'value', splitLine: {lineStyle: {color: plotColor('grid')}}, axisLabel: {color: plotColor('text-3'), fontSize: 10, formatter: v => v.toLocaleString()}},
      {type: 'value', position: 'right', min: 0, max: tab === 't5' ? 60 : undefined, splitLine: {show: false},
       axisLabel: {color: plotColor('series-2'), fontSize: 10, formatter: v => v + '%'}},
    ],
    series: [
      {name: d.label, type: 'bar', stack: 'x', data: d.amount, barWidth: '62%', itemStyle: {color: plotColor('series-1')}},
      {name: '그 밖', type: 'bar', stack: 'x', data: d.others, itemStyle: {color: plotColor('surface-2'), borderRadius: [3, 3, 0, 0]},
       markPoint: {symbol: 'roundRect', symbolSize: [64, 20], symbolOffset: [0, -16],
         data: [{coord: [last, d.total[last]], value: Math.round(d.total[last]).toLocaleString() + '억$'}],
         itemStyle: {color: plotColor('series-2')}, label: {color: plotColor('value-label'), fontWeight: 'bold', fontSize: 11}}},
      {name: shareName, type: 'line', yAxisIndex: 1, data: d.share, showSymbol: false, smooth: true, connectNulls: false,
       lineStyle: {color: plotColor('series-2'), width: 2}, itemStyle: {color: plotColor('series-2')}},
    ],
  });
}
window.addEventListener('resize', () => {
  const visible = document.querySelector('.tab.on .xchart[data-export-overview]');
  if (visible && typeof echarts !== 'undefined') echarts.getInstanceByDom(visible)?.resize();
});
"""


def export_data(raw):
    """수출 탭 데이터 (억 달러)."""
    df = raw / 1e8
    df = df.tail(EXPORT_MONTHS + 12)  # 전년 대비 계산용 12개월 여유
    last = df.index[-1]
    year_ago = str(pd.Period(last, "M") - 12)
    three_ago = str(pd.Period(last, "M") - 3)
    month_ago = str(pd.Period(last, "M") - 1)
    change = lambda col, ref: (df.at[last, col] / df.at[ref, col] - 1) * 100 if ref in df.index and df.at[ref, col] else None

    view = df.tail(EXPORT_MONTHS)
    items = []
    for item in CFG["exports"]["items"]:
        name = item["name"]
        items.append({"name": name, "hs": item["hs"], "value": df.at[last, name],
                      "m1": change(name, month_ago), "m3": change(name, three_ago), "y1": change(name, year_ago),
                      "bars": [round(v, 2) for v in view[name]]})
    items.sort(key=lambda i: -(i["y1"] if i["y1"] is not None else -1e9))
    first = (pd.Period(last, "M") - 47).asfreq("Q").asfreq("M", "start")
    product_months = pd.period_range(first, last, freq="M").astype(str)
    product_values = raw.reindex(product_months)
    products = {
        "months": list(product_months), "asof": last, "updated": TODAY.strftime("%Y-%m-%d"),
        "notes": ["일부 월 자료가 없어 해당 구간과 전년 비교를 빈 값으로 표시합니다."]
                 if product_values[[item["name"] for item in items]].isna().any(axis=None) else [],
        "items": [
            {"id": "hs-" + "-".join(item["hs"]), "name": item["name"], "hs": item["hs"],
             "values": [None if pd.isna(v) else float(v) for v in product_values[item["name"]]]}
            for item in items
        ],
    }
    return {
        "month": last, "months": list(view.index),
        "total": [round(v, 1) for v in view["total"]],
        "semis": [round(v, 1) for v in view["semis"]],
        "others": [round(v, 1) for v in view["total"] - view["semis"]],
        "share": [round(v, 1) for v in view["semis"] / view["total"] * 100],
        "total_last": df.at[last, "total"], "total_y1": change("total", year_ago), "total_m1": change("total", month_ago),
        "semis_last": df.at[last, "semis"], "semis_y1": change("semis", year_ago), "semis_m1": change("semis", month_ago),
        "share_last": df.at[last, "semis"] / df.at[last, "total"] * 100,
        "items": items, "products": products, "flash": motir_flash(),
    }


def export_share_data(totals, product_data, item, category, scope):
    amounts = dict(zip(product_data["months"], item["values"]))
    comparable = {
        month: (total, amounts[month])
        for month, total in totals.items()
        if pd.notna(total) and total > 0 and amounts.get(month) is not None
        and 0 <= amounts[month] <= total
    }
    if not comparable:
        return None
    month = max(comparable)
    period = pd.Period(month, "M")
    months = pd.period_range(period - (EXPORT_MONTHS - 1), period, freq="M").astype(str).tolist()
    latest_total, latest_amount = comparable[month]

    def change(latest, previous):
        return (latest / previous - 1) * 100 if pd.notna(previous) and previous > 0 else None

    return {
        "category": category, "label": scope, "digits": 2, "month": month, "months": months,
        "total": [float(totals[m] / 1e8) if m in totals and pd.notna(totals[m]) and totals[m] > 0
                  else None for m in months],
        "amount": [float(comparable[m][1] / 1e8) if m in comparable else None for m in months],
        "others": [float((comparable[m][0] - comparable[m][1]) / 1e8) if m in comparable
                   else None for m in months],
        "share": [float(comparable[m][1] / comparable[m][0] * 100) if m in comparable
                  else None for m in months],
        "total_last": latest_total / 1e8,
        "amount_last": latest_amount / 1e8,
        "total_y1": change(latest_total, totals.get(str(period - 12))),
        "total_m1": change(latest_total, totals.get(str(period - 1))),
        "amount_y1": change(latest_amount, amounts.get(str(period - 12))),
        "amount_m1": change(latest_amount, amounts.get(str(period - 1))),
    }


def render_export_share(tab, data):
    def comparison(value):
        if value is None:
            return "–"
        if round(value, 1) == 0:
            return "0.0%"
        return f'<b style="color:{market_verdict_tone(value)}">{value:+.1f}%</b>'

    month = data["month"]
    return (
        f'<div class="card"><div class="chead"><time class="cdate" datetime="{month}">'
        f'{month.replace("-", ".")}</time>{int(month[5:])}월 수출 {data["total_last"]:,.0f}억 달러 · 1년 전 대비 '
        f'{comparison(data["total_y1"])}<small class="hm1"> · 전월 대비 {comparison(data["total_m1"])}</small></div>'
        f'<div class="csub">{esc(data["label"])} {data["amount_last"]:,.2f}억 달러 · 1년 전 대비 '
        f'{comparison(data["amount_y1"])} · 전월 대비 {comparison(data["amount_m1"])} · '
        f'전체 수출의 <b>{data["share"][-1]:.2f}%</b></div>'
        f'<div id="{esc(tab)}-overview-chart" class="xchart" data-export-overview role="img" '
        f'aria-label="{esc(data["category"])} 수출액과 전체 수출 대비 비중 추이"></div>'
        '<div class="legend">최근 2년 월별 전국 수출 · 관세청 품목별 수출입실적 · FOB 달러 · '
        '비중은 해당 품목 수출액 ÷ 같은 달 전체 수출액 × 100</div></div>'
    )


def export_overviews(raw, semiconductors, industries, output, today, fetch_month):
    charts, cards = {}, {}
    if semiconductors:
        charts["t5"] = {
            **{key: semiconductors[key] for key in ("months", "total", "others", "share")},
            "amount": semiconductors["semis"], "label": "반도체 (집적회로)", "category": "반도체", "digits": 0,
        }
    if raw is None:
        return charts, cards

    def add(tab, dataset, item, category, scope):
        data = export_share_data(raw["total"], dataset, item, category, scope)
        if data:
            charts[tab] = data
            cards[tab] = render_export_share(tab, data)

    if industries:
        for item in industries["items"]:
            group_id = item["group"]
            if item["id"] == group_id + "-total":
                add(industry_exports.TAB_PREFIX + group_id, industries, item, item["name"], item["name"])

    try:
        cosmetics_totals = cosmetics.collect(
            output, today, {"title": "화장품 수출 비중", "items": [
                {"id": "cosmetics", "name": "화장품(HS 3304)", "hs": ["3304"]},
            ]}, fetch_month, cache_prefix="cosmetics_overview",
        )
    except Exception as error:
        print(f"  ! 화장품 수출 비중: {error.__class__.__name__}")
    else:
        item = cosmetics_totals["items"][0]
        add("cosmetics", cosmetics_totals, item, "화장품", item["name"])
    return charts, cards


def export_overview_script(charts):
    if not charts:
        return ""
    serialized = json.dumps(charts, ensure_ascii=False, default=float, allow_nan=False).replace("</", r"<\/")
    return EXPORT_JS.replace("__DATA__", serialized)


def render_exports(data, note=None):
    if data is None:
        return f'<p class="note">{esc(note or "수출 데이터를 받지 못했습니다.")}</p>'
    month = int(data["month"][5:])
    pct = lambda v: "–" if v is None else ("0%" if round(v) == 0 else f"{v:+.0f}%")
    tone = lambda v: "var(--mu)" if v is None or round(v) == 0 else market_verdict_tone(v)
    head = (f'<div class="chead"><span class="cdate">{data["month"].replace("-", ".")}</span>'
            f'{month}월 수출 {data["total_last"]:,.0f}억 달러 · 1년 전 대비 '
            f'<span style="color:{tone(data["total_y1"])}">{pct(data["total_y1"])}</span>'
            f'<small class="hm1"> · 전월 대비 <span style="color:{tone(data["total_m1"])}">{pct(data["total_m1"])}</span></small></div>'
            f'<div class="csub">반도체(집적회로) {data["semis_last"]:,.0f}억 달러 · 1년 전 대비 '
            f'<b style="color:{tone(data["semis_y1"])}">{pct(data["semis_y1"])}</b> · 전월 대비 '
            f'<b style="color:{tone(data["semis_m1"])}">{pct(data["semis_m1"])}</b> · 전체 수출의 {data["share_last"]:.0f}%</div>')

    flash, flash_card = data["flash"], ""
    if flash and flash["month"] > data["month"]:
        fm = int(flash["month"][5:])
        flash_card = (f'<div class="card"><div class="chead"><span class="cdate">{flash["month"].replace("-", ".")}</span>'
                      f'{fm}월 수출 {flash["total"]:,.0f}억 달러 · 1년 전 대비 '
                      f'<span style="color:{tone(flash["total_y1"])}">{pct(flash["total_y1"])}</span></div>'
                      f'<div class="csub">반도체 {flash["semis"]:,.0f}억 달러 · 1년 전 대비 '
                      f'<b style="color:{tone(flash["semis_y1"])}">{pct(flash["semis_y1"])}</b> · '
                      f'전체 수출의 {flash["semis"] / flash["total"] * 100:.0f}%</div>'
                      f'<div class="legend">※ 산업통상부 {flash["posted"][5:].replace("-", ".")} 발표 잠정치 · '
                      f'반도체 범위가 넓어(산업부 분류) 아래 집적회로(HS 8542) 수치와 직접 비교할 수 없습니다 · '
                      f'품목별 자료는 매월 15일경 나오면 아래에 반영됩니다</div></div>')
    return (f'{flash_card}<div class="card">{head}<div id="export-chart" class="xchart" data-export-overview></div>'
            f'<div class="legend">※ 관세청 품목별 수출입실적 (공공데이터포털) · FOB 달러 · 매월 15일경 전월분 발표 · '
            f'반도체 = 집적회로(HS 8542) 전체 · SSD는 관세청 분류상 저장매체(HS 8523)라 아래 품목에서 따로 봅니다.<br>DRAM 수출액은 가격과 수량이 함께 반영된 전국 통관 금액입니다. 칩 규격별 시장 현물가는 <a href="https://www.dramexchange.com/" target="_blank" rel="noopener noreferrer">TrendForce DRAMeXchange</a>에서 확인할 수 있습니다.</div></div>'
            + cosmetics.render(data["products"], namespace="semiconductors", title="반도체 품목별 수출"))


WEEKDAYS = "월화수목금토일"


def _ev_when(e):
    at = datetime.fromisoformat(e["at"])
    day = f"{at:%m-%d} ({WEEKDAYS[at.weekday()]})"
    return day, (f"{at:%H:%M}" if e["time_known"] else "시간 미정")


def render_events(ev):
    """일정 탭: 구분 · 중요도 필터 + 한국 시각 기준 다가오는 일정 목록 (지난 일정과 D-day는 페이지에서 다시 계산)."""
    if not ev:
        return '<p class="note">일정을 받지 못했습니다.</p>'
    notes = "".join(f'<p class="note">{esc(x)}</p>' for x in ev["errors"])
    buttons = '<button class="on" data-r="all">전체</button>' + "".join(
        f'<button data-r="{k}">{v}</button>' for k, v in events.REGIONS.items())
    rows, month = "", None
    for e in ev["events"]:
        at = datetime.fromisoformat(e["at"])
        if at.strftime("%Y-%m") != month:
            month = at.strftime("%Y-%m")
            rows += f'<h3 class="evmonth">{at.year}년 {at.month}월</h3>'
        day, clock = _ev_when(e)
        stars = "★" * e["stars"] + f'<i>{"☆" * (3 - e["stars"])}</i>'
        unit = e.get("unit") or ""
        result = f'<b class="evact">결과 {esc(e["actual"])}{esc(unit)}</b>' if e.get("actual") else ""
        extra = [x for x in [f'직전 {e["prev"]}{unit}' if e["prev"] else None, f'예상 {e["expected"]}{unit}' if e["expected"] else None,
                             e["note"]] if x]
        parts = ""
        for x in e.get("items", []):
            shown = f'<b>{esc(x["actual"])}{esc(x["unit"])}</b>' if x["actual"] else ""
            before = f'직전 {esc(x["prev"])}{esc(x["unit"])}' if x["prev"] else ""
            if shown or before:
                parts += f'<span>{esc(x["label"])} {shown}{f" ({before})" if shown and before else before}</span>'
        why = f'<div class="evwhy">{esc(e["why"])}</div>' if e.get("why") else ""
        rows += (f'<a class="evrow" href="{esc(e["url"])}" target="_blank" rel="noopener noreferrer" data-r="{e["region"]}" '
                 f'data-s="{e["stars"]}" data-at="{e["at"]}" data-known="{int(e["time_known"])}">'
                 f'<div class="evtop"><span class="evtime">{day} <b>{clock}</b></span><span class="evd"></span>'
                 f'<span class="evstars" aria-label="중요도 {e["stars"]}/3">{stars}</span></div>'
                 f'<div class="evtitle">{esc(e["title"])}</div>'
                 f'<div class="evsub"><span class="evreg {e["region"]}">{events.REGIONS[e["region"]]}</span>'
                 f'{result}{"".join(f"<span>{esc(x)}</span>" for x in extra)}</div>'
                 f'{f"<div class=evitems>{parts}</div>" if parts else ""}{why}</a>')
    return (f'{notes}<div class="card"><div class="fhead"><span class="sec-title">일정</span>'
            f'<span>한국 시각 (UTC+9) · 이달 1일부터 앞으로 {ev["horizon"]}일까지 · 받은 시각 {ev["today"]}</span></div>'
            f'<div class="evtools"><div class="evfilter">{buttons}</div>'
            f'<label class="evonly"><input type="checkbox" id="ev3"> ★★★만</label></div>'
            f'<div class="cal"><div class="calnav"><button id="calprev" aria-label="이전 달">‹</button><b id="calttl"></b>'
            f'<button id="calnext" aria-label="다음 달">›</button></div>'
            f'<div class="calhead">{"".join(f"<span>{d}</span>" for d in "일월화수목금토")}</div><div class="calgrid" id="calgrid"></div></div>'
            f'<div class="evday" id="evday" hidden></div><div class="evlist" hidden>{rows}</div>'
            f'<div class="legend">★★★ 시장을 크게 움직이는 일정 · ★★ 중요 · ★ 참고 · 날짜를 누르면 그날 일정이 달력 아래에 나오고, 일정을 누르면 출처가 열립니다 · '
            f'출처: Yahoo 경제 일정표(지표 시각·결과·직전 값, 약 4주 앞까지) · 연준·한국은행·일본은행 공식 일정 · Yahoo 실적 예정일·추정치 · '
            f'옵션 만기·지수 변경·관세청 수출·보고서 기한은 규칙으로 계산 · 삼성전자 잠정실적과 TSMC 월매출은 날짜 추정 · 휴장일 표는 해마다 갱신 필요</div></div>')


def render_html(ctx):
    directory = ctx["company_catalog"]
    search = company_catalog.search_markup(directory["asof"], directory["count"], index_based=directory["index_based"])
    sidebar, datasets = dashboard_layout.navigation(
        ctx["companies"], search, flow_etfs=ctx["sector_map"].get("rows", []),
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>국장 매크로 대시보드</title><style>{dashboard_theme.CSS}{CSS}{cosmetics.CSS}{local_views.CSS}{dashboard_layout.CSS}{trend_charts.CSS}{local_views.MARKET_CSS}{company_catalog.CSS}{sector_map.CSS}{inflation.CSS}{stock_flow_map.CSS}</style>{dashboard_theme.BOOT}</head>
<body><div class="dashboard-shell">
{sidebar}
<main class="dashboard-main">
{dashboard_layout.HEADER}
<div class="meta">시장 데이터 기준 {ctx["asof"]:%Y-%m-%d} · 마지막 화면 갱신 성공 {ctx["generated"]:%Y-%m-%d %H:%M} KST · <span id="refresh-state" data-at="{ctx["generated"].isoformat()}">로컬 생성본</span></div>
{local_views.STATUS_SCRIPT}
<div class="workspace-grid">
{datasets}
<div class="workspace-detail">

{render_market_assessment(ctx)}

{sector_map.markup(ctx["sector_map"])}

{"".join(f'<div class="tab" id="s-{c["id"]}">{render_company(c)}{ctx["apr_comparison"] if c["id"] == "apr" else ""}</div>' for c in ctx["companies"])}

<div class="tab" id="s-catalog"><p class="card company-load-message">종목을 검색해 선택하세요.</p></div>

<div class="tab" id="t7">
{ctx["events_html"]}
</div>
{"".join(f'<div class="tab" id="r-{esc(c["id"])}">{render_reports(c)}</div>' for c in ctx["companies"])}
<div class="tab" id="r-catalog"><p class="card company-load-message">종목을 검색해 리포트를 열어 보세요.</p></div>
{stock_flow_map.markup(ctx["stock_flow_map"])}
{sector_map.flow_etf_panels(ctx["sector_map"])}

<div class="tab" id="t4">
{ctx["credit_html"]}
</div>

<div class="tab" id="memory">
{(ROOT / "dram_spot_snapshot.html").read_text(encoding="utf-8").replace("<!-- memory-chart -->", trend_charts.memory_chart())}
</div>

<div class="tab" id="t5">
{ctx["export_html"]}
</div>

<div class="tab" id="cosmetics">
{ctx["cosmetics_html"]}
</div>

{ctx["industry_html"]}

</div>
</div>
</main>
</div>
<script src="{ECHARTS_URL}"></script>
<script src="{LWC_URL}"></script>
<script>{ctx["credit_js"]}</script>
<script>{ctx["export_js"]}</script>
<script>{ctx["semiconductor_js"]}</script>
<script>{ctx["cosmetics_js"]}</script>
<script>{ctx["industry_js"]}</script>
<script>{ctx["stock_js"]}</script>
{dashboard_layout.SCRIPT}<script>
function chartUnavailable(id) {{
  const parent = document.getElementById(id);
  if (!parent || parent.querySelector('.chart-error')) return;
  const msg = document.createElement('p'); msg.className = 'note chart-error'; msg.setAttribute('role', 'status');
  msg.textContent = '차트 라이브러리를 불러오지 못했습니다. 인터넷 연결을 확인한 뒤 새로고침하세요. 저장된 수치는 계속 확인할 수 있습니다.';
  parent.prepend(msg);
}}
document.querySelectorAll('.dataset-list button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.dataset-list button,.tab').forEach(e=>e.classList.remove('on'));
  b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');
  selectWorkspaceView(b);
  if (b.dataset.t === 't4' && typeof drawCredit === 'function') {{ drawCredit(); creditCharts.forEach(c => c.resize()); }}
  if (b.dataset.t === 'memory' && typeof drawMemory === 'function') drawMemory();
  if (b.dataset.group === 'flow') drawStockFlowMap(b.dataset.t);
  if (b.dataset.t === 'sector-heatmap') drawSectorHeatmap();
  if (typeof drawExportOverview === 'function') drawExportOverview(b.dataset.t);
  if (b.dataset.t === 't5' && typeof drawSemiconductors === 'function') {{ drawSemiconductors(); }}
  if (b.dataset.t === 'cosmetics' && typeof drawCosmetics === 'function') {{ drawCosmetics(); }}
  if (b.dataset.t.startsWith('{industry_exports.TAB_PREFIX}')) {{
    window['draw' + b.dataset.t[0].toUpperCase() + b.dataset.t.slice(1)]?.();
  }}
  if (b.dataset.t.startsWith('s-') && b.dataset.t !== 's-catalog' && typeof drawStock === 'function') {{
    const id = b.dataset.t.slice(2);
    drawStock(id);
    // 숨어 있는 동안 창 크기가 바뀌면 너비 0으로 줄어 있으므로 다시 맞춘다.
    [FLOW[id], FCF[id]].forEach(f => f && f.chart.resize());
  }}
}});
(function () {{
  // 일정: 구분 · ★★★만 필터. D-day와 지난 일정은 페이지를 연 시각 기준으로 다시 계산한다 (주소로 공유해 나중에 열어도 맞게).
  const rows = [...document.querySelectorAll('.evrow')];
  if (!rows.length) return;
  const kst = d => new Date(d.getTime() + 9 * 3600e3).toISOString().slice(0, 10);
  const now = new Date(), today = kst(now);
  let region = 'all';
  rows.forEach(r => {{
    const at = new Date(r.dataset.at), day = kst(at);
    const days = Math.round((new Date(day) - new Date(today)) / 864e5);
    const done = r.dataset.known === '1' ? at < now : days < 0;
    r.classList.toggle('past', done);
    const tag = r.querySelector('.evd');
    tag.textContent = done ? '지남' : days === 0 ? '오늘' : 'D-' + days;
    tag.classList.toggle('today', !done && days === 0);
  }});
  // 달력: 목록의 일정(같은 필터)을 월 격자에 찍는다. 첫 달은 오늘이 있는 달, 마지막 달은 마지막 일정이 있는 달.
  const items = rows.map(r => ({{day: kst(new Date(r.dataset.at)), r: r.dataset.r, s: +r.dataset.s, title: r.querySelector('.evtitle').textContent, row: r}}));
  const firstMonth = today.slice(0, 7), lastMonth = items.length ? items[items.length - 1].day.slice(0, 7) : firstMonth;
  let calMonth = firstMonth;
  const shiftMonth = (ym, n) => {{ const d = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'); }};
  const esc = s => s.replace(/[&<>"]/g, c => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}})[c]);
  function renderCal(match) {{
    const [y, m] = calMonth.split('-').map(Number), first = new Date(y, m - 1, 1), n = new Date(y, m, 0).getDate();
    document.getElementById('calttl').textContent = y + '년 ' + m + '월';
    document.getElementById('calprev').disabled = calMonth <= firstMonth;
    document.getElementById('calnext').disabled = calMonth >= lastMonth;
    let html = '<div class="calcell blank"></div>'.repeat(first.getDay());
    for (let d = 1; d <= n; d++) {{
      const day = calMonth + '-' + String(d).padStart(2, '0'), wd = (first.getDay() + d - 1) % 7;
      const evs = items.filter(e => e.day === day && match(e)).sort((a, b) => b.s - a.s);
      const list = evs.slice(0, 4).map(e => '<div class="ci ' + e.r + (e.s === 3 ? ' s3' : '') + '"><i></i><span>' + esc(e.title) + '</span></div>').join('');
      const more = evs.length > 4 ? '<div class="cmore">+' + (evs.length - 4) + '</div>' : '';
      html += '<div class="calcell' + (wd === 0 ? ' we sun' : wd === 6 ? ' we' : '') + (day === today ? ' today' : '') + '" data-day="' + day + '" title="'
        + esc(evs.map(e => e.title).join(' · ')) + '"><div class="cd">' + d + '</div>' + list + more + '</div>';
    }}
    const grid = document.getElementById('calgrid');
    grid.innerHTML = html;
    grid.querySelectorAll('.calcell[data-day]').forEach(c => c.onclick = () => showDay(c.dataset.day, match, true));
    if (picked) showDay(picked, match, false);  // 필터를 바꾸면 열린 날짜도 같은 필터로 다시 그린다
  }}
  // 날짜를 누르면 그날 일정만 달력 아래에 보여 준다 (같은 날을 다시 누르면 닫는다).
  let picked = null;
  function showDay(day, match, toggle) {{
    const box = document.getElementById('evday');
    if (toggle && picked === day) {{ picked = null; box.hidden = true; grid_mark(null); return; }}
    picked = day;
    const evs = items.filter(e => e.day === day && match(e));
    const d = new Date(day + 'T00:00:00');
    box.innerHTML = '<div class="evdayhead">' + (d.getMonth() + 1) + '월 ' + d.getDate() + '일 (' + '일월화수목금토'[d.getDay()] + ')</div>'
      + (evs.length ? '' : '<p class="muted">이날 일정이 없습니다.</p>');
    evs.forEach(e => {{ const n = e.row.cloneNode(true); n.hidden = false; box.appendChild(n); }});
    box.hidden = false;
    grid_mark(day);
  }}
  function grid_mark(day) {{
    document.querySelectorAll('.calcell[data-day]').forEach(c => c.classList.toggle('picked', c.dataset.day === day));
  }}
  document.getElementById('calprev').onclick = () => {{ calMonth = shiftMonth(calMonth, -1); apply(); }};
  document.getElementById('calnext').onclick = () => {{ calMonth = shiftMonth(calMonth, 1); apply(); }};
  function apply() {{
    const only3 = document.getElementById('ev3').checked;
    renderCal(e => (region === 'all' || e.r === region) && (!only3 || e.s === 3));
    rows.forEach(r => r.hidden = !((region === 'all' || r.dataset.r === region) && (!only3 || r.dataset.s === '3') && !(r.classList.contains('past') && kst(new Date(r.dataset.at)) < today)));
    document.querySelectorAll('.evmonth').forEach(h => {{
      let n = h.nextElementSibling, any = false;
      while (n && !n.classList.contains('evmonth')) {{ if (!n.hidden) any = true; n = n.nextElementSibling; }}
      h.hidden = !any;
    }});
  }}
  document.querySelectorAll('.evfilter button').forEach(b => b.onclick = () => {{
    region = b.dataset.r; document.querySelectorAll('.evfilter button').forEach(e => e.classList.toggle('on', e === b)); apply();
  }});
  document.getElementById('ev3').onchange = apply;
  picked = today;  // 처음 열면 오늘 일정을 달력 아래에 펼쳐 둔다
  apply();
}})();
</script>
{company_catalog.script(directory["rows"])}
{sector_map.script(ctx["sector_map"])}
{stock_flow_map.script(ctx["stock_flow_map"], ctx["sector_map"])}
{dashboard_theme.SWITCH}</body></html>"""


# ---------------------------------------------------------------- 실행

def main():
    OUT.mkdir(exist_ok=True)
    inds = CFG["indicators"]
    print("데이터 받는 중...")
    idx_tickers = {i["calc"]["ticker"] for i in inds if "ticker" in i.get("calc", {})}
    idx_tickers |= {a["ticker"] for a in CFG["period_assets"]} | {"^KS11", "^KQ11"}
    px = yahoo(idx_tickers, "2005-01-01")
    uni = yahoo(CFG["breadth_universe"], (TODAY - pd.Timedelta(days=800)).date())
    uni = uni.dropna(axis=1, how="all").ffill(limit=3)
    kospi = px["^KS11"].dropna()

    rows = []
    for ind in inds:
        row = {"ind": ind}
        if ind["calc"]["src"] != "pending":
            try:
                s = compute(ind, px, uni)
                row["res"] = analyze(ind, s, kospi)
            except MissingKey as e:
                row["note"] = str(e)
                print(f"  - {ind['name']}: 키 없음 (미연결로 표시)")
            except Exception as e:  # 한 지표가 실패해도 나머지는 그린다
                row["err"] = e.__class__.__name__
                print(f"  ! {ind['name']}: {e.__class__.__name__}")
        rows.append(row)

    verdicts = {g["id"]: group_verdict(g, [r for r in rows if r["ind"]["group"] == g["id"]]) for g in CFG["groups"]}
    try:
        ev = events.collect()
        print(f"  일정 {len(ev['events'])}건" + (f" (실패: {', '.join(ev['errors'])})" if ev["errors"] else ""))
    except Exception as e:  # 일정이 실패해도 나머지 대시보드는 만든다
        ev = None
        print(f"  ! 일정: {e.__class__.__name__}")
    try:
        companies = company_tabs()
    except Exception as e:  # 회사 탭이 실패해도 나머지 대시보드는 만든다
        companies = []
        print(f"  ! 회사 탭: {e.__class__.__name__}")
    catalog = prepare_company_catalog(companies, kospi.index[-1])
    try:
        build_report_assets(catalog)
    except (OSError, ValueError) as error:
        print(f"  ! 리포트 파일 갱신: {type(error).__name__}")
    raw_exports, exports, export_note = None, None, None
    try:
        raw_exports = customs_exports()
        exports = export_data(raw_exports)
    except MissingKey as e:
        export_note = str(e)
    except Exception as e:
        export_note = f"수출 데이터를 받지 못했습니다 ({e.__class__.__name__})"
        print(f"  ! 수출 탭: {e.__class__.__name__}")
    cosmetics_data, cosmetics_note = None, None
    try:
        if not ENV.get("DATA_GO_KR_KEY"):
            raise MissingKey("공공데이터포털 키가 .env에 없습니다 (DATA_GO_KR_KEY).")
        cosmetics_data = cosmetics.collect(OUT, TODAY, CFG["cosmetics_exports"], _customs_month)
    except Exception as e:
        cosmetics_note = f"화장품 수출 자료를 받지 못했습니다 ({e.__class__.__name__})."
        print(f"  ! 화장품 수출: {e.__class__.__name__}")
    industry_data, industry_note = None, None
    try:
        industry_data = industry_exports.collect(OUT, TODAY, _customs_rows)
    except Exception as e:
        industry_note = f"품목별 수출 자료를 받지 못했습니다 ({e.__class__.__name__})."
        print(f"  ! 품목별 수출: {e.__class__.__name__}")
    overviews, overview_cards = export_overviews(raw_exports, exports, industry_data, OUT, TODAY, _customs_month)
    industry_html, industry_js = industry_exports.views(industry_data, industry_note, overview_cards)
    try:
        credit = credit_charts(px)
    except Exception as e:  # 신용잔고 탭이 실패해도 나머지 대시보드는 만든다
        credit = {"panels": [], "notes": ["신용잔고 데이터를 받지 못했습니다."]}
        print(f"  ! 신용잔고 탭: {e.__class__.__name__}")
    try:
        credit["deposit"] = deposit_panel(next(i["calc"] for i in inds if i["id"] == "deposit"), kospi)
    except Exception as e:
        credit["deposit"] = None
        print(f"  ! 투자자예탁금: {e.__class__.__name__}")
    ctx = {
        "inflation": inflation.collect(OUT, datetime.now(ZoneInfo(KST))),
        "apr_comparison": local_views.apr_comparison(cosmetics_data),
        "rows": rows, "verdicts": verdicts, "sentence": summary_sentence(verdicts),
        "asof": kospi.index[-1], "generated": datetime.now(ZoneInfo(KST)),
        "uni_n": uni.shape[1],
        "events_html": render_events(ev),
        "credit_html": render_credit(credit),
        "export_html": render_exports(exports, export_note),
        "semiconductor_js": cosmetics.script(exports["products"], namespace="semiconductors") if exports else "",
        "cosmetics_html": overview_cards.get("cosmetics", "") + cosmetics.render(cosmetics_data, cosmetics_note),
        "cosmetics_js": cosmetics.script(cosmetics_data),
        "industry_html": industry_html, "industry_js": industry_js,
        "companies": companies, "company_catalog": catalog,
        "stock_flow_map": stock_flow_map.load_snapshot(OUT, catalog["flow_directory"], kospi.index, ENV),
        "sector_map": sector_map.load_snapshot(OUT, kospi.index[-1].strftime("%Y-%m-%d"), ENV),
        "stock_js": STOCK_JS.replace("__DATA__", json.dumps(companies, ensure_ascii=False, allow_nan=False).replace("</", r"<\/")),
        "export_js": export_overview_script(overviews),
        "credit_js": trend_charts.SCRIPT.replace("__DATA__", json.dumps(credit, ensure_ascii=False, allow_nan=False).replace("</", r"<\/")),
    }
    target = OUT / "dashboard.html"
    temporary = OUT / f".dashboard-{os.getpid()}.tmp"
    temporary.write_text(render_html(ctx), encoding="utf-8")
    temporary.replace(target)
    print(f"완료: {OUT / 'dashboard.html'}")
    print("요약:", ctx["sentence"])


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    local_views.write_state(OUT, "running", datetime.now(ZoneInfo(KST)))
    try:
        main()
    except BaseException:
        local_views.write_state(OUT, "failed", datetime.now(ZoneInfo(KST)))
        raise
    local_views.write_state(OUT, "success", datetime.now(ZoneInfo(KST)))
