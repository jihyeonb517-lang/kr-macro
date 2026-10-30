"""국장 매크로 대시보드.

indicators.json을 읽어 데이터를 받고, output/ 폴더에 HTML 대시보드를 만든다.
실행: python build.py
"""
import contextlib
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
import news

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

# 구간 색. IV Terminal 팔레트(--bad, --accent, --warn, --series-1, --good)에 맞춘다.
COLORS = {
    "red": "#ff7770", "orange": "#ffad33", "yellow": "#e8d36a", "gray": "#5d686d",
    "blue": "#49a5e8", "green": "#62d59a", "dkgreen": "#2fb876",
}
TONE = {"pos": "#62d59a", "neu": "#b3c0c5", "neg": "#ffbf59"}
GROUP_NUM = "①②③④⑤⑥"
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


def fred(sid):
    if sid not in _fred_cache:
        cache = OUT / f".fred_{sid}.csv"
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
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
    d = d[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    valid = (np.isfinite(d).all(axis=1) & (d[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
             & (d["Volume"] >= 0) & (d["High"] >= d[["Open", "Low", "Close"]].max(axis=1))
             & (d["Low"] <= d[["Open", "High", "Close"]].min(axis=1)))
    rejected = int((~valid).sum())
    d = d.loc[valid]
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d.attrs["rejected_rows"] = rejected
    d.attrs["cached"] = cached
    if len(d) < 2:
        raise ValueError("검증된 시세가 2거래일 미만입니다")
    OUT.mkdir(exist_ok=True)
    d.to_csv(cache)
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
        last_d = close.index[-1]
        year = px[px.index > last_d - pd.Timedelta(weeks=52)]
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
            "price": float(close.iloc[-1]), "prev": float(close.iloc[-2]), "date": last_d.strftime("%Y-%m-%d"),
            "rsi": float(rsi(close).iloc[-1]), "rsi_prev": float(rsi(close).iloc[-6]),
            "hi52": float(year["High"].max()), "hi52_date": year["High"].idxmax().strftime("%Y-%m-%d"),
            "lo52": float(year["Low"].min()), "lo52_date": year["Low"].idxmin().strftime("%Y-%m-%d"),
            "ohlcv":[[d.strftime("%Y-%m-%d"), round(r.Open), round(r.High), round(r.Low), round(r.Close), int(r.Volume)]
                      for d, r in px.iterrows()],
        })
    return out


DART_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DART_FIRST_YEAR = 2015  # 전체 재무제표 API가 제공하는 첫 해
DART_REPORTS = [("11013", 3), ("11012", 6), ("11014", 9), ("11011", 12)]  # 1분기·반기·3분기·사업보고서 (누적 개월)
# 연결재무제표 표준계정(접두어 ifrs_/ifrs-full_/dart_ 제외) → 열 이름. 현금흐름표는 연초부터 누적 금액이다.
DART_CF = {
    "CashFlowsFromUsedInOperatingActivities": "ocf",                # 영업활동현금흐름
    "CashFlowsFromUsedInOperations": "ops",                         # 영업에서 창출된 현금 (이자·배당·법인세 전)
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
        rows += [("비현금 조정·운전자본<small>감가상각비 포함 · 공시 본문에 나눠 있지 않음</small>", ops - ni)]
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
        if not rows:
            raise ValueError("한경 컨센서스 목록이 비어 있습니다")
        df = pd.DataFrame(rows)
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
    df["view"] = df["opinion"].fillna("").map(_opinion)
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
            "upside": (int(last["target"]) / price - 1) * 100, "title": last["title"], "url": last["url"],
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


def _customs_month(month):
    """한 달 전체 품목 수출(달러): 총계 + 설정한 HS 묶음별 합계. 아직 발표 전이면 None."""
    query = urllib.parse.urlencode({"serviceKey": ENV["DATA_GO_KR_KEY"], "strtYymm": month, "endYymm": month})
    root = ET.fromstring(urllib.request.urlopen(f"{CUSTOMS_URL}?{query}", timeout=120).read())
    if root.findtext(".//resultCode") != "00":
        raise ValueError(f"관세청 API: {root.findtext('.//resultMsg')}")
    rows = [(it.findtext("hsCode") or "", float(it.findtext("expDlr") or 0)) for it in root.iter("item")]
    total = next((v for code, v in rows if code in ("-", "")), None)
    if not total:
        return None
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
        return fred(c["series"])
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
        return (fred(c["a"]) - fred(c["b"])).dropna()
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


def analyze(ind, s, kospi):
    source_note = s.attrs.get("source_note")
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if s.empty:
        raise ValueError("유효한 지표 데이터가 없습니다")
    last = s.index[-1]
    v = s.iloc[-1]
    if ind["freq"] == "daily":
        prev = s[: last - pd.Timedelta(days=7)]
        pv = prev.iloc[-1] if len(prev) else np.nan
    else:
        pv = s.get(last - pd.DateOffset(months=1), np.nan)
    since = ind["pct_since"]
    hist = s[s.index >= last - pd.DateOffset(years=1)] if since == "1y" else s[s.index >= since]
    if hist.empty:
        raise ValueError("백분위 기준 기간에 데이터가 없습니다")
    actual_since = hist.index[0].strftime("%Y-%m")
    stale_days = 7 if ind["freq"] == "daily" else 100
    stale = (TODAY - last).days > stale_days
    z = ind["zones"][zone_of(ind, v)]
    return {
        "value": v, "prev": pv, "date": last, "zone": z, "source_note": source_note,
        "pos": marker_pos(ind, v), "prev_pos": None if np.isnan(pv) else marker_pos(ind, pv),
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
        text = "경기 지표는 아직 버티는데 시장 내부가 먼저 약해진 조합"
    elif e < 0 and i < 0:
        text = "경기와 시장 내부가 함께 약한 조합"
    elif e < 0 and i >= 0:
        text = "경기 지표는 약한데 시장은 버티는 조합"
    elif e > 0 and i > 0:
        text = "경기와 시장 내부가 함께 양호한 조합"
    else:
        text = "뚜렷한 방향 없이 혼재된 조합"
    if s < 0:
        text += " · 심리는 공포 쪽"
    if m < 0:
        text += " · 돈의 환경은 부담"
    return text


def asof(s, when):
    t = s[:when]
    return t.iloc[-1] if len(t) else np.nan


def period_returns(px, assets=None):
    rows = []
    for a in assets or CFG["period_assets"]:
        if a["ticker"] not in px:
            continue
        s = px[a["ticker"]].dropna()
        if len(s) < 2:
            continue
        last, v = s.index[-1], s.iloc[-1]
        base = {
            "1D": s.iloc[-2],
            "1W": asof(s, last - pd.Timedelta(days=7)),
            "1M": asof(s, last - pd.DateOffset(months=1)),
            "YTD": asof(s, pd.Timestamp(last.year - 1, 12, 31)),
            "1Y": asof(s, last - pd.DateOffset(years=1)),
        }
        w = s[s.index > last - pd.Timedelta(days=365)]
        rows.append({
            "name": a["name"], "decimals": a["decimals"], "price": v, "date": last,
            **{k: (v / b - 1) * 100 for k, b in base.items()},
            "high": w.max(), "high_date": w.idxmax(), "from_high": (v / w.max() - 1) * 100,
        })
    return rows


def sector_flow(px):
    sc = CFG["sectors"]
    bench = sc["benchmark"]["ticker"]
    monthly = px.resample("ME").last()
    ret = monthly.pct_change(fill_method=None) * 100
    names = {it["ticker"]: it["name"] for it in sc["items"] if it["ticker"] in ret}
    rel = ret[list(names)].sub(ret[bench], axis=0).iloc[-sc["months"]:]
    rel = rel.rename(columns=names).T
    partial = px.index[-1].normalize() < rel.columns[-1].normalize()
    rel["3개월 합계"] = rel.iloc[:, -3:].sum(axis=1, min_count=1)
    return rel.sort_values("3개월 합계", ascending=False), partial


# ---------------------------------------------------------------- 표시 형식

def num(v, d, unit="", sign=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return f"{v:+,.{d}f}{unit}" if sign else f"{v:,.{d}f}{unit}"


def delta_unit(unit):
    return "%p" if unit in ("%", "%p") else unit


def heat(v, cap):
    """값 크기에 따라 초록(+)/빨강(−) 배경."""
    if v is None or np.isnan(v):
        return "background:#1b1b1b"
    if v == 0:
        return "background:#303030"
    a = 0.18 + 0.72 * min(abs(v) / cap, 1)
    rgb = "98,213,154" if v > 0 else "255,119,112"
    return f"background:rgba({rgb},{a:.2f})"


esc = html.escape


# ---------------------------------------------------------------- HTML

def render_row(ind, res, err, note=None):
    star = "★ " if ind.get("star") else ""
    name = f'<div class="nm"><b>{star}{esc(ind["name"])}</b><small>{esc(ind["sub"])}</small></div>'
    if res is None:
        msg = note or ind["calc"].get("note") if note or ind["calc"]["src"] == "pending" else f"데이터 오류: {err}"
        return f'<div class="row pending" id="ind-{ind["id"]}">{name}<div class="pend">{esc(msg)}</div></div>'
    unit, d = ind["unit"], ind["decimals"]
    prev_label = "1주 전" if ind["freq"] == "daily" else "전월"
    z = res["zone"]
    return (
        f'<div class="row" id="ind-{ind["id"]}">{name}'
        f'<div class="val"><b>{num(res["value"], d)}<small>{unit}</small></b>'
        f'<small>{prev_label} {num(res["prev"], d)}</small><small class="asof">{res["date"]:%Y-%m-%d} 기준{" · 갱신 지연" if res.get("stale") else ""}</small>{"<small>" + esc(res["source_note"]) + "</small>" if res.get("source_note") else ""}</div>'
        f'<div class="mean"><b style="color:{COLORS[z["color"]]}">{esc(z["label"])}</b><small>{esc(z["meaning"])}</small></div>'
        f'<div class="pos"><b>{res["pct"]:.0f}%</b><div class="mini"><i style="width:{res["pct"]:.0f}%"></i></div>'
        f'<small>{res["since"]}</small></div></div>')


def render_period(rows):
    caps = {"1D": 3, "1W": 6, "1M": 12, "YTD": 35, "1Y": 50}
    body = ""
    for r in rows:
        cells = "".join(f'<td class="hc" style="{heat(r[k], c)}">{num(r[k], 1, "%", True)}</td>' for k, c in caps.items())
        fh = r["from_high"]
        col = "#edf2f0" if fh > -5 else "#ffad33" if fh > -15 else "#ff7770"
        stale = f'<small>{r["date"]:%m-%d} 기준</small>' if (TODAY - r["date"]).days > 3 else ""
        body += (
            f'<tr><th>{esc(r["name"])}</th><td class="px">{num(r["price"], r["decimals"])}{stale}</td>{cells}'
            f'<td class="hi"><b style="color:{col}">{num(fh, 1, "%", True)}</b>'
            f'<small>최고 {num(r["high"], r["decimals"])} · {r["high_date"]:%y-%m-%d}</small></td></tr>')
    head = "".join(f"<th>{k}</th>" for k in caps)
    return (f'<table class="ptab"><thead><tr><th></th><th>현재가</th>{head}<th>52주 종가 최고 대비</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def render_sectors(rel, partial):
    months = list(rel.columns[:-1])
    head = ""
    for i, m in enumerate(months):
        yr = f"<small>{m.year}</small>" if i == 0 or m.month == 1 else "<small>&nbsp;</small>"
        star = "*" if partial and i == len(months) - 1 else ""
        head += f"<th>{yr}{m.month:02d}월{star}</th>"
    body = ""
    for name, row in rel.iterrows():
        cells = "".join(f'<td class="hc" style="{heat(row[m], 12)}">{num(row[m], 1, "", True)}</td>' for m in months)
        t = row["3개월 합계"]
        body += (f'<tr><th>{esc(name)}</th>{cells}'
                 f'<td class="sum" style="color:{"#62d59a" if t > 0 else "#ff7770"}">{num(t, 1, "", True)}</td></tr>')
    return (f'<table class="stab"><thead><tr><th></th>{head}<th>3개월<br>합계</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


CSS = """
:root{color-scheme:dark;--series-1:#49a5e8;--bg:#050505;--surface:#262626;--surface-2:#303030;--border:#353535;--text:#edf2f0;--text-2:#b3c0c5;--text-3:#81939b;--accent:#ffad33;--good:#62d59a;--warn:#ffbf59;--bad:#ff7770;--card:var(--surface);--line:var(--border);--tx:var(--text);--mu:var(--text-3)}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.45 "Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif}
.wrap{max-width:1320px;margin:0 auto;padding:16px}
h1{font-size:17px;margin:0 0 4px}
.meta{color:var(--mu);font-size:12px}
.tabs{display:flex;margin:12px 0;border:1px solid var(--border);border-radius:14px;overflow:hidden;width:fit-content;background:var(--surface)}
.tabs button{background:transparent;border:0;border-right:1px solid var(--border);color:var(--text-2);padding:9px 18px;cursor:pointer;font:inherit;font-weight:600;font-size:12px}.tabs button:last-child{border-right:0}.tabs button:hover{background:var(--surface-2);color:var(--text)}
.tabs button.on{background:var(--accent);color:#091018;font-weight:700}
.tab{display:none}.tab.on{display:block}
.card{background:var(--surface);border:1px solid var(--border);border-radius:24px;padding:18px;margin-bottom:14px;box-shadow:inset 0 1px #ffffff0a}
.status b.big{font-size:15px}
.headline{background:var(--bg);border-left:3px solid var(--accent);padding:8px 12px;margin:10px 0;border-radius:4px}
.kpis{color:var(--mu)}.kpis b{color:var(--tx)}
.gcards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px}
.gcard{border:1px solid var(--line);border-top:3px solid;border-radius:8px;padding:8px 10px}
.gcard div{font-size:16px;font-weight:700}
.gcard small{color:var(--mu)}
.flow{margin-top:6px;color:var(--mu)}.flow a{color:var(--mu);text-decoration:none;margin-right:2px}.flow a:hover b{text-decoration:underline}.flow b{color:var(--tx)}
.jump{margin:-4px 0 8px;color:var(--mu);font-size:12px}.jump a{display:inline-block;margin:2px 4px;padding:3px 10px;border:1px solid var(--line);border-radius:12px;color:var(--tx);text-decoration:none}.jump a:hover{border-color:var(--accent);color:var(--accent)}
.chead{font-size:18px;font-weight:700;margin:2px 0 6px}.chead .cdate{font-size:12px;font-weight:500;color:var(--mu);background:var(--bg);border-radius:5px;padding:2px 8px;margin-right:10px;vertical-align:middle}
.clegend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px 14px;color:var(--mu);font-size:12px;margin:8px 0 4px}
.clegend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}.clegend i.sq{width:10px;height:10px;border-radius:2px}
.cpanel{position:relative;border-top:1px solid var(--line);padding-top:4px}.cname{position:absolute;left:4px;top:6px;font-size:12px;color:var(--mu);z-index:2}
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
.sbadge{border:1px solid var(--accent);color:var(--accent);background:rgba(255,173,51,.1);font-weight:700;font-size:12px;padding:1px 6px;margin-left:8px;vertical-align:middle}
.smkt{color:var(--mu);font-size:12px;margin-left:6px}
.sprice{text-align:right}.sprice small{display:block;color:var(--mu);font-size:11px}.sprice b{display:block;font-size:24px}.sprice span{font-weight:700}
.schartwrap{position:relative}.schart{width:100%;height:560px}
.rsiwrap{position:relative;border-top:1px solid #2a2a2a}.rsichart{width:100%;height:150px}
.sright{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap}.srsi{text-align:right;border-left:1px solid var(--border);padding-left:16px}.srsi small{display:block;color:var(--mu);font-size:11px}
.srsi b{font-size:24px;margin-right:6px}.srsi span{font-weight:700;color:var(--text-2)}
.srsi.hot b,.srsi.hot span{color:var(--warn)}.srsi.cold b,.srsi.cold span{color:var(--series-1)}
@media (max-width:760px){.srsi{border-left:0;padding-left:0;text-align:left}}
.slegend{position:absolute;left:10px;top:8px;z-index:3;font-size:12.5px;color:var(--text-2);pointer-events:none;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center}
.smabox{position:absolute;left:10px;top:36px;z-index:3;pointer-events:none;background:rgba(10,10,10,.82);border:1px solid #3a3a3a;border-radius:8px;padding:8px 10px;font-size:12px;color:var(--text-2);font-variant-numeric:tabular-nums;min-width:210px}
.smabox .mh{color:var(--text);font-weight:600;margin-bottom:5px}.smabox table{border-collapse:collapse;width:100%}.smabox td{padding:2px 0}.smabox td+td{text-align:right;padding-left:12px}
.smabox i{display:inline-block;width:10px;height:2px;margin:0 6px 3px 0}.smabox small{color:var(--mu);font-size:10.5px}
.slegend .lname{color:var(--text);font-size:14px;font-weight:600}.slegend b{font-weight:500;margin:0 6px 0 2px}.slegend .lma i{display:inline-block;width:10px;height:2px;margin:0 3px 3px 6px}
.ftab{width:100%;border-collapse:separate;border-spacing:0 4px}
.ftab thead th{color:var(--mu);font-weight:500;font-size:12px;text-align:left;padding:4px 14px}
.ftab tbody th{background:var(--bg);color:var(--text-2);font-weight:500;text-align:left;padding:12px 14px;border-radius:10px 0 0 10px;border-left:3px solid var(--border);width:24%}
.ftab tbody th small{display:block;color:var(--mu);font-size:10.5px;font-weight:400}.ftab tbody th b{color:var(--text)}
.ftab td{text-align:left;padding:12px 14px;font-weight:700;background:var(--bg);font-variant-numeric:tabular-nums}.ftab td:last-child{border-radius:0 10px 10px 0}
.qhead{display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin-bottom:12px}.qhead>span:last-child{color:var(--mu);font-size:12px}
.qsplit{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px;align-items:start}
.qleft .ilegend{justify-content:flex-start;flex-wrap:wrap;margin-bottom:8px}.qleft .ictrl{gap:6px}.qleft .igroup button{padding:5px 9px}
.qtab{border-spacing:0;width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12px;font-variant-numeric:tabular-nums}
.qtab th,.qtab td{padding:9px 6px;border-bottom:1px solid var(--border);background:transparent;border-left:0;border-radius:0;text-align:right;white-space:nowrap}
.qtab thead th{color:var(--mu);font-weight:600;font-size:11.5px}.qtab thead th.qgrp{text-align:center;color:var(--text-2);border-bottom:1px solid var(--border)}
.qtab tbody th{text-align:left;color:var(--text);font-family:inherit;font-weight:700;font-size:12.5px}.qtab tbody th small{display:block;color:var(--mu);font-weight:400;font-size:10.5px}
.qtab th:first-child{text-align:left}.qtab td.up{color:#ff7770}.qtab td.down{color:#49a5e8}.qtab td{color:var(--text)}.qtab td.qnow{font-weight:700}
.qshort{margin-top:14px}.qnote{color:var(--text-2);font-size:12px;margin:12px 0 0}.qnote b{color:var(--text)}
@media (max-width:1100px){.qsplit{grid-template-columns:minmax(0,1fr)}}
.ftab.fwide{min-width:760px}.ftab td small{display:block;font-size:10.5px;font-weight:500;opacity:.8;margin-top:2px}
.ftab td.fplain{color:var(--text)}.ftab tr.fsep th,.ftab tr.fsep td{box-shadow:inset 0 2px 0 var(--border)}
.ftab td.fpos{color:var(--good);background:rgba(98,213,154,.09)}.ftab td.fneg{color:var(--bad);background:rgba(255,119,112,.09)}
.itop{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px}.ilegend{display:flex;gap:12px;color:var(--text-2);font-size:12px}.ilegend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.ictrl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}.igroup{display:flex;border:1px solid var(--border);border-radius:10px;overflow:hidden}.igroup button{background:transparent;border:0;border-right:1px solid var(--border);color:var(--text-2);font:inherit;font-size:12px;padding:6px 12px;cursor:pointer}.igroup button:last-child{border-right:0}.igroup button:hover{background:var(--surface-2)}.igroup button.on{background:var(--bg);color:var(--text);font-weight:700;box-shadow:inset 0 -2px var(--accent)}
.ichart{width:100%;height:300px}.ilegend i.idash{width:14px;height:0;border-radius:0;border-top:2px dashed #c39bff;vertical-align:middle}
.fhead{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}.fhead>span:last-child{color:var(--mu);font-size:12px}
.fbanner{margin:0 0 12px;padding:8px 12px;border-radius:10px;font-size:12.5px}.fbanner.good{background:rgba(98,213,154,.1);color:var(--good)}.fbanner.bad{background:rgba(255,119,112,.1);color:var(--bad)}
.fkpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.fkpi{background:var(--bg);border:1px solid var(--border);border-radius:16px;padding:14px 10px;text-align:center}.fkpi small{display:block;color:var(--mu);font-size:11px}
.fkpi b{display:block;font-size:24px;margin:4px 0;font-variant-numeric:tabular-nums}.fkpi b.good{color:var(--good)}.fkpi b.bad{color:var(--bad)}
.fbody{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:18px;margin-top:16px}.fmode{width:fit-content;margin-bottom:6px}.fchart{width:100%;height:280px}
.fcap{color:var(--mu);font-size:12px;margin-bottom:6px}
.fbreak{border-spacing:0}.fbreak th,.fbreak td{border-top:1px solid var(--border);background:transparent;border-left:0;border-radius:0;padding:9px 4px;font-weight:400;color:var(--text-2);white-space:normal}
.fbreak th small{display:block;color:var(--mu);font-size:10.5px}.fbreak td{text-align:right;color:var(--text);font-variant-numeric:tabular-nums;white-space:nowrap}.fbreak td.bad{color:var(--bad)}
.fkpi b .good,.fkpi small .good,.rtab .good{color:var(--good)}.fkpi small .bad,.rtab .bad{color:var(--bad)}
.rtab{border-spacing:0;min-width:880px}.rtab thead th{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}
.rtab td,.rtab tbody th{text-align:left;padding:9px 10px;border-top:1px solid var(--border);background:transparent;border-left:0;border-radius:0;vertical-align:top}
.rtab tbody th{font-weight:600;color:var(--text)}.rtab small{display:block;color:var(--mu);font-size:10.5px;font-weight:400}
.rtab td.rnum{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}.rtab td.rdate{color:var(--mu);font-variant-numeric:tabular-nums}
.rtab td.rtitle{white-space:normal;min-width:260px}.rtab td.rtitle a{color:var(--text-2);text-decoration:none}.rtab td.rtitle a:hover{color:var(--accent)}
.rop{border:1px solid var(--border);border-radius:6px;padding:1px 7px;font-size:12px}.rop.good{border-color:rgba(98,213,154,.5)}.rop.warn{color:var(--warn);border-color:rgba(255,191,89,.5)}.rop.bad{border-color:rgba(255,119,112,.5)}
.rtab tr.old{opacity:.45}
.fbreak tr.sub th,.fbreak tr.sub td{color:var(--text);font-weight:600}.fbreak tr.total th,.fbreak tr.total td{color:var(--text);font-weight:700;border-top:1px solid var(--text-3)}
.tvcard{padding:0;overflow:hidden}.tvtitle{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border)}
.tvtitle b{font-size:16px}.tvtitle span{color:var(--mu);font-size:12px}.tvtitle a{color:var(--mu);text-decoration:none}.tvtitle a:hover{color:var(--accent)}
.tvframe{margin:18px 20px 6px;background:#000;border:1px solid #1b1b1b}
.tvbar,.tvfoot{display:flex;align-items:center;gap:2px;padding:6px 8px;background:#000}
.tvbar button,.tvfoot button{background:transparent;border:0;color:var(--text-2);font:inherit;font-size:13px;padding:5px 8px;border-radius:4px;cursor:pointer}
.tvbar button:hover,.tvfoot button:hover{background:#1f1f1f;color:var(--text)}.tvbar button.on,.tvfoot button.on{background:#2a2a2a;color:var(--text)}
.tvlbl{color:var(--mu);font-size:12px;margin:0 4px}
.tvbar{flex-wrap:wrap}
.mabtn i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}.tvbar .mabtn:not(.on){opacity:.4}.tvbar .mabtn:not(.on) i{background:transparent!important;border:1px solid var(--mu)}
.tvbar .sep{width:1px;height:20px;background:#2a2a2a;margin:0 6px}
.tvclock{margin-left:auto;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
.tvcredit{text-align:center;color:var(--mu);font-size:11px;padding:2px 0 14px}
.row:target{background:var(--surface-2);outline:1px solid var(--accent);border-radius:6px}
.fresh{color:var(--mu);font-size:11.5px;margin-top:8px}.fresh b{color:var(--accent);font-weight:500}
.sec-title{display:inline-block;font-size:16px;font-weight:700;border:1px solid var(--border);background:var(--bg);padding:6px 14px;border-radius:12px;margin-right:10px}
.sec-head{margin-bottom:12px}.sec-head span{color:var(--mu);font-size:12px}
.scroll{overflow-x:auto}
.grid{min-width:700px}
.ghead{margin:16px 0 6px;font-weight:700}
.ghead .pill{font-size:11px;border:1px solid;border-radius:4px;padding:1px 6px;margin:0 6px}
.ghead small{color:var(--mu);font-weight:400}
.row{display:grid;grid-template-columns:210px 120px minmax(230px,1fr) 110px;gap:12px;align-items:center;padding:8px 6px;border-bottom:1px solid var(--line)}
.row.pending{grid-template-columns:210px 1fr;opacity:.6}
.pend{color:var(--mu);font-style:italic}
.nm b{display:block}.nm small,.val small,.mean small,.pos small{display:block;color:var(--mu);font-size:11px}
.val b{font-size:17px}.val b small{display:inline;font-size:11px;color:var(--mu);margin-left:2px}
.bar{position:relative;padding-top:15px}
.segs{display:flex;gap:2px}
.seg{flex:1;position:relative}
.seg span{position:absolute;top:-15px;left:0;right:0;text-align:center;font-size:10.5px;color:var(--mu);white-space:nowrap;overflow:hidden}
.seg i{display:block;height:9px;border-radius:2px;opacity:.85}
.marks{position:absolute;left:0;right:0;bottom:0;height:9px}
.dot{position:absolute;top:50%;transform:translate(-50%,-50%);border-radius:50%}
.dot.now{width:15px;height:15px;border:2px solid #fff;box-shadow:0 0 0 2px #0008}
.dot.prev{width:7px;height:7px;background:var(--bg);border:1.5px solid #fff}
.trail{position:absolute;top:50%;border-top:1.5px dotted #fff;opacity:.7}
.mean{background:var(--bg);border-radius:6px;padding:6px 8px}
.pos b{font-size:15px}.mini{height:3px;background:var(--surface-2);border-radius:2px;margin:3px 0}.mini i{display:block;height:3px;background:var(--text-2);border-radius:2px}

.muted{color:var(--mu);font-size:11px}
.legend{color:var(--mu);font-size:11px;margin-top:10px}
table{border-collapse:separate;border-spacing:3px;width:100%}
th{font-weight:600;text-align:left;white-space:nowrap;padding:4px 8px}
thead th{text-align:center;color:var(--mu);font-size:11.5px;font-weight:500}
thead th small{display:block;font-size:10px}
td{padding:6px 8px;text-align:center;white-space:nowrap}
td.hc{border-radius:4px;font-weight:600;min-width:62px}
tbody th{background:var(--bg);border-left:3px solid var(--border);border-radius:4px}
td.sum{font-weight:700}
td.px{text-align:right}td.px small,td.hi small{display:block;color:var(--mu);font-size:10.5px;font-weight:400}
td.hi{background:var(--bg);border-radius:4px}
.ptab td.hc{min-width:96px}
.digest{background:var(--bg);border-left:3px solid var(--accent);border-radius:4px;padding:10px 14px;margin-bottom:12px}
.digest ul{margin:6px 0 6px 18px;padding:0}.digest li{margin:3px 0}.digest small{color:var(--mu);font-size:11px}
.note{color:var(--mu);font-size:12px;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:8px 12px;margin:0 0 10px}
.ntools{display:flex;gap:8px;margin-bottom:10px}.ntools select,.ntools input{background:var(--bg);border:1px solid var(--border);color:var(--tx);border-radius:10px;padding:8px 12px;font:inherit;font-size:13px}
.ntools select{min-width:190px}.ntools input{flex:1;min-width:0}.ntools input:focus,.ntools select:focus{outline:1px solid var(--accent);border-color:var(--accent)}
.nperiod{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}.nperiod>span{color:var(--mu);font-size:12px;margin-right:4px}
.nperiod button{background:var(--surface);border:1px solid var(--border);color:var(--text-2);border-radius:10px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
.nperiod button b{color:var(--mu);font-weight:500;margin-left:2px}.nperiod button:hover{color:var(--tx)}
.nperiod button.on{border-color:var(--accent);color:var(--accent);background:rgba(255,173,51,.1)}.nperiod button.on b{color:var(--accent)}
.nday h3{font-size:14px;margin:22px 0 8px;padding-bottom:8px;border-bottom:1px solid var(--border)}.nday h3 small{color:var(--mu);font-weight:500}
.nbox{border-left:2px solid var(--border)}
.nrow{display:flex;align-items:flex-start;gap:12px;padding:10px 12px;border-bottom:1px solid var(--border)}.nrow:hover{background:var(--surface-2)}
.ntime{color:var(--mu);font-size:12px;font-family:ui-monospace,Consolas,monospace;padding-top:2px;flex-shrink:0}
.nsrc{flex-shrink:0;background:var(--surface-2);color:var(--text-2);border-radius:6px;padding:2px 8px;font-size:11px;font-weight:700;max-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nmain{flex:1;min-width:0}.nline{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.nchip{font-size:12px;font-weight:600}.ndot{color:var(--mu);font-style:normal;font-size:12px}
.nrow .ntitle{display:inline;margin:0;font-weight:500;font-size:14px}
.nrow p{margin:4px 0 0;color:var(--text-2);font-size:12px}
.nrow details{flex-shrink:0;font-size:12px}.nrow details summary{cursor:pointer;color:var(--mu);list-style:none;border:1px solid var(--border);border-radius:6px;padding:0 6px}
.nrow details[open]{flex-basis:100%;order:5}.nrow details ul{margin:8px 0 0;padding-left:14px;border-left:1px solid var(--line);list-style:none}
.nrow details li{margin:3px 0}.nrow details a{color:var(--text-2);text-decoration:none}.nrow details a:hover{color:var(--accent)}.nrow details span{color:var(--mu)}
.ngo{flex-shrink:0;color:var(--mu);text-decoration:none}.ngo:hover{color:var(--accent)}
.nrow{flex-wrap:wrap}
@media (max-width:760px){.ntools{flex-direction:column}.ntools select{min-width:0}.nrow{gap:6px 10px}.nmain{flex-basis:100%;order:4}}
.evtools{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.evfilter{display:flex;flex-wrap:wrap;gap:6px}.evfilter button{background:var(--surface);border:1px solid var(--border);color:var(--text-2);border-radius:10px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
.evfilter button.on{border-color:var(--accent);color:var(--accent);background:rgba(255,173,51,.1)}.evonly{color:var(--text-2);font-size:12px;cursor:pointer}
.evlist{columns:2 460px;column-gap:28px}.evmonth{font-size:13px;color:var(--mu);margin:14px 0 2px;break-after:avoid}
.evrow{display:block;break-inside:avoid;padding:12px 4px;border-bottom:1px solid var(--border);color:inherit;text-decoration:none}.evrow:hover .evtitle{color:var(--accent)}
.evtop{display:flex;align-items:center;gap:8px}.evtime{color:var(--mu);font-family:ui-monospace,Consolas,monospace;font-size:13px}.evtime b{color:var(--text-2);font-weight:500}
.evd{font-size:11px;font-weight:700;border-radius:6px;padding:1px 6px;background:var(--surface-2);color:var(--text-2)}.evd:empty{display:none}.evd.today{background:rgba(255,173,51,.15);color:var(--accent)}
.evstars{margin-left:auto;color:var(--accent);letter-spacing:1px;white-space:nowrap}.evstars i{color:var(--mu);font-style:normal}
.evtitle{font-weight:700;font-size:14px;margin-top:5px}.evsub{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:5px;color:var(--mu);font-size:11.5px}
.evreg{font-weight:700}.evreg.us{color:#49a5e8}.evreg.kr{color:#62d59a}.evreg.jp{color:#ff7770}.evreg.co{color:#ffad33}.evreg.mk{color:#c39bff}
.evrow.past{opacity:.4}
.cal{margin:10px 0 18px}.calnav{display:flex;align-items:center;justify-content:center;gap:22px;margin-bottom:10px}.calnav b{font-size:18px;min-width:130px;text-align:center}
.calnav button{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;width:34px;height:32px;font-size:18px;cursor:pointer}
.calnav button:disabled{opacity:.3;cursor:default}.calnav button:not(:disabled):hover{border-color:var(--accent);color:var(--accent)}
.calhead,.calgrid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}.calhead span{text-align:center;color:var(--text-2);font-size:12px;font-weight:700;padding:4px 0}
.calhead span:first-child{color:#ff7770}.calhead span:last-child{color:#49a5e8}
.calcell{background:var(--bg);border:1px solid var(--border);border-radius:10px;min-height:96px;padding:7px 8px;min-width:0;cursor:pointer}.calcell:hover{border-color:var(--text-3)}
.calcell.blank{background:transparent;border:0;cursor:default}.calcell.today{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.calcell .cd{font-weight:700;font-size:13px;margin-bottom:4px;color:var(--text)}.calcell.we .cd{color:#49a5e8}.calcell.sun .cd{color:#ff7770}.calcell.today .cd{color:var(--accent)}
.calcell .ci{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.55}
.calcell .ci i{flex-shrink:0;width:6px;height:6px;border-radius:50%}.calcell .ci span{overflow:hidden;text-overflow:ellipsis}
.calcell .cmore{color:var(--mu);font-size:11px}
.ci.us i{background:#49a5e8}.ci.kr i{background:#62d59a}.ci.jp i{background:#ff7770}.ci.co i{background:#ffad33}.ci.mk i{background:#c39bff}
.calcell.picked{border-color:var(--text-2);background:var(--surface-2)}
.evday{background:var(--bg);border:1px solid var(--border);border-radius:14px;padding:6px 16px 4px;margin:-6px 0 14px}
.evdayhead{font-weight:700;font-size:14px;padding:8px 4px 2px}.evday .evrow:last-child{border-bottom:0}.evday .muted{padding:8px 4px}
@media (max-width:760px){.calcell{min-height:52px;padding:5px}.calcell .ci{display:inline-flex}.calcell .ci span,.calcell .cmore{display:none}.calhead,.calgrid{gap:3px}}
.upnext{margin:-2px 0 10px;color:var(--text-2);font-size:12.5px}.upnext a{color:var(--accent);text-decoration:none;font-weight:700;margin-right:8px}.upnext b{color:var(--text)}
.nfilter{display:flex;gap:6px;margin-bottom:8px}
.nfilter button{background:var(--bg);border:1px solid var(--line);color:var(--tx);padding:5px 12px;border-radius:5px;cursor:pointer;font:inherit;font-size:12px}
.nfilter button.on{border-color:var(--accent);color:var(--accent)}
.nlist article{padding:10px 4px;border-bottom:1px solid var(--line)}
.nmeta{color:var(--mu);font-size:11px}.nmeta .tag{font-weight:700;border-radius:3px;padding:1px 6px;margin-right:6px}
.ntitle{display:block;color:var(--tx);font-weight:600;text-decoration:none;margin-top:3px}.ntitle:hover{color:var(--accent)}
.nlist p{margin:4px 0 0;color:var(--text-2);font-size:12px}
.nlist details{margin-top:5px;font-size:12px}.nlist summary{cursor:pointer;color:var(--mu)}
.nlist details ul{margin:6px 0 0;padding-left:14px;border-left:1px solid var(--line);list-style:none}
.nlist details li{margin:3px 0}.nlist details a{color:var(--text-2);text-decoration:none}.nlist details a:hover{color:var(--accent)}
.nlist details span{color:var(--mu)}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.tabs{max-width:100%;overflow-x:auto;white-space:nowrap;flex-wrap:wrap}.tabs button{flex-shrink:0}
.tvbar,.tvfoot{flex-wrap:wrap}.tvframe{margin:12px 8px 6px}.asof{font-size:10px}
@media (max-width:900px){.fbody{grid-template-columns:1fr}}
@media (max-width:760px){.fkpis{grid-template-columns:repeat(2,1fr)}.fkpi b{font-size:19px}.gcards{grid-template-columns:repeat(2,1fr)}.wrap{padding:8px}.card{padding:12px}.tabs button{padding:9px 10px}.tvclock{width:100%;margin-left:0}.cchart{min-width:500px}.cpanel{overflow-x:auto}}
"""


NEWS_ARCHIVE_DAYS = 400  # 저장본에 남겨 두는 기간 (월별 버튼용)


def _merge_story(pool, story):
    """같은 기사(대표 기사나 묶인 기사의 링크가 겹침)가 이미 있으면 합치고, 없으면 새로 넣는다."""
    links = {story["link"]} | {r["link"] for r in story["related"]}
    existing = next((s for s in pool if s["link"] in links or links & {r["link"] for r in s["related"]}), None)
    if existing is None:
        pool.append(story)
        return
    for t in story.get("tickers", [story["ticker"]]):
        if t not in existing.setdefault("tickers", [existing["ticker"]]):
            existing["tickers"].append(t)
    have = {existing["link"]} | {r["link"] for r in existing["related"]}
    existing["related"] += [r for r in ([{k: story[k] for k in ("title", "source", "published_at", "link")}] + story["related"])
                            if r["link"] not in have]
    existing["latest"] = max(existing["latest"], story["latest"])


def news_archive(fresh):
    """이번에 받은 뉴스를 output/.news_archive.json에 쌓는다. 처음에는 지난달 1일부터 1주씩 나눠 받아 채운다."""
    path = OUT / ".news_archive.json"
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pool = []
        start = (TODAY - pd.DateOffset(months=1)).replace(day=1)
        days = (TODAY - start).days + 1
        for ticker in news.COMPANIES:
            for a in pd.date_range(start, TODAY, freq="7D"):
                b = min(a + pd.Timedelta(days=7), TODAY + pd.Timedelta(days=1))
                try:
                    for story in news.fetch_stories(ticker, days=days + 1, after=f"{a:%Y-%m-%d}", before=f"{b:%Y-%m-%d}"):
                        story["tickers"] = [ticker]
                        _merge_story(pool, story)
                except Exception as e:
                    print(f"  ! 뉴스 지난 기간 {a:%m-%d}: {e.__class__.__name__}")
        print(f"  뉴스 저장본 처음 채움: {len(pool)}건 ({start:%Y-%m-%d}부터)")
    for story in fresh:
        _merge_story(pool, story)
    cutoff = (datetime.now(ZoneInfo(KST)) - pd.Timedelta(days=NEWS_ARCHIVE_DAYS)).isoformat()
    pool = [s for s in pool if (s["latest"] or "") >= cutoff]
    pool.sort(key=lambda s: s["published_at"] or "", reverse=True)
    path.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    return pool


def collect_news():
    """두 회사 뉴스 (주요 언론사 · 같은 내용 묶음) + 키가 있으면 AI 요약."""
    stories, errors = [], []
    seen = set()
    for ticker, info in news.COMPANIES.items():
        try:
            # 두 회사를 함께 다룬 기사는 양쪽 피드에 다 나오므로 한 번만 넣는다.
            for story in news.fetch_stories(ticker):
                existing = next((s for s in stories if s["id"] == story["id"]), None)
                if existing:
                    existing.setdefault("tickers", [existing["ticker"]])
                    if ticker not in existing["tickers"]:
                        existing["tickers"].append(ticker)
                else:
                    story["tickers"] = [ticker]
                    stories.append(story)
        except Exception as e:
            errors.append(f"{info['name']} 뉴스를 받지 못했습니다 ({e.__class__.__name__})")
    stories.sort(key=lambda s: s["latest"], reverse=True)

    digest, digest_note = None, None
    api_key = ENV.get("ANTHROPIC_API_KEY")
    if not stories:
        pass
    elif not api_key:
        digest_note = "AI 요약을 쓰려면 .env에 ANTHROPIC_API_KEY를 넣으세요. 지금은 기사 목록만 표시합니다."
    else:
        try:
            model = ENV.get("NEWS_SUMMARY_MODEL") or "claude-sonnet-5-5"
            digest = news.digest(stories, api_key, model, OUT / ".news_digest.json")
        except Exception as e:
            digest_note = f"AI 요약을 만들지 못했습니다 ({e.__class__.__name__}). 기사 목록만 표시합니다."
            print(f"  ! 뉴스 요약: {e.__class__.__name__}")
    print(f"  뉴스 {len(stories)}건 (같은 내용 {sum(len(s['related']) for s in stories)}건 묶음)")
    try:
        archive = news_archive(stories)
    except Exception as e:  # 저장본이 망가져도 이번에 받은 뉴스는 보여 준다
        archive = stories
        print(f"  ! 뉴스 저장본: {e.__class__.__name__}")
    return {"stories": archive, "digest": digest, "digest_note": digest_note, "errors": errors}


CREDIT_WINDOW_YEARS = 2
CRASH_DAY = -5.0          # 지수 하루 −5% 이하 = 급락일
CREDIT_DROP_DAY = -3.0    # 신용 하루 −3% 이하 = 신용 급감
FAST_QUANTILE = 0.15      # 20일 신용 증감률이 2010년 이후 상·하위 15% = 빨리 쌓임/빨리 줄어듦


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


def _runs(mask):
    """True가 이어지는 구간들의 (시작, 끝) 날짜."""
    out, start, prev = [], None, None
    for d, flag in mask.items():
        if flag and start is None:
            start = d
        if not flag and start is not None:
            out.append((start, prev))
            start = None
        prev = d
    if start is not None:
        out.append((start, prev))
    return out


def credit_panel(name, credit, index, cap, fast_hi, fast_lo, since):
    """한 시장(코스피/코스닥)의 차트 데이터: 금액(조원)과 시총 대비 비율(%) 두 가지."""
    credit = credit.where(credit > 0)
    if credit.dropna().empty:
        raise ValueError(f"{name} 신용 데이터가 비어 있습니다")
    df = pd.DataFrame({"credit": credit, "index": index}).dropna(subset=["credit"])
    df["index"] = df["index"].ffill()
    df["ratio"] = (credit / cap * 100).reindex(df.index) if cap is not None else np.nan
    df["chg20"] = df["credit"].pct_change(20, fill_method=None) * 100
    idx_ret = index.dropna().pct_change() * 100
    df = df[df.index >= since]
    day = lambda d: d.strftime("%Y-%m-%d")

    crash = [d for d in df.index if idx_ret.get(d, 0) <= CRASH_DAY]
    credit_ret = df["credit"].pct_change(fill_method=None) * 100
    drops = [d for d in df.index if credit_ret.get(d, 0) <= CREDIT_DROP_DAY]
    shade = ([{"from": day(a), "to": day(b), "kind": "up"} for a, b in _runs(df["chg20"] >= fast_hi)] +
             [{"from": day(a), "to": day(b), "kind": "down"} for a, b in _runs(df["chg20"] <= fast_lo)])

    def view(col, scale, unit, decimals):
        s = df[col].dropna() / scale
        if s.empty:
            return None
        peak = s.idxmax()
        return {
            "unit": unit, "decimals": decimals,
            "line": [[day(d), round(v, 4)] for d, v in s.items()],
            "labels": [[day(d), round(v, 4)] for d, v in _extremes(s)],
            "crash": [[day(d), round(s[d], 4)] for d in crash if d in s.index],
            "drops": [[day(d), round(s[d], 4)] for d in drops if d in s.index],
            "peak": [day(peak), round(s[peak], 4)],
        }

    if df.empty:
        raise ValueError(f"{name} 신용 데이터가 비어 있습니다")
    # 머리말: 최근 20거래일 안의 꼭지(또는 바닥)에서 지금까지의 변화
    recent = df["credit"].tail(21)
    last_d, last_v = recent.index[-1], recent.iloc[-1]
    ref_d = recent.idxmax() if recent.idxmax() != last_d else recent.idxmin()
    days = len(recent.loc[ref_d:]) - 1
    chg = (last_v / recent[ref_d] - 1) * 100
    verb = "줄었어요" if chg < 0 else "늘었어요"
    headline = (f"{name} 신용 {last_v / 1e12:.1f}조, {days}거래일 새 {chg:+.1f}% {verb}"
                if days > 0 else f"{name} 신용 {last_v / 1e12:.1f}조")
    return {
        "name": name, "asof": day(last_d), "headline": headline,
        "index": [[day(d), round(v, 2)] for d, v in df["index"].dropna().items()],
        "shade": shade,
        "amount": view("credit", 1e12, "조", 1),
        "ratio": view("ratio", 1, "%", 2),
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
        chg20 = credit[column].pct_change(20, fill_method=None) * 100
        hi, lo = chg20.quantile(1 - FAST_QUANTILE), chg20.quantile(FAST_QUANTILE)
        panels.append(credit_panel(name, credit[column], index, cap, hi, lo, since))
    return {"panels": panels, "notes": notes,
            "rules": {"crash": CRASH_DAY, "drop": CREDIT_DROP_DAY, "fast": int(FAST_QUANTILE * 100)}}


ECHARTS_URL = "https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"
LWC_URL = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"

CREDIT_JS = r"""
const CREDIT = __DATA__;
const COLORS = {코스피: '#49a5e8', 코스닥: '#ffad33', index: '#81939b', crash: '#ff7770', drop: '#edf2f0',
                up: 'rgba(255,119,112,0.12)', down: 'rgba(98,213,154,0.10)', grid: '#2c2c2c', text: '#81939b'};
function fmt(v, view) { return view.decimals === 1 ? v.toFixed(1) + view.unit : v.toFixed(2) + view.unit; }
function creditOption(panel, kind) {
  const view = panel[kind], color = COLORS[panel.name];
  const vals = view.line.map(p => p[1]), lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.12;
  const last = view.line[view.line.length - 1], lastIdx = panel.index.length ? panel.index[panel.index.length - 1] : null;
  return {
    animation: false, backgroundColor: 'transparent',
    grid: {left: 12, right: 176, top: 14, bottom: 26},
    tooltip: {trigger: 'axis', backgroundColor: '#262626', borderColor: '#353535', textStyle: {color: '#edf2f0', fontSize: 12},
      formatter: ps => ps[0].axisValue + '<br>' + ps.filter(p => p.seriesName.indexOf('지수') >= 0 || p.seriesName.indexOf('신용') >= 0)
        .map(p => p.marker + p.seriesName + ' <b>' + (p.seriesName.indexOf('지수') >= 0 ? p.value[1].toLocaleString() : fmt(p.value[1], view)) + '</b>').join('<br>')},
    xAxis: {type: 'time', axisLine: {lineStyle: {color: '#353535'}}, axisLabel: {color: COLORS.text, fontSize: 11,
      formatter: {year: '{yyyy}년', month: '{M}월', day: '{M}/{d}'}}, splitLine: {show: false}},
    yAxis: [
      {type: 'value', position: 'right', min: +(lo - pad).toFixed(4), max: +(hi + pad).toFixed(4), splitLine: {lineStyle: {color: COLORS.grid}},
       axisLabel: {color: COLORS.text, fontSize: 10, formatter: v => fmt(v, view)}},
      {type: 'value', position: 'right', offset: 56, scale: true, splitLine: {show: false},
       axisLabel: {color: COLORS.index, fontSize: 10, formatter: v => Math.round(v).toLocaleString()}},
    ],
    series: [
      {name: panel.name + ' 지수', type: 'line', yAxisIndex: 1, data: panel.index, showSymbol: false, z: 1,
       lineStyle: {color: COLORS.index, width: 1.2, opacity: 0.6},
       markPoint: {symbol: 'roundRect', symbolSize: [52, 18], symbolOffset: [146, 0], data: lastIdx ? [{coord: lastIdx, value: Math.round(lastIdx[1]).toLocaleString()}] : [],
         itemStyle: {color: COLORS.index}, label: {color: '#091018', fontSize: 11, fontWeight: 'bold'}}},
      {name: panel.name + ' 신용', type: 'line', data: view.line, showSymbol: false, z: 3, lineStyle: {color: color, width: 2},
       markArea: {silent: true, data: panel.shade.map(s => [{xAxis: s.from, itemStyle: {color: s.kind === 'up' ? COLORS.up : COLORS.down}}, {xAxis: s.to}])},
       markPoint: {data: [
         ...view.labels.map(p => ({coord: p, value: fmt(p[1], view), symbol: 'circle', symbolSize: 1,
           label: {show: true, color: color, fontWeight: 'bold', fontSize: 11, position: 'top', distance: 6}})),
         {coord: view.peak, symbol: 'circle', symbolSize: 18, itemStyle: {color: 'transparent', borderColor: color, borderWidth: 2},
          label: {show: false}},
         {coord: last, value: fmt(last[1], view), symbol: 'roundRect', symbolSize: [50, 18], symbolOffset: [30, 0],
          itemStyle: {color: color}, label: {show: true, color: '#091018', fontWeight: 'bold', fontSize: 11}},
       ]}},
      {name: '급락일', type: 'scatter', data: view.crash, symbol: 'diamond', symbolSize: 10, z: 5, itemStyle: {color: COLORS.crash, borderColor: '#050505', borderWidth: 1}},
      {name: '신용 급감', type: 'scatter', data: view.drops, symbol: 'triangle', symbolRotate: 180, symbolSize: 9, symbolOffset: [0, 10], z: 5,
       itemStyle: {color: COLORS.drop}},
    ],
  };
}
const creditCharts = [];
function drawCredit() {
  if (creditCharts.length) return;
  if (typeof echarts === 'undefined') { chartUnavailable('t4'); return; }
  document.querySelectorAll('[data-credit]').forEach(el => {
    const [i, kind] = el.dataset.credit.split(':');
    const panel = CREDIT.panels[+i];
    if (!panel[kind]) return;
    const chart = echarts.init(el, null, {renderer: 'canvas'});
    chart.setOption(creditOption(panel, kind));
    chart.group = 'credit-' + kind;
    creditCharts.push(chart);
  });
  echarts.connect('credit-amount'); echarts.connect('credit-ratio');
}
window.addEventListener('resize', () => creditCharts.forEach(c => c.resize()));
"""


def render_credit(data):
    """신용잔고 탭: 머리말 + 금액 차트 2개 + 시가총액 대비 비율 차트 2개."""
    if data is None:
        return '<p class="note">신용잔고 데이터를 받지 못했습니다.</p>'
    r = data["rules"]
    legend = (
        '<div class="clegend"><span><i style="background:#49a5e8"></i>코스피 신용</span>'
        '<span><i style="background:#ffad33"></i>코스닥 신용</span><span><i style="background:#81939b"></i>지수</span>'
        f'<span><b style="color:#ff7770">◆</b> 급락일 (지수 {r["crash"]:.0f}%↓)</span>'
        f'<span><b style="color:#edf2f0">▼</b> 신용 급감 ({r["drop"]:.0f}%↓)</span><span>◯ 신용 꼭지점</span>'
        f'<span><i class="sq" style="background:rgba(255,119,112,.5)"></i>빨리 쌓임</span>'
        f'<span><i class="sq" style="background:rgba(98,213,154,.45)"></i>빨리 줄어듦</span></div>')
    heads = "".join(
        f'<div class="chead"><span class="cdate">{p["asof"][5:].replace("-", ".")}</span>{esc(p["headline"])}</div>'
        for p in data["panels"])
    notes = "".join(f'<p class="note">{esc(n)}</p>' for n in data["notes"])

    def block(kind):
        return "".join(
            f'<div class="cpanel"><div class="cname">{esc(p["name"])}</div><div class="cchart" data-credit="{i}:{kind}"></div></div>'
            if p[kind] else f'<p class="note">{esc(p["name"])} 시가총액 데이터가 없어 비율을 그리지 못했습니다.</p>'
            for i, p in enumerate(data["panels"]))

    return (f'{notes}<div class="card">{heads}{legend}{block("amount")}</div>'
            f'<div class="card"><div class="sec-head"><span class="sec-title">시가총액 대비 비율</span>'
            f'<span>신용거래융자 ÷ 상장 시가총액 · 시장이 커지면 금액은 저절로 늘어나므로 비율로 본 부담</span></div>{legend}{block("ratio")}'
            f'<div class="legend">※ 빨리 쌓임/줄어듦: 20거래일 신용 증감률이 2010년 이후 상·하위 {r["fast"]}%인 구간 · '
            f'출처: 금융투자협회(신용), 한국은행 ECOS(코스피 시총), KRX(코스닥 시총·지수)</div></div>')


STOCK_JS = r"""
// TradingView Lightweight Charts. TradingView 위젯은 KRX 종목을 막아 두어, 같은 엔진에 시세를 직접 넣고
// IV Terminal의 TradingView 카드 모양(툴바 · 정보줄 · 기간 버튼 · 시계)을 따라 만든다.
const STOCKS = __DATA__;
const TV = {};
const UP = '#26a69a', DOWN = '#ef5350';  // TradingView 기본 색 (IV Terminal 차트와 같음)
// 가격 눈금: 원 단위 정수, 거래량 공간으로 내려간 0 이하 눈금은 숨긴다.
const PRICE_FMT = {type: 'custom', minMove: 1, formatter: p => p > 0 ? Math.round(p).toLocaleString() : ''};
const MA = [[5, '#edf2f0'], [20, '#ffad33'], [60, '#49a5e8'], [120, '#62d59a'], [240, '#c39bff']];
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
const RSI_N = 14, RSI_HI = 70, RSI_LO = 30, RSI_COLOR = '#c39bff';
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
  const tag = v === null ? '' : v >= RSI_HI ? ' <b style="color:#ffbf59">과매수</b>' : v <= RSI_LO ? ' <b style="color:#49a5e8">과매도</b>' : ' <b>중립</b>';
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
    [[RSI_HI, '#ffbf59', '과매수'], [50, '#5d686d', ''], [RSI_LO, '#49a5e8', '과매도']].forEach(([price, color, title]) =>
      t.rsiLine.createPriceLine({price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: !!title, title}));
    rsiLegend(id, rows.length - 1);
  }
  showLegend(id, rows.length - 1);
}

function showLegend(id, i) {
  const t = TV[id], r = t.rows[i], prev = i ? t.rows[i - 1][4] : r[1], diff = r[4] - prev, pct = diff / prev * 100;
  const c = diff >= 0 ? UP : DOWN, f = v => v.toLocaleString(), sign = diff >= 0 ? '+' : '';
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
    const gap = (r[4] - v) / v * 100, gc = gap >= 0 ? UP : DOWN;
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
    layout: {background: {color: '#1f1f1f'}, textColor: '#b3c0c5', fontSize: 11},
    grid: {vertLines: {color: '#2a2a2a'}, horzLines: {color: '#2a2a2a'}},
    rightPriceScale: {borderColor: '#353535'}, timeScale: {borderColor: '#353535', rightOffset: 6},
    crosshair: {mode: LightweightCharts.CrosshairMode.Normal},
    localization: {locale: 'ko-KR', dateFormat: 'yyyy-MM-dd'},
  });
  TV[id] = {s, chart, iv: 'D', ma: Object.fromEntries(MA.map(([n]) => [n, true])), range: '1Y'};
  // RSI 보조 차트: 주가 차트와 가격 눈금 폭을 같게 해 봉 위치를 맞추고, 보이는 구간을 서로 따라가게 한다.
  const rsiEl = document.getElementById('rsi-' + id);
  if (rsiEl) {
    const rsiChart = LightweightCharts.createChart(rsiEl, {
      autoSize: true,
      layout: {background: {color: '#1f1f1f'}, textColor: '#b3c0c5', fontSize: 11},
      grid: {vertLines: {color: '#2a2a2a'}, horzLines: {visible: false}},
      rightPriceScale: {borderColor: '#353535', minimumWidth: 80, scaleMargins: {top: 0.1, bottom: 0.1}},
      timeScale: {borderColor: '#353535', rightOffset: 6, visible: false},
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
  tick(); setInterval(tick, 1000);
  drawFlow(id);
  drawFcf(id);
}

// 투자자별 매매 동향 (ECharts): 주체 · 누적/일별 · 단위(주/억원) · 기간(1/3/6개월)을 버튼으로 바꾼다.
const WHO = [['외국인', '#49a5e8'], ['기관', '#ffad33'], ['개인', '#b3c0c5']];
const FLOW = {};
const FLOW_DAYS = {'30': 30, '60': 60, '120': 120};  // 거래일
function flowSet(id, key, value) { const f = FLOW[id]; if (!f) return; f[key] = value; renderFlow(id); }
function renderFlow(id) {
  const f = FLOW[id], d = f.d, n = d.dates.length, start = Math.max(0, n - FLOW_DAYS[f.p]);
  const dates = d.dates.slice(start), src = d[f.unit], unit = f.unit === 'volume' ? '주' : '억원';
  const fmt = v => Math.round(v).toLocaleString();
  const series = WHO.filter(([k]) => f.who === 'all' || f.who === k).map(([k, color]) => {
    let vals = src[k].slice(start);
    if (f.mode === 'cum') { let s = 0; vals = vals.map(v => (s += v)); }
    return f.mode === 'cum'
      ? {name: k, type: 'line', data: vals, showSymbol: false, lineStyle: {color, width: 2}, itemStyle: {color}}
      : {name: k, type: 'bar', data: vals, itemStyle: {color}, barMaxWidth: 10};
  });
  series[0].markLine = {symbol: 'none', silent: true, label: {show: false}, lineStyle: {color: '#81939b', type: 'dashed'}, data: [{yAxis: 0}]};
  const bal = (d.short_bal || []).slice(start);
  if (bal.some(v => v !== null))
    series.push({name: '공매도 잔고 비중', type: 'line', yAxisIndex: 1, data: bal, connectNulls: true, showSymbol: false,
      lineStyle: {color: '#c39bff', width: 1.5, type: 'dashed'}, itemStyle: {color: '#c39bff'}});
  f.chart.setOption({
    animation: false, backgroundColor: 'transparent',
    grid: {left: 62, right: 50, top: 28, bottom: 28},
    tooltip: {trigger: 'axis', backgroundColor: '#262626', borderColor: '#353535', textStyle: {color: '#edf2f0', fontSize: 12},
      formatter: ps => ps[0].axisValue + '<br>' + ps.map(p => p.marker + p.seriesName + ' <b>' + (p.seriesName === '공매도 잔고 비중'
        ? (p.value === null || p.value === undefined ? '—' : p.value.toFixed(3) + '%') : (p.value > 0 ? '+' : '') + fmt(p.value) + unit) + '</b>').join('<br>')},
    xAxis: {type: 'category', data: dates, boundaryGap: f.mode === 'day', axisLine: {lineStyle: {color: '#353535'}}, axisTick: {show: false},
      axisLabel: {color: '#81939b', fontSize: 11, formatter: v => v.slice(5).replace('-', '.')}},
    yAxis: [{type: 'value', position: 'left', name: '(' + unit + ')', nameTextStyle: {color: '#81939b', fontSize: 11},
      splitLine: {lineStyle: {color: '#2c2c2c'}}, axisLabel: {color: '#81939b', fontSize: 10, formatter: fmt}},
      {type: 'value', position: 'right', name: '잔고 비중(%)', nameTextStyle: {color: '#81939b', fontSize: 11}, scale: true,
      splitLine: {show: false}, axisLabel: {color: '#c39bff', fontSize: 10, formatter: v => v.toFixed(2)}}],
    series,
  }, true);
  document.querySelectorAll('[data-flow-for="' + id + '"]').forEach(b => b.classList.toggle('on', f[b.dataset.k] === b.dataset.v));
}
function drawFlow(id) {
  if (FLOW[id] || typeof echarts === 'undefined') return;
  const s = STOCKS.find(x => x.id === id), el = document.getElementById('flowchart-' + id);
  if (!s || !el || !s.flow || !s.flow.daily) return;
  FLOW[id] = {d: s.flow.daily, who: 'all', mode: 'cum', unit: 'value', p: '120', chart: echarts.init(el, null, {renderer: 'canvas'})};
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
      backgroundColor: '#262626', borderColor: '#353535', textStyle: {color: '#edf2f0', fontSize: 12},
      formatter: ps => ps[0].name + '<br>FCF <b>' + krw(ps[0].value, 1) + '원</b>'},
    xAxis: {type: 'category', data: pts.map(p => p[0]), axisLine: {lineStyle: {color: '#353535'}}, axisTick: {show: false},
      axisLabel: {color: '#81939b', fontSize: 10}},
    yAxis: {type: 'value', position: 'right', splitLine: {lineStyle: {color: '#2c2c2c'}}, axisLabel: {color: '#81939b', fontSize: 10, formatter: v => krw(v, 0)}},
    series: [{type: 'bar', barMaxWidth: m === 'annual' ? 60 : 18, data: pts.map(p => ({value: p[1], itemStyle: {color: p[1] >= 0 ? '#8fa3bd' : '#ff7770', borderRadius: p[1] >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2]}})),
      markLine: {symbol: 'none', silent: true, label: {show: false}, lineStyle: {color: '#81939b', type: 'solid', width: 1}, data: [{yAxis: 0}]}}],
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
    """수급 카드: 왼쪽 투자자별 순매수 차트(+ 공매도 잔고 비중), 오른쪽 기간별 누적 순매수·시가총액 대비·공매도 표."""
    def group(key, options):
        return '<div class="igroup">' + "".join(
            f'<button data-flow-for="{i}" data-k="{key}" data-v="{v}" onclick="flowSet(\'{i}\', \'{key}\', \'{v}\')">{label}</button>'
            for v, label in options) + "</div>"

    legend = "".join(f'<span><i style="background:{color}"></i>{name}</span>'
                     for name, color in [("외국인", "#49a5e8"), ("기관", "#ffad33"), ("개인", "#b3c0c5")])
    legend += '<span><i class="idash"></i>공매도 잔고 비중 (우)</span>'
    left = ('<div class="qleft"><div class="ilegend">' + legend + '</div><div class="ictrl">'
            + group("who", [("all", "전체"), ("외국인", "외국인"), ("기관", "기관"), ("개인", "개인")])
            + group("mode", [("cum", "누적"), ("day", "일별")])
            + group("unit", [("value", "금액(억원)"), ("volume", "수량(주)")])
            + group("p", [("30", "30일"), ("60", "60일"), ("120", "120일")])
            + f'</div><div id="flowchart-{i}" class="ichart"></div></div>')

    def tone(v):  # 한국식: 순매수·증가 빨강, 순매도·감소 파랑
        return "up" if v > 0 else ("down" if v < 0 else "")

    def num(v, fmt):
        return '<td class="muted">—</td>' if v is None else f'<td class="{tone(v)}">{fmt(v)}</td>'

    head = "".join(f"<th>{n}D</th>" for n in FLOW_PERIODS)
    rows = ""
    for name in ["외국인", "기관", "개인"]:
        v = f["rows"][name]
        rows += (f'<tr><th>{name}</th>'
                 + "".join(num(v[n] and v[n]["won"] / 1e8, lambda x: f"{x:+,.0f}") for n in FLOW_PERIODS)
                 + "".join(num(v[n] and v[n]["pct"], lambda x: f"{x:+.2f}%") for n in FLOW_PERIODS) + "</tr>")
    flows = (f'<table class="qtab"><thead><tr><th></th><th colspan="{len(FLOW_PERIODS)}" class="qgrp">누적 순매수(억원)</th>'
             f'<th colspan="{len(FLOW_PERIODS)}" class="qgrp">÷ 시총(%)</th></tr>'
             f'<tr><th>투자자</th>{head}{head}</tr></thead><tbody>{rows}</tbody></table>')

    later = [n for n in FLOW_PERIODS if n > 1]
    sb, st = f["short_bal"], f["short_trade"]
    srows = ""
    if sb:
        srows += (f'<tr><th>잔고비중 · Δ<small>{sb["date"]} 기준</small></th><td class="qnow">{sb["now"]:.3f}%</td>'
                  + "".join(num(sb["chg"][n], lambda x: f"{x:+.3f}%p") for n in later) + "</tr>")
    srows += (f'<tr><th>거래비중 · 평균<small>거래량 대비</small></th>'
              f'<td class="qnow">{"—" if st[1] is None else f"{st[1]:.2f}%"}</td>'
              + "".join('<td>—</td>' if st[n] is None else f'<td>{st[n]:.2f}%</td>' for n in later) + "</tr>")
    shorts = (f'<table class="qtab qshort"><thead><tr><th>공매도(%)</th><th>현재</th>'
              f'{"".join(f"<th>{n}D</th>" for n in later)}</tr></thead><tbody>{srows}</tbody></table>')

    streak = f'{f["streak"]}일 연속 {"순매수" if f["streak_buy"] else "순매도"}'
    persist = (f'<p class="qnote">매집 지속성 (기관+외국인) · 최근 30일 중 순매수 <b>{f["buy_days"]}일</b> '
               f'({f["buy_days"] / 30 * 100:.0f}%) · {streak}</p>')
    right = f'<div class="qright"><div class="scroll">{flows}</div><div class="scroll">{shorts}</div>{persist}</div>'
    return (f'<div class="card"><div class="qhead"><span class="sec-title">수급</span>'
            f'<span>외국인 · 기관 누적 순매수 · 공매도 · {f["date"]} 기준</span></div>'
            f'<div class="qsplit">{left}{right}</div>'
            f'<div class="legend">순매수 = 매수 − 매도 · D는 거래일 · 누적은 기간 첫날부터 더한 값 · 시총 대비는 현재 시가총액 기준 · '
            f'공매도 잔고 비중 = 공매도 잔고 ÷ 상장주식수 (2거래일 늦게 공시) · 거래비중의 현재는 당일 값, 나머지는 기간 평균 · '
            f'출처: KRX 투자자별 거래실적 · 공매도 (넣어 주신 KRX 로그인 사용) · 종목별 프로그램 매매는 출처를 찾지 못해 넣지 않았습니다</div></div>')


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
        return f'<div class="card">{head}<p class="note">{esc(c.get("reports_note") or "리포트가 없습니다.")}</p></div>'
    note = '<p class="note">리포트 목록을 받지 못해 마지막 저장본을 보여 줍니다.</p>' if r["cached"] else ""

    def up(v):
        return f'<span class="{"good" if v >= 0 else "bad"}">{v:+.1f}%</span>'

    if r["avg"]:
        avg_up = (r["avg"] / r["price"] - 1) * 100
        v = r["views"]
        kpis = [
            ("평균 목표가", f'{r["avg"]:,.0f}원', f'현재가 대비 {up(avg_up)} · 중간값 {r["median"]:,.0f}원'),
            ("최고 목표가", f'{r["high"]["target"]:,.0f}원', f'{esc(r["high"]["broker"])} · {up(r["high"]["upside"])}'),
            ("최저 목표가", f'{r["low"]["target"]:,.0f}원', f'{esc(r["low"]["broker"])} · {up(r["low"]["upside"])}'),
            ("투자의견", f'매수 {v["매수"]}', f'중립 {v["중립"]} · 매도 {v["매도"]} · 증권사 {r["n_brokers"]}곳'),
        ]
        tiles = "".join(f'<div class="fkpi"><small>{a}</small><b>{b}</b><small>{s}</small></div>' for a, b, s in kpis)
        summary = (f'<div class="fkpis">{tiles}</div><div class="legend" style="margin:6px 0 14px">'
                   f'최근 {REPORT_DAYS}일 안에 목표가를 낸 증권사 {r["n_brokers"]}곳의 최신 목표가 기준 · 이 기간 리포트 {r["n_reports"]}건 · '
                   f'현재가 {r["price"]:,.0f}원</div>')
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


def render_rsi_badge(c):
    label, cls = rsi_zone(c["rsi"])
    return (f'<div class="srsi {cls}"><small>RSI({RSI_N}) · 일봉</small><b>{c["rsi"]:.1f}</b><span>{label}</span>'
            f'<small>1주 전 {c["rsi_prev"]:.1f} · {RSI_HI} 이상 과매수 · {RSI_LO} 이하 과매도</small></div>')


def render_company(c):
    """삼성전자·하이닉스 탭: 기업 개요 카드 + 일봉 차트."""
    if c.get("error"):
        return f'<p class="note">{esc(c["name"])}: {esc(c["error"])}</p>'
    chg = (c["price"] / c["prev"] - 1) * 100
    tone = "var(--bad)" if chg > 0 else ("var(--series-1)" if chg < 0 else "var(--mu)")  # 한국식 색 (상승 빨강)
    flow_card = render_flow(c["flow"], c["id"]) if c["flow"] else ""
    fcf_card = render_fcf(c)
    note = f'<p class="note">{esc(c["note"])}</p>' if c["note"] else ""
    i = c["id"]
    ranges = "".join(f'<button data-range-for="{i}" data-r="{r}" onclick="tvRange(\'{i}\', \'{r}\')">{r}</button>'
                     for r in ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"])
    intervals = "".join(f'<button data-iv-for="{i}" data-iv="{v}" onclick="tvInterval(\'{i}\', \'{v}\')">{label}</button>'
                        for v, label in [("D", "일"), ("W", "주"), ("M", "월")])
    mas = "".join(f'<button class="mabtn on" data-ma-for="{i}" data-n="{n}" onclick="tvMA(\'{i}\', {n})">'
                  f'<i style="background:{color}"></i>{n}</button>'
                  for n, color in [(5, "#edf2f0"), (20, "#ffad33"), (60, "#49a5e8"), (120, "#62d59a"), (240, "#c39bff")])
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
        f'<small>{esc(c["eng"])} · KRX 업종 {esc(c["sector"])}</small></div>'
        f'<div class="sright"><div class="sprice"><small>현재가</small><b>{c["price"]:,.0f}원</b>'
        f'<span style="color:{tone}">{c["price"] - c["prev"]:+,.0f} ({chg:+.2f}%)</span><small>종가 기준일 {c["date"]}</small></div>'
        f'{render_rsi_badge(c)}</div></div>'
        f'{note}{chart_card}{flow_card}{fcf_card}')


EXPORT_MONTHS = 36

EXPORT_JS = r"""
const EXPORTS = __DATA__;
let exportChart = null;
function drawExports() {
  if (exportChart) return;
  if (typeof echarts === 'undefined') { chartUnavailable('t5'); return; }
  const el = document.getElementById('export-chart');
  if (!el) return;
  const d = EXPORTS, last = d.months.length - 1;
  exportChart = echarts.init(el, null, {renderer: 'canvas'});
  exportChart.setOption({
    animation: false, backgroundColor: 'transparent',
    grid: {left: 52, right: 56, top: 30, bottom: 28},
    legend: {top: 0, right: 0, textStyle: {color: '#b3c0c5', fontSize: 12}, itemWidth: 12, itemHeight: 10,
             data: ['반도체 (집적회로)', '그 밖', '반도체 비중']},
    tooltip: {trigger: 'axis', backgroundColor: '#262626', borderColor: '#353535', textStyle: {color: '#edf2f0', fontSize: 12},
      formatter: ps => ps[0].axisValue + '<br>' + ps.map(p => p.marker + p.seriesName + ' <b>' +
        (p.seriesName === '반도체 비중' ? p.value.toFixed(1) + '%' : Math.round(p.value).toLocaleString() + '억$') + '</b>').join('<br>')
        + '<br>합계 <b>' + Math.round(d.total[ps[0].dataIndex]).toLocaleString() + '억$</b>'},
    xAxis: {type: 'category', data: d.months, axisLine: {lineStyle: {color: '#353535'}}, axisTick: {show: false},
      axisLabel: {color: '#81939b', fontSize: 11, interval: 0, formatter: m => m.endsWith('-01') ? m.slice(0, 4) + '년' : (m.endsWith('-07') ? '7월' : '')}},
    yAxis: [
      {type: 'value', splitLine: {lineStyle: {color: '#2c2c2c'}}, axisLabel: {color: '#81939b', fontSize: 10, formatter: v => v.toLocaleString()}},
      {type: 'value', position: 'right', min: 0, max: 60, splitLine: {show: false},
       axisLabel: {color: '#ffad33', fontSize: 10, formatter: v => v + '%'}},
    ],
    series: [
      {name: '반도체 (집적회로)', type: 'bar', stack: 'x', data: d.semis, barWidth: '62%', itemStyle: {color: '#49a5e8'}},
      {name: '그 밖', type: 'bar', stack: 'x', data: d.others, itemStyle: {color: '#3a3a3a', borderRadius: [3, 3, 0, 0]},
       markPoint: {symbol: 'roundRect', symbolSize: [64, 20], symbolOffset: [0, -16],
         data: [{coord: [last, d.total[last]], value: Math.round(d.total[last]).toLocaleString() + '억$'}],
         itemStyle: {color: '#ffad33'}, label: {color: '#091018', fontWeight: 'bold', fontSize: 11}}},
      {name: '반도체 비중', type: 'line', yAxisIndex: 1, data: d.share, showSymbol: false, smooth: true,
       lineStyle: {color: '#ffad33', width: 2}, itemStyle: {color: '#ffad33'}},
    ],
  });
}
window.addEventListener('resize', () => exportChart && exportChart.resize());
"""


def export_data():
    """수출 탭 데이터 (억 달러)."""
    df = customs_exports() / 1e8
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
    return {
        "month": last, "months": list(view.index),
        "total": [round(v, 1) for v in view["total"]],
        "semis": [round(v, 1) for v in view["semis"]],
        "others": [round(v, 1) for v in view["total"] - view["semis"]],
        "share": [round(v, 1) for v in view["semis"] / view["total"] * 100],
        "total_last": df.at[last, "total"], "total_y1": change("total", year_ago), "total_m1": change("total", month_ago),
        "semis_last": df.at[last, "semis"], "semis_y1": change("semis", year_ago), "semis_m1": change("semis", month_ago),
        "share_last": df.at[last, "semis"] / df.at[last, "total"] * 100,
        "items": items,
    }


def render_exports(data, note=None):
    if data is None:
        return f'<p class="note">{esc(note or "수출 데이터를 받지 못했습니다.")}</p>'
    month = int(data["month"][5:])
    pct = lambda v: "–" if v is None else ("0%" if round(v) == 0 else f"{v:+.0f}%")
    tone = lambda v: "var(--mu)" if v is None or round(v) == 0 else ("var(--good)" if v > 0 else "var(--bad)")
    head = (f'<div class="chead"><span class="cdate">{data["month"].replace("-", ".")}</span>'
            f'{month}월 수출 {data["total_last"]:,.0f}억 달러 · 1년 전 대비 '
            f'<span style="color:{tone(data["total_y1"])}">{pct(data["total_y1"])}</span>'
            f'<small class="hm1"> · 전월 대비 <span style="color:{tone(data["total_m1"])}">{pct(data["total_m1"])}</span></small></div>'
            f'<div class="csub">반도체(집적회로) {data["semis_last"]:,.0f}억 달러 · 1년 전 대비 '
            f'<b style="color:{tone(data["semis_y1"])}">{pct(data["semis_y1"])}</b> · 전월 대비 '
            f'<b style="color:{tone(data["semis_m1"])}">{pct(data["semis_m1"])}</b> · 전체 수출의 {data["share_last"]:.0f}%</div>')

    grown = [i for i in data["items"] if i["y1"] is not None and round(i["y1"]) > 0]
    top = data["items"][0] if data["items"] else {"name": "비교 가능한 품목 없음", "y1": None}
    item_head = (f'<div class="chead">{len(data["items"])}개 품목 중 {len(grown)}개가 1년 전보다 늘었고, '
                 f'증가율 1위: {esc(top["name"])} ({pct(top["y1"])})</div>')
    cards = ""
    for item in data["items"]:
        peak = max(item["bars"]) or 1
        bars = "".join(f'<i style="height:{max(v / peak * 100, 1.5):.1f}%"></i>' for v in item["bars"])
        value = f'{item["value"]:,.1f}' if item["value"] < 100 else f'{item["value"]:,.0f}'
        cards += (f'<div class="xcard"><div class="xname">{esc(item["name"])} <small>HS {", ".join(item["hs"])}</small></div>'
                  f'<div class="xval">{value}<small> 억 달러</small></div>'
                  f'<div class="xchg"><span>전월<b style="color:{tone(item["m1"])}">{pct(item["m1"])}</b></span>'
                  f'<span>3개월 전<b style="color:{tone(item["m3"])}">{pct(item["m3"])}</b></span>'
                  f'<span>1년 전<b style="color:{tone(item["y1"])}">{pct(item["y1"])}</b></span></div>'
                  f'<div class="xbars">{bars}</div>'
                  f'<div class="xaxis"><span>{data["months"][0].replace("-", ".")}</span><span>{data["month"].replace("-", ".")}</span></div></div>')
    return (f'<div class="card">{head}<div id="export-chart" class="xchart"></div>'
            f'<div class="legend">※ 관세청 품목별 수출입실적 (공공데이터포털) · FOB 달러 · 매월 15일경 전월분 발표 · '
            f'반도체 = 집적회로(HS 8542) 전체 · SSD는 관세청 분류상 저장매체(HS 8523)라 아래 품목에서 따로 봅니다</div></div>'
            f'<div class="card">{item_head}<div class="csub">{month}월 수출 · 막대 = 최근 {len(data["months"])}개월 · 1년 전 대비 증가율 순</div>'
            f'<div class="xgrid">{cards}</div></div>')


def _kst(iso):
    return pd.Timestamp(iso).tz_convert(KST).strftime("%m-%d %H:%M") if iso else ""


def render_news(ctx):
    digest = ctx["digest"]
    summaries = digest.get("summaries", {}) if digest else {}
    top = ""
    if digest:
        items = "".join(f"<li>{esc(h)}</li>" for h in digest["highlights"])
        top = (f'<div class="digest"><b>오늘의 핵심</b><ul>{items}</ul>'
               f'<small>AI가 기사 제목만 보고 쓴 요약입니다. 정확한 내용은 원문을 확인하세요 · {esc(digest["model"])}</small></div>')
    elif ctx["digest_note"]:
        top = f'<p class="note">{esc(ctx["digest_note"])}</p>'
    top += "".join(f'<p class="note">{esc(e)}</p>' for e in ctx["errors"])

    options = '<option value="all">종목 전체</option>' + "".join(
        f'<option value="{t}">{esc(c["name"])}</option>' for t, c in news.COMPANIES.items())
    weekdays = "월화수목금토일"
    days = {}
    for s in ctx["stories"]:
        if s["published_at"]:
            days.setdefault(pd.Timestamp(s["published_at"]).tz_convert(KST).strftime("%Y-%m-%d"), []).append(s)
    groups = ""
    for day in sorted(days, reverse=True):
        rows = ""
        for s in sorted(days[day], key=lambda s: s["published_at"], reverse=True):
            tickers = s.get("tickers", [s["ticker"]])
            chips = '<i class="ndot">·</i>'.join(
                f'<span class="nchip" style="color:{news.COMPANIES[t]["color"]}">{esc(news.COMPANIES[t]["name"])}</span>' for t in tickers)
            body = summaries.get(s["id"])
            related = ""
            if s["related"]:
                links = "".join(
                    f'<li><a href="{esc(r["link"])}" target="_blank" rel="noopener noreferrer">{esc(r["title"])}</a>'
                    f'<span> · {esc(r["source"] or "")} · {_kst(r["published_at"])}</span></li>'
                    for r in s["related"])
                related = f'<details><summary>+{len(s["related"])}</summary><ul>{links}</ul></details>'
            search = " ".join([s["title"], s["source"] or ""] + [r["title"] + " " + (r["source"] or "") for r in s["related"]]).lower()
            rows += (
                f'<article class="nrow" data-tickers="{" ".join(tickers)}" data-day="{day}" data-q="{esc(search)}">'
                f'<span class="ntime">{pd.Timestamp(s["published_at"]).tz_convert(KST):%H:%M}</span>'
                f'<span class="nsrc">{esc(s["source"] or "기사")}</span>'
                f'<div class="nmain"><div class="nline">{chips}'
                f'<a class="ntitle" href="{esc(s["link"])}" target="_blank" rel="noopener noreferrer">{esc(s["title"])}</a></div>'
                f'{f"<p>{esc(body)}</p>" if body else ""}</div>'
                f'{related}<a class="ngo" href="{esc(s["link"])}" target="_blank" rel="noopener noreferrer" aria-label="원문 열기">↗</a></article>')
        wd = weekdays[pd.Timestamp(day).weekday()]
        groups += f'<section class="nday" data-day="{day}"><h3>{day} <small>({wd})</small></h3><div class="nbox">{rows}</div></section>'
    if not groups:
        groups = '<p class="note">주요 언론사 뉴스가 없습니다.</p>'
    months = sorted({d[:7] for d in days}, reverse=True)
    periods = [("7", "최근 7일"), ("30", "최근 30일")] + [(m, f"{int(m[:4])}년 {int(m[5:])}월") for m in months]
    on = ' class="on"'
    pbtn = "".join(f'<button data-p="{p}"{on if p == "7" else ""}>{label} <b></b></button>' for p, label in periods)
    today = datetime.now(ZoneInfo(KST)).strftime("%Y-%m-%d")
    return (f'{top}<div class="ntools"><select id="nco" aria-label="종목">{options}</select>'
            f'<input id="nq" type="search" placeholder="제목·언론사 검색..." aria-label="뉴스 검색"></div>'
            f'<div class="nperiod" data-today="{today}"><span>기간</span>{pbtn}</div>'
            f'<div class="nlist">{groups}<p class="note nempty" hidden>조건에 맞는 뉴스가 없습니다.</p></div>'
            f'<div class="legend">주요 언론사 기사만 · 같은 내용은 +N으로 묶음 · 받은 뉴스는 저장해 두어 지난달도 볼 수 있습니다 (저장 시작 이후부터)</div>')


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
        extra = [x for x in [f'직전 {e["prev"]}' if e["prev"] else None, f'예상 {e["expected"]}' if e["expected"] else None,
                             e["note"]] if x]
        rows += (f'<a class="evrow" href="{esc(e["url"])}" target="_blank" rel="noopener noreferrer" data-r="{e["region"]}" '
                 f'data-s="{e["stars"]}" data-at="{e["at"]}" data-known="{int(e["time_known"])}">'
                 f'<div class="evtop"><span class="evtime">{day} <b>{clock}</b></span><span class="evd"></span>'
                 f'<span class="evstars" aria-label="중요도 {e["stars"]}/3">{stars}</span></div>'
                 f'<div class="evtitle">{esc(e["title"])}</div>'
                 f'<div class="evsub"><span class="evreg {e["region"]}">{events.REGIONS[e["region"]]}</span>'
                 f'{"".join(f"<span>{esc(x)}</span>" for x in extra)}</div></a>')
    return (f'{notes}<div class="card"><div class="fhead"><span class="sec-title">다가오는 일정</span>'
            f'<span>한국 시각 (UTC+9) · 앞으로 {ev["horizon"]}일 · 받은 시각 {ev["today"]}</span></div>'
            f'<div class="evtools"><div class="evfilter">{buttons}</div>'
            f'<label class="evonly"><input type="checkbox" id="ev3"> ★★★만</label></div>'
            f'<div class="cal"><div class="calnav"><button id="calprev" aria-label="이전 달">‹</button><b id="calttl"></b>'
            f'<button id="calnext" aria-label="다음 달">›</button></div>'
            f'<div class="calhead">{"".join(f"<span>{d}</span>" for d in "일월화수목금토")}</div><div class="calgrid" id="calgrid"></div></div>'
            f'<div class="evday" id="evday" hidden></div><div class="evlist" hidden>{rows}</div>'
            f'<div class="legend">★★★ 시장을 크게 움직이는 일정 · ★★ 중요 · ★ 참고 · 날짜를 누르면 그날 일정(시각·직전 값)이 달력 아래에 나오고, 일정을 누르면 출처가 열립니다 · '
            f'출처: Yahoo 경제 일정표(지표 시각·직전 값) · 연준·한국은행·일본은행 공식 일정 · Yahoo 실적 예정일 · '
            f'옵션 만기·관세청 수출·보고서 기한은 규칙으로 계산 (공휴일은 반영하지 않고 주말만 다음 평일로)</div></div>')


def render_upcoming(ev, days=7, limit=5):
    """매크로 탭 한 줄: 앞으로 7일 안의 ★★ 이상 일정."""
    if not ev:
        return ""
    today = datetime.fromisoformat(ev["today"] + "T00:00:00+09:00")
    items = [e for e in ev["events"] if e["stars"] >= 2 and datetime.fromisoformat(e["at"]) < today + pd.Timedelta(days=days)][:limit]
    if not items:
        return ""
    parts = []
    for e in items:
        day, clock = _ev_when(e)
        parts.append(f'<span>{day[:5]} {clock if e["time_known"] else ""} <b>{esc(e["title"])}</b></span>')
    return f'<div class="upnext"><a href="#" onclick="document.querySelector(\'[data-t=t7]\').click();return false">다가오는 일정</a>{" · ".join(parts)}</div>'


def render_html(ctx):
    v = ctx["verdicts"]
    groups = CFG["groups"]
    status = " · ".join(f'{g["name"]} <b style="color:{TONE[v[g["id"]]["key"]]}">{v[g["id"]]["label"]}</b>' for g in groups)
    gcards = ""
    for n, g in enumerate(groups):
        gv = v[g["id"]]
        c = gv["counts"]
        gcards += (f'<div class="gcard" style="border-top-color:{TONE[gv["key"]]}"><small>{GROUP_NUM[n]} {g["name"]}</small>'
                   f'<div style="color:{TONE[gv["key"]]}">{gv["label"]}</div>'
                   f'<small>{g["labels"]["pos"]} {c["pos"]} · 중립 {c["neu"]} · {g["labels"]["neg"]} {c["neg"]}</small></div>')

    sections = ""
    for n, g in enumerate(groups):
        gv = v[g["id"]]
        c = gv["counts"]
        rows = "".join(render_row(r["ind"], r.get("res"), r.get("err"), r.get("note")) for r in ctx["rows"] if r["ind"]["group"] == g["id"])
        sections += (f'<div class="ghead" id="grp-{g["id"]}">{GROUP_NUM[n]} {g["name"]}'
                     f'<span class="pill" style="color:{TONE[gv["key"]]}">{gv["label"]}</span>'
                     f'<small>{g["labels"]["pos"]} {c["pos"]} · 중립 {c["neu"]} · {g["labels"]["neg"]} {c["neg"]}</small></div>{rows}')

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>국장 매크로 대시보드</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>국장 매크로 대시보드</h1>
<div class="meta">데이터 기준 {ctx["asof"]:%Y-%m-%d} · 생성 {ctx["generated"]:%Y-%m-%d %H:%M} · 판정 기준: indicators.json</div>
<div class="tabs"><button class="on" data-t="t1">매크로</button>{"".join(f'<button data-t="s-{c["id"]}">{esc(c["tab"])}</button>' for c in ctx["companies"])}<button data-t="t3">삼전닉스 뉴스</button><button data-t="t6">증권사 리포트</button><button data-t="t7">일정</button><button data-t="t4">신용잔고</button><button data-t="t5">수출</button><button data-t="t2">시장 흐름</button></div>

<div class="tab on" id="t1">
<div class="card status">
  <div><b class="big">오늘의 상태</b> · {status}</div>
  <div class="headline">{esc(ctx["sentence"])}</div>
  {ctx["upcoming_html"]}
  <div class="kpis">{ctx["kpis"]}</div>
  <div class="gcards">{gcards}</div>
  <div class="fresh">{ctx["fresh"]}</div>
</div>
<div class="card">
  <div class="sec-head"><span class="sec-title">핵심 지표 위치</span><span>현재 구간 · ★ 먼저 볼 지표</span></div>
  <div class="scroll"><div class="grid">{sections}</div></div>
  <div class="legend">※ <b>위치</b>: 기준 기간 중 지금 값보다 낮거나 같았던 날의 비율 (0% = 기간 중 최저, 100% = 최고)<br>
  ※ <b>구간 색</b>: 빨강 스트레스 · 주황 경계 · 노랑 주의 · 회색 중립 · 파랑 안정 · 초록 양호 · 진한 초록 과열 (빨강은 '매도'가 아니라 '스트레스가 크다'는 뜻)<br>
  ※ 시장 내부의 종목 비율은 코스피·코스닥 혼합 {ctx["uni_n"]}종목 표본 기준입니다.</div>
</div>
</div>

{"".join(f'<div class="tab" id="s-{c["id"]}">{render_company(c)}</div>' for c in ctx["companies"])}

<div class="tab" id="t7">
{ctx["events_html"]}
</div>
<div class="tab" id="t6">
{"".join(render_reports(c) for c in ctx["companies"] if not c.get("error"))}
</div>
<div class="tab" id="t3">
<div class="card">
  <div class="sec-head"><span class="sec-title">삼전닉스 뉴스</span><span>최근 7일 · 주요 언론사만 · 같은 내용은 하나로 묶음 (Google 뉴스)</span></div>
  {ctx["news_html"]}
</div>
</div>

<div class="tab" id="t4">
{ctx["credit_html"]}
</div>

<div class="tab" id="t5">
{ctx["export_html"]}
</div>

<div class="tab" id="t2">
<div class="card">
  <div class="sec-head"><span class="sec-title">기간별 수익률</span><span>각 자산의 최신 거래일 기준</span></div>
  <div class="scroll">{ctx["period_html"]}</div>
  <div class="legend">※ 1D 전 거래일 · 1W 7일 전 · 1M 1개월 전 · YTD 전년 말 · 1Y 1년 전 (휴장이면 직전 거래일) · 52주 종가 최고 대비 −5% 이내 흰색, −5~−15% 주황, −15% 초과 빨강</div>
</div>
<div class="card">
  <div class="sec-head"><span class="sec-title">섹터 흐름</span><span>월별 섹터 ETF 수익률 − {esc(CFG["sectors"]["benchmark"]["name"])} 수익률 (%p) · 초록 = 시장보다 강함 · 최근 3개월 합계 순 · * 진행 중인 달</span></div>
  <div class="scroll">{ctx["sector_html"]}</div>
</div>
</div>

</div>
<script src="{ECHARTS_URL}"></script>
<script src="{LWC_URL}"></script>
<script>{ctx["credit_js"]}</script>
<script>{ctx["export_js"]}</script>
<script>{ctx["stock_js"]}</script>
<script>
function chartUnavailable(id) {{
  const parent = document.getElementById(id);
  if (!parent || parent.querySelector('.chart-error')) return;
  const msg = document.createElement('p'); msg.className = 'note chart-error'; msg.setAttribute('role', 'status');
  msg.textContent = '차트 라이브러리를 불러오지 못했습니다. 인터넷 연결을 확인한 뒤 새로고침하세요. 저장된 수치는 계속 확인할 수 있습니다.';
  parent.prepend(msg);
}}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('.tabs button,.tab').forEach(e=>e.classList.remove('on'));
  b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');
  if (b.dataset.t === 't4' && typeof drawCredit === 'function') {{ drawCredit(); creditCharts.forEach(c => c.resize()); }}
  if (b.dataset.t === 't5' && typeof drawExports === 'function') {{ drawExports(); exportChart && exportChart.resize(); }}
  if (b.dataset.t.startsWith('s-') && typeof drawStock === 'function') {{
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
      const list = evs.slice(0, 3).map(e => '<div class="ci ' + e.r + '"><i></i><span>' + esc(e.title) + '</span></div>').join('');
      const more = evs.length > 3 ? '<div class="cmore">+' + (evs.length - 3) + '</div>' : '';
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
  apply();
}})();
(function () {{
  // 뉴스: 종목 · 검색어 · 기간을 함께 걸고, 기간 버튼 옆 건수는 종목·검색어 기준으로 다시 센다.
  const bar = document.querySelector('.nperiod');
  if (!bar) return;
  const rows = [...document.querySelectorAll('.nrow')], days = [...document.querySelectorAll('.nday')];
  // 날짜는 한국 날짜 문자열끼리 비교한다 (toISOString은 UTC라 하루가 밀린다).
  const [ty, tm, td] = bar.dataset.today.split('-').map(Number);
  const pad = v => String(v).padStart(2, '0');
  const since = n => {{ const d = new Date(ty, tm - 1, td - (n - 1)); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }};
  let period = '7';
  const inPeriod = (day, p) => p.length === 7 ? day.startsWith(p) : day >= since(+p);
  function apply() {{
    const co = document.getElementById('nco').value, q = document.getElementById('nq').value.trim().toLowerCase();
    const base = rows.filter(r => (co === 'all' || r.dataset.tickers.split(' ').includes(co)) && (!q || r.dataset.q.includes(q)));
    bar.querySelectorAll('button').forEach(b => {{ b.querySelector('b').textContent = base.filter(r => inPeriod(r.dataset.day, b.dataset.p)).length; }});
    rows.forEach(r => r.hidden = !(base.includes(r) && inPeriod(r.dataset.day, period)));
    days.forEach(d => d.hidden = !d.querySelector('.nrow:not([hidden])'));
    document.querySelector('.nempty').hidden = days.some(d => !d.hidden);
  }}
  bar.querySelectorAll('button').forEach(b => b.onclick = () => {{
    period = b.dataset.p; bar.querySelectorAll('button').forEach(e => e.classList.toggle('on', e === b)); apply();
  }});
  document.getElementById('nco').onchange = apply;
  document.getElementById('nq').oninput = apply;
  apply();
}})();
</script>
</body></html>"""


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
    sc = CFG["sectors"]
    spx = yahoo([sc["benchmark"]["ticker"]] + [i["ticker"] for i in sc["items"]],
                (TODAY - pd.DateOffset(months=sc["months"] + 2)).date())
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
    by_id = {r["ind"]["id"]: r.get("res") for r in rows}

    def from_high(t):
        s = px[t].dropna()
        return (s.iloc[-1] / s[s.index > s.index[-1] - pd.Timedelta(days=365)].max() - 1) * 100

    kp = [f"코스피 52주 종가 고점 대비 <b>{num(from_high('^KS11'), 1, '%', True)}</b>",
          f"코스닥 52주 종가 고점 대비 <b>{num(from_high('^KQ11'), 1, '%', True)}</b>"]
    for key, label, d, unit in [("breadth200", "200일선 위", 0, "%"), ("vix", "VIX", 1, ""), ("usdkrw", "원/달러", 1, "")]:
        if by_id.get(key):
            r = by_id[key]
            kp.append(f"{label} <b>{num(r['value'], d, unit)}</b> (1주 전 {num(r['prev'], d, unit)})")
    flow = []
    for key, label in [("foreign_net", "외국인 순매수"), ("credit", "신용잔고"), ("deposit", "고객예탁금"), ("cli", "경기선행지수")]:
        r = by_id.get(key)
        if r:
            ind = next(i for i in inds if i["id"] == key)
            color = COLORS[r["zone"]["color"]]
            flow.append(f'<a href="#ind-{key}">{label} <b>{num(r["value"], ind["decimals"], ind["unit"])}</b> '
                        f'<span style="color:{color}">{esc(r["zone"]["label"])}</span></a>')
    stale = [f'{r["ind"]["name"]} {r["res"]["date"]:%Y-%m}' for r in rows
             if r.get("res") and r["ind"]["freq"] != "daily"]
    pending = [r["ind"]["name"] for r in rows if not r.get("res")]
    fresh = f"데이터 기준 <b>{kospi.index[-1]:%Y-%m-%d}</b> (코스피)"
    if stale:
        fresh += " · 월간 최신: " + ", ".join(stale)
    if pending:
        fresh += " · <b>미연결: " + ", ".join(pending) + "</b>"
    delayed = [r["ind"]["name"] for r in rows if r.get("res", {}).get("stale")]
    if delayed:
        fresh += " · <b>갱신 지연 (판정 제외): " + ", ".join(delayed) + "</b>"

    try:
        sector, partial = sector_flow(spx)
        sector_html = render_sectors(sector, partial)
    except Exception as e:
        sector_html = f'<p class="note">섹터 데이터를 계산하지 못했습니다 ({e.__class__.__name__}).</p>'
    period = period_returns(px)
    news_ctx = collect_news()
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
    exports, export_note = None, None
    try:
        exports = export_data()
    except MissingKey as e:
        export_note = str(e)
    except Exception as e:
        export_note = f"수출 데이터를 받지 못했습니다 ({e.__class__.__name__})"
        print(f"  ! 수출 탭: {e.__class__.__name__}")
    try:
        credit = credit_charts(px)
    except Exception as e:  # 신용잔고 탭이 실패해도 나머지 대시보드는 만든다
        credit = None
        print(f"  ! 신용잔고 탭: {e.__class__.__name__}")
    ctx = {
        "rows": rows, "verdicts": verdicts, "sentence": summary_sentence(verdicts),
        "kpis": " · ".join(kp) + (f'<div class="flow">수급·자금 · {" · ".join(flow)}</div>' if flow else ""), "fresh": fresh, "asof": kospi.index[-1], "generated": datetime.now(ZoneInfo(KST)),
        "uni_n": uni.shape[1],
        "period_html": render_period(period), "sector_html": sector_html,
        "news_html": render_news(news_ctx),
        "events_html": render_events(ev), "upcoming_html": render_upcoming(ev),
        "credit_html": render_credit(credit),
        "export_html": render_exports(exports, export_note),
        "companies": companies,
        "stock_js": STOCK_JS.replace("__DATA__", json.dumps(companies, ensure_ascii=False, allow_nan=False).replace("</", r"<\/")) if companies else "",
        "export_js": EXPORT_JS.replace("__DATA__", json.dumps(exports, ensure_ascii=False, default=float, allow_nan=False).replace("</", r"<\/")) if exports else "",
        "credit_js": CREDIT_JS.replace("__DATA__", json.dumps(credit, ensure_ascii=False, allow_nan=False).replace("</", r"<\/")) if credit else "",
    }
    target = OUT / "dashboard.html"
    temporary = OUT / f".dashboard-{os.getpid()}.tmp"
    temporary.write_text(render_html(ctx), encoding="utf-8")
    temporary.replace(target)
    print(f"완료: {OUT / 'dashboard.html'}")
    print("요약:", ctx["sentence"])


if __name__ == "__main__":
    main()
