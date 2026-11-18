"""공식 IR의 당기·전년 동기 해외 매출(백만원)을 확인해 저장한다."""
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup


def amounts(page, quarter):
    heading, separator, chart = page.partition("국내/해외 매출")
    if not separator or "백만 원" not in chart[:250] or not re.search(
            rf"{quarter.year}\s*년\s*{quarter.quarter}\s*분기\s*지역별\s*매출", heading):
        return None
    growth = re.search(r"해외.{0,25}?YoY\s*([+-]?\d+)%", heading, re.I)
    chart, separator, _ = chart.partition("성장률")
    if not growth or not separator:
        return None
    numbers = re.findall(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,]|\s*억)", chart)
    if len(numbers) != 2:
        return None
    current, prior = (int(x.replace(",", "")) for x in numbers)
    if prior and abs((current / prior - 1) * 100 - int(growth[1])) < 1:
        return current, prior
    return None


def reports():
    here = Path(__file__).resolve().parent
    records = json.loads((here / "apr_overseas.json").read_text(encoding="utf-8"))
    cache = here / "output/.apr_overseas_ir.json"
    verified_through = pd.Period(max(records), freq="Q")
    if cache.exists():
        try:
            records.update(json.loads(cache.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    latest_closed = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).to_period("Q") - 1
    missing = [q for q in pd.period_range(verified_through + 1, latest_closed, freq="Q")
               if str(q) not in records]
    if not missing:
        return records, ""
    note = ""
    try:
        from pypdf import PdfReader

        for year in sorted({q.year for q in missing}):
            url = f"https://www.apr-in.com/ir.php?year={year}"
            listing = requests.get(url, timeout=15)
            listing.raise_for_status()
            for row in BeautifulSoup(listing.content, "html.parser").select("tr"):
                period = re.search(r"(\d{4})년도\s*([1-4])분기", row.get_text())
                link = row.select_one("a.file_download_btn[href]")
                if not period or not link:
                    continue
                q = pd.Period(f"{period[1]}Q{period[2]}", freq="Q")
                if q not in missing:
                    continue
                source = urljoin(url, link["href"])
                if urlsplit(source).hostname not in {"apr-in.aprd.io","www.apr-in.com","apr-in.com"}:
                    continue
                pdf = requests.get(source, timeout=30)
                pdf.raise_for_status()
                for page in PdfReader(io.BytesIO(pdf.content)).pages:
                    pair = amounts(page.extract_text(extraction_mode="layout"), q)
                    if pair:
                        records[str(q)] = dict(current=pair[0], prior=pair[1], url=source)
                        break
        temp = cache.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        temp.replace(cache)
    except Exception:  # IR PDF 형식·접속 오류가 다른 대시보드 탭에 영향을 주지 않게 한다.
        note = "공식 IR 조회 지연 · 확인된 분기까지 표시"
    if str(latest_closed) not in records and not note:
        note = "최근 분기 해외 매출은 공식 IR 확인 대기"
    return records, note
