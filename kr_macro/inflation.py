"""US inflation readings and publication status."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from html import escape
import json
import re
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import pandas as pd

from company_catalog import write_atomic
from local_views import render_market_history


BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
FRED = "https://fred.stlouisfed.org/"
ET = ZoneInfo("America/New_York")
CALENDARS = {
    "cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "ppi": "https://www.bls.gov/schedule/news_release/ppi.htm",
    "pce": "https://www.bea.gov/news/schedule/full",
}
ATLANTA = "https://www.atlantafed.org/en/research-and-data/data/sticky-price-cpi"
DALLAS = "https://www.dallasfed.org/research/pce"
# An official snapshot is used when the BLS schedule endpoint is unavailable.
# These are release dates, starting with the October 2026 observation month.
BLS_DATES = {
    "cpi": (
        "2026-11-10 2026-12-10 2027-01-13 2027-02-11 2027-03-10 2027-04-13 2027-05-12 "
        "2027-06-10 2027-07-14 2027-08-11 2027-09-14 2027-10-14 2027-11-10 2027-12-10"
    ),
    "ppi": (
        "2026-11-13 2026-12-15 2027-01-14 2027-02-12 2027-03-12 2027-04-14 2027-05-13 "
        "2027-06-11 2027-07-15 2027-08-10 2027-09-15 2027-10-14 2027-11-12"
    ),
}

# FRED and BLS are equivalent seasonally adjusted series for the first three BLS indicators.
SERIES = (
    ("CPIAUCSL", "헤드라인 CPI 상승률", "cpi", "BLS", "CUSR0000SA0"),
    ("CPILFESL", "근원 CPI 상승률", "cpi", "BLS", "CUSR0000SA0L1E"),
    ("PCEPI", "헤드라인 PCE 상승률", "pce", "BEA", None),
    ("PCEPILFE", "근원 PCE 상승률", "pce", "BEA", None),
    ("PPIFES", "근원 PPI 상승률", "ppi", "BLS", "WPSFD49104"),
    ("CORESTICKM159SFRBATL", "애틀랜타 연은 Sticky CPI", "cpi", "애틀랜타 연은", None),
    ("PCETRIM12M159SFRBDAL", "댈러스 연은 절사평균 PCE", "pce", "댈러스 연은", None),
)
RATE_SERIES = {"CORESTICKM159SFRBATL", "PCETRIM12M159SFRBDAL"}
FED_TARGET = "https://www.federalreserve.gov/monetarypolicy/2026-07-mpr-statement.htm"
PCE_BAND = (1.5, 2.5)
# Rounded 25th and 75th percentiles of 2012–2025 year-over-year observations.
HISTORICAL_BANDS = {
    "CPIAUCSL": (1.52, 2.96), "CPILFESL": (1.81, 3.27),
    "PPIFES": (1.44, 2.91), "CORESTICKM159SFRBATL": (2.12, 3.31),
}
PRESSURE_LABELS = {1: "완화", 0: "중립", -1: "부담"}


def pressure(value, band):
    low, high = band
    return 1 if value < low else 0 if value < high else -1


def pressure_summary(readings):
    signals = [row["signal"] for row in readings if row.get("signal") is not None]
    score = sum(signals)
    label = PRESSURE_LABELS[(score > 0) - (score < 0)] if signals else "판정 보류"
    counts = " · ".join(f"{name} {signals.count(signal)}" for signal, name in PRESSURE_LABELS.items())
    return label, score, counts, f"{len(readings)}개 중 {len(signals)}개 반영"


def download(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urlopen(req, timeout=12) as response:
        return response.read()


def table_rows(document):
    soup = BeautifulSoup(document, "html.parser", from_encoding="utf-8")
    for row in soup.find_all("tr"):
        yield [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]


def read_bls(payload):
    response = json.loads(payload)
    if response.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS series request failed")
    series = {}
    for entry in response["Results"]["series"]:
        records = {
            f'{point["year"]}-{point["period"][1:]}-01': point["value"]
            for point in entry["data"] if re.fullmatch(r"M(0[1-9]|1[0-2])", point["period"])
        }
        series[entry["seriesID"]] = clean_series(records)
    return series


def read_fred(document):
    return clean_series({
        row[0]: row[1]
        for row in table_rows(document)
        if len(row) == 2 and re.fullmatch(r"\d{4}-\d{2}-01", row[0])
    })


def clean_series(records):
    values = pd.Series(records, dtype=object)
    values.index = pd.to_datetime(values.index)
    return pd.to_numeric(values, errors="coerce").dropna().sort_index()


def read_schedule(kind, document):
    schedule = {}
    for row in table_rows(document):
        if kind == "pce":
            prefix = "Personal Income and Outlays, "
            if len(row) < 3 or not row[2].startswith(prefix):
                continue
            reference = re.fullmatch(r"([A-Za-z]+(?: and [A-Za-z]+)?) (\d{4})", row[2].removeprefix(prefix))
            if not reference:
                continue
            months = [pd.Period(f"{month} {reference[2]}", freq="M") for month in reference[1].split(" and ")]
            latest = months[-1]
            stamp = pd.Timestamp(f"{row[0]} {latest.year}")
            if stamp.month < latest.month:
                stamp = stamp.replace(year=latest.year + 1)
        else:
            if len(row) != 3 or not re.fullmatch(r"[A-Za-z]+ \d{4}", row[0]):
                continue
            observed = pd.Period(row[0], freq="M")
            months = [observed]
            stamp = pd.Timestamp(f"{row[1]} {row[2]}")
        for month in months:
            schedule[str(month)] = stamp.to_pydatetime().replace(tzinfo=ET)
    if not schedule:
        raise ValueError("no releases found")
    return schedule


def initial_schedule(kind):
    dates = BLS_DATES.get(kind, "").split()
    entries = {
        str(period): datetime.combine(datetime.fromisoformat(day), time(8, 30), ET)
        for period, day in zip(pd.period_range("2026-10", periods=len(dates), freq="M"), dates, strict=True)
    }
    if kind == "pce":
        entries["2026-09"] = datetime(2026, 10, 29, 8, 30, tzinfo=ET)
        entries["2026-10"] = datetime(2026, 11, 25, 8, 30, tzinfo=ET)
        entries["2026-11"] = datetime(2026, 12, 23, 8, 30, tzinfo=ET)
    return entries


def source_cache(out, filename, refresh):
    path = out / filename
    try:
        data = refresh()
        if data:
            write_atomic(path, data)
            return data
    except (OSError, ValueError, KeyError) as error:
        print(f"  ! 물가 {filename}: {type(error).__name__} · 저장 자료 확인")
    return path.read_text(encoding="utf-8") if path.is_file() else None


def load_schedule(out, kind):
    def refresh():
        schedule = read_schedule(kind, download(CALENDARS[kind]))
        return json.dumps({month: date.isoformat() for month, date in schedule.items()})

    data = source_cache(out, f".inflation-release-{kind}.json", refresh)
    entries = initial_schedule(kind)
    if data:
        entries.update({month: datetime.fromisoformat(date) for month, date in json.loads(data).items()})
    return entries


def load_bls(out, year):
    def refresh():
        codes = [code for *_, code in SERIES if code]
        data = download(BLS_API, {"seriesid": codes, "startyear": str(year - 3), "endyear": str(year)})
        if not any(len(series) for series in read_bls(data).values()):
            raise ValueError("no BLS observations found")
        return data.decode("utf-8")

    data = source_cache(out, ".inflation-bls.json", refresh)
    return read_bls(data) if data else {}


def load_fred(out, sid):
    def refresh():
        series = read_fred(download(FRED + "data/" + sid))
        if series.empty:
            raise ValueError("no FRED observations found")
        return series.to_json(date_format="iso")

    data = source_cache(out, f".inflation-fred-{sid}.json", refresh)
    return clean_series(json.loads(data)) if data else pd.Series(dtype=float)


def release_for(sid, at):
    if sid == "CORESTICKM159SFRBATL":
        # Atlanta Fed publishes by 11 a.m. ET on the CPI release day.
        return at.replace(hour=11, minute=0)
    if sid == "PCETRIM12M159SFRBDAL":
        # Dallas Fed follows the BEA date and typically publishes later that day.
        return at.replace(hour=23, minute=59)
    return at


def deadline(sid, schedule, now):
    published = [month for month, at in schedule.items() if release_for(sid, at) <= now]
    return max(published, default="")


def result(spec, raw, origin, schedule, now):
    sid, name, kind, publisher, bls_code = spec
    series = raw.loc[:pd.Timestamp(now.date())] if not raw.empty else raw
    raw_month = series.index[-1].strftime("%Y-%m") if not series.empty else ""
    if sid not in RATE_SERIES and not series.empty:
        year_ago = series.shift(freq=pd.DateOffset(years=1))
        series = ((series / year_ago.where(year_ago > 0) - 1) * 100).dropna()
    reading = {"id": sid, "name": name, "url": FRED + "series/" + sid, "origin": origin}
    if origin == "BLS":
        reading["url"] = f"https://data.bls.gov/timeseries/{bls_code}"
    schedule_url = {"애틀랜타 연은": ATLANTA, "댈러스 연은": DALLAS}.get(publisher, CALENDARS[kind])
    reading["schedule_url"] = schedule_url
    if series.empty:
        return reading | {"value": None, "status": "자료 수신 실패", "tone": "muted"}
    last = series.index[-1]
    month = last.strftime("%Y-%m")
    end = pd.Period(raw_month, freq="M")
    start = max(series.index[0].to_period("M"), end - 23)
    recent = series.reindex(pd.period_range(start, end, freq="M").to_timestamp())
    band = HISTORICAL_BANDS.get(sid, PCE_BAND)
    points = []
    previous = None
    for date, value in recent.items():
        label = PRESSURE_LABELS[pressure(value, band)] if pd.notna(value) else None
        change_from = previous if label is not None and previous != label else None
        points.append([date.strftime("%Y-%m"), float(value) if label is not None else None, label, change_from])
        previous = label
    history = {"points": points, "boundaries": band, "units": "%", "decimals": 2, "period": "최근 24개월"}
    cpi_shutdown = sid in {"CPIAUCSL", "CPILFESL"} and pd.Timestamp("2025-10-01") not in raw.index
    if cpi_shutdown and any(date in {"2025-10", "2026-10"} and value is None
                            for date, value, *_ in history["points"]):
        reading["history_note"] = (
            "2025년 10월에는 미국 연방정부 셧다운으로 CPI 조사가 이뤄지지 않았습니다. "
            "그달의 CPI 지수와 이를 기준으로 하는 2026년 10월 전년비는 차트에서 빈 구간으로 표시합니다."
        )
    prev = series.get(last - pd.DateOffset(months=3))
    expected = deadline(sid, schedule, now)
    next_month = str(last.to_period("M") + 1)
    upcoming = schedule.get(next_month)
    if expected > raw_month:
        status = f"{expected} 최신분 미반영"
        tone = "late"
    elif raw_month > month:
        status = f"{raw_month} 전년비 산출 불가 · 1년 전 지수 없음"
        tone = "gap"
        if cpi_shutdown and raw_month == "2026-10":
            status = "2026-10 전년비 산출 불가 · 2025년 10월 미국 연방정부 셧다운으로 기준 지수 미발표"
            reading["schedule_url"] = "https://www.bls.gov/news.release/archives/cpi_11102026.htm"
    elif upcoming and release_for(sid, upcoming) > now:
        status = f"최신 발표 반영 · {int(next_month[-2:])}월분 {upcoming:%m/%d} 발표 예정"
        tone = "current"
    else:
        status = "다음 발표 일정 미확인"
        tone = "muted"
    value = series.iloc[-1]
    signal = pressure(value, band) if tone == "current" else None
    prior = recent.get(last - pd.DateOffset(months=1))
    changed = None if signal is None or pd.isna(prior) else pressure(prior, band) != signal
    return reading | {"value": value, "month": month, "band": band, "signal": signal, "changed": changed,
                      "change": None if prev is None else value - prev,
                      "status": status, "tone": tone, "history": history}


def collect(out, now):
    with ThreadPoolExecutor(max_workers=8) as pool:
        releases = {kind: pool.submit(load_schedule, out, kind) for kind in CALENDARS}
        bls = pool.submit(load_bls, out, now.year)
        fred = {sid: pool.submit(load_fred, out, sid) for sid, *_, bls_code in SERIES if not bls_code}
        schedules = {kind: job.result() for kind, job in releases.items()}
        local = bls.result()
        readings = []
        for spec in SERIES:
            sid, _, kind, publisher, bls_code = spec
            schedule = schedules[kind]
            if bls_code:
                preferred = result(spec, local.get(bls_code, pd.Series(dtype=float)), "BLS", schedule, now)
                if preferred["value"] is not None and preferred["tone"] != "late":
                    readings.append(preferred)
                    continue
                fallback = result(spec, load_fred(out, sid), "FRED", schedule, now)
                if fallback.get("month", "") > preferred.get("month", ""):
                    readings.append(fallback)
                else:
                    readings.append(preferred)
            else:
                readings.append(result(spec, fred[sid].result(), f"{publisher} · FRED", schedule, now))
    return readings


CSS = """
.inflation-status a,.inflation-source a{color:inherit;text-underline-offset:2px}
[data-inflation-signal="-1"]{color:var(--accent)}
[data-inflation-signal="1"]{color:var(--bad)}
.inflation-release{margin-top:5px;padding-top:4px;border-top:1px solid var(--border)}
"""


def render(readings):
    label, score, _, coverage = pressure_summary(readings)
    direction = (score > 0) - (score < 0)
    rows = []
    for row in readings:
        value = row["value"]
        name = escape(row["name"])
        reading = f"{value:.2f}%" if value is not None else "—"
        delta = row.get("change")
        observation = ""
        if month := row.get("month"):
            observation = f'<small>관측월 <time datetime="{month}">{month}</time></small>'
        comparison = (
            f'<div class="market-delta">3개월 전 대비 · <b>{delta:+.2f}%p</b></div>'
            if delta is not None else ""
        )
        status, separator, schedule = row["status"].partition(" · ")
        status_link = escape(row["schedule_url"], quote=True)
        detail = f'<small>{escape(schedule)}</small>' if separator else ""
        history = render_market_history(row["name"], row.get("history"))
        signal = row.get("signal")
        state, change = "delayed", "no"
        assessment = '<strong>판정 보류</strong>'
        if signal is not None:
            state = "negative" if signal < 0 else "current"
            changed = row["changed"]
            change = "unknown" if changed is None else "yes" if changed else "no"
            low, high = row["band"]
            limits = {
                1: f"{low:g}% 미만",
                0: f"{low:g}% 이상 ~ {high:g}% 미만",
                -1: f"{high:g}% 이상",
            }
            points = {1: "+1", 0: "0", -1: "−1"}
            assessment = (
                f'<strong data-inflation-signal="{signal}">{PRESSURE_LABELS[signal]}</strong>'
                f'<span class="market-direction">{points[signal]}</span>'
                f'<div class="market-zone-rule">기준 {limits[signal]}</div>'
            )
        elif value is None:
            state = "unavailable"
        note = row.get("history_note")
        history_note = f'<small>{escape(note)}</small>' if note else ""
        source = f'<a href="{escape(row["url"], quote=True)}">{escape(row["origin"])}</a>'
        rows.append(
            f'<article class="market-row inflation-reading" data-series="{row["id"]}" data-state="{state}" data-change="{change}">'
            f'<div class="market-label"><h4>{name}</h4></div>'
            f'<div class="market-reading"><b>{reading}</b>{observation}{comparison}</div>'
            f'<div class="market-verdict inflation-status" data-status="{row["tone"]}">'
            f'{assessment}<div class="inflation-release"><small><a href="{status_link}">{escape(status)}</a></small>{detail}</div></div>'
            f'<details class="market-row-details"><summary>상세 보기<span class="market-sr-only"> · {name}</span></summary>'
            f'<div class="market-detail-body">{history}'
            f'<div>{history_note}<small class="inflation-source">출처 {source}</small></div></div></details></article>'
        )
    return (
        '<section class="market-section" data-market-section="inflation" aria-label="물가" data-market-custom-guide hidden>'
        f'<header class="market-section-head"><span data-inflation-signal="{direction}">{label}</span>'
        f'<small>{coverage} · 점수 {score:+d} · 발표일은 미국 동부 기준</small></header>'
        + "".join(rows) +
        '<details class="market-guide"><summary>물가 판정 기준</summary><ul>'
        f'<li><a href="{FED_TARGET}">연준의 장기 물가 목표는 헤드라인 PCE 2%</a>입니다. '
        '대시보드는 PCE 계열에서 1.5% 이상 2.5% 미만을 중립으로 봅니다.</li>'
        '<li>CPI·PPI·Sticky CPI의 기준은 각 지표의 2012~2025년 전년비 관측값에서 구한 25·75백분위입니다.</li>'
        '<li>발표 일정이 확인된 최신 지표마다 완화 +1, 중립 0, 부담 −1을 부여해 합산합니다.</li>'
        '</ul></details></section>'
    )


def market_card(readings):
    label, score, counts, coverage = pressure_summary(readings)
    direction = (score > 0) - (score < 0)
    core = next((row for row in readings if row["id"] == "PCEPILFE" and row.get("signal") is not None), None)
    summary = f'<span class="market-group-reason">근원 PCE <b>{core["value"]:.2f}%</b></span>' if core else ""
    return ('<button class="market-group-card" type="button" data-market-group="inflation" '
            'aria-controls="market-evidence-list" aria-pressed="false">'
            '<span class="market-group-name">물가<span aria-hidden="true">›</span></span>'
            f'<strong data-inflation-signal="{direction}">{label}</strong>'
            f'{summary}<small>{counts} · {coverage}</small></button>')
