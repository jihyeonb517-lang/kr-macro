"""삼성전자 · SK하이닉스 뉴스: Google 뉴스 RSS → 주요 언론사만 → 같은 내용 묶기 → (키가 있으면) AI 요약.

피드가 주는 제목·언론사·시각만 쓰고 기사 본문은 가져오지 않는다.
표준 라이브러리만 쓴다 (Claude API도 urllib로 직접 호출).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse

COMPANIES = {
    "005930": {"name": "삼성전자", "query": "삼성전자", "color": "#49a5e8"},
    "000660": {"name": "SK하이닉스", "query": "SK하이닉스", "color": "#ffad33"},
}
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
LOOKBACK_DAYS = 7
MAX_PER_COMPANY = 30

# 주요 언론사: 피드의 언론사 웹사이트 도메인으로 판별 (하위 도메인 포함). 앞에 있을수록 우선 —
# 같은 내용을 여러 곳이 보도하면 앞선 언론사 기사를 대표로 보여 주고 나머지는 접어 둔다.
MAJOR_OUTLETS = (
    # 통신사
    "yna.co.kr", "news1.kr", "newsis.com",
    # 종합일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    "hankookilbo.com", "seoul.co.kr", "kmib.co.kr", "munhwa.com",
    # 경제일간지
    "hankyung.com", "mk.co.kr", "sedaily.com", "mt.co.kr", "edaily.co.kr",
    "asiae.co.kr", "fnnews.com", "heraldcorp.com",
    # 방송사
    "kbs.co.kr", "imbc.com", "sbs.co.kr", "jtbc.co.kr", "ytn.co.kr", "yonhapnewstv.co.kr",
    # IT일간지
    "etnews.com", "dt.co.kr",
)
# 주요 언론사 도메인 아래의 주간지·전문 채널
EXCLUDED_HOSTS = ("weekly.donga.com", "weekly.chosun.com", "magazine.hankyung.com", "science.ytn.co.kr")

# 같은 내용 판정: 회사명·[속보] 같은 태그를 뺀 제목의 글자쌍이 짧은 쪽 기준으로 얼마나 겹치는지.
# 기존 묶음의 모든 제목과 평균 0.45 이상 겹치면(72시간 이내) 그 묶음에 넣는다. 평균을 쓰는 이유는
# 실적 발표일처럼 기사가 몰린 날 서로 다른 기사가 연쇄적으로 한 묶음이 되는 것을 막기 위해서다.
SAME_STORY_OVERLAP = 0.45
STORY_WINDOW = timedelta(hours=72)
_STOPWORDS = {"삼성전자", "삼성", "sk하이닉스", "하이닉스", "속보", "단독", "종합", "포토", "2보"}

DIGEST_TTL = timedelta(hours=6)
DIGEST_SYSTEM = """You summarize news headlines about Korean memory-chip makers (Samsung
Electronics, SK hynix) for an investor dashboard.

You only see each article's headline, publisher and publication time. Use nothing else: do not
add facts, numbers or context from memory, and do not guess beyond the headline.

Reply with JSON only, no other text:
{"highlights": ["3 to 5 short lines on the main themes, most important first"],
 "summaries": {"<article id>": "one plain line restating what that headline reports"}}

Write in Korean. Keep a neutral, factual tone. Never give buy/sell/hold opinions, price targets
or predictions; if a headline itself contains a forecast, attribute it to the outlet."""


def _clean_title(title: str, source: str | None) -> str:
    title = html.unescape(title).strip()
    if source:
        while title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
    return title


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def outlet_rank(host: str | None) -> int | None:
    """MAJOR_OUTLETS 순서 (작을수록 우선). 주요 언론사가 아니면 None."""
    if not host:
        return None
    host = host.lower().removeprefix("www.").removeprefix("m.")
    if host in EXCLUDED_HOSTS:
        return None
    for rank, domain in enumerate(MAJOR_OUTLETS):
        if host == domain or host.endswith("." + domain):
            return rank
    return None


def parse_feed(xml_bytes: bytes, ticker: str, now: datetime, days: int = LOOKBACK_DAYS) -> list[dict]:
    """최근 days일 안의 기사, 제목이 완전히 같은 기사는 하나만."""
    cutoff = now - timedelta(days=days)
    seen: set[str] = set()
    out = []
    for item in ET.fromstring(xml_bytes).findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        raw_title = item.findtext("title") or ""
        if not link or not raw_title:
            continue
        node = item.find("source")
        source = ((node.text if node is not None else None) or "").strip() or None
        title = _clean_title(raw_title, source)
        key = _norm(title)
        if not key or key in seen:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        if published is None:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        published = published.astimezone(UTC)
        if published < cutoff or published > now + timedelta(minutes=5):
            continue
        if urlparse(link).scheme not in ("http", "https"):
            continue
        seen.add(key)
        out.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "ticker": ticker,
            "title": title,
            "source": source,
            "source_host": urlparse(node.get("url", "")).hostname if node is not None else None,
            "published_at": published.isoformat() if published else None,
            "link": link,
        })
    return out


def _pairs(title: str) -> set[str]:
    text = re.sub(r"\[[^\]]*\]", " ", title.lower())
    joined = "".join(w for w in re.findall(r"[0-9a-z가-힣]+", text) if w not in _STOPWORDS)
    return {joined[i : i + 2] for i in range(len(joined) - 1)}


def _overlap(a: dict, b: dict) -> float:
    if a["published_at"] and b["published_at"]:
        gap = datetime.fromisoformat(a["published_at"]) - datetime.fromisoformat(b["published_at"])
        if abs(gap) > STORY_WINDOW:
            return 0.0
    pa, pb = _pairs(a["title"]), _pairs(b["title"])
    shorter = min(len(pa), len(pb))
    return len(pa & pb) / shorter if shorter >= 4 else 0.0


def select_major_stories(items: list[dict]) -> list[dict]:
    """주요 언론사만 남기고 같은 내용을 한 기사로 묶는다 (나머지는 related). 최신 묶음 먼저."""
    groups: list[list[dict]] = []
    major = [i for i in items if outlet_rank(i["source_host"]) is not None]
    for item in sorted(major, key=lambda m: m["published_at"] or ""):
        best = None
        for group in groups:
            score = sum(_overlap(item, m) for m in group) / len(group)
            if score >= SAME_STORY_OVERLAP and (best is None or score > best[0]):
                best = (score, group)
        if best is None:
            groups.append([item])
        else:
            best[1].append(item)

    stories = []
    for members in groups:
        members.sort(key=lambda m: (outlet_rank(m["source_host"]), m["published_at"] or ""))
        lead = members[0]
        stories.append({
            **lead,
            "related": [{k: o[k] for k in ("title", "source", "published_at", "link")} for o in members[1:]],
            "latest": max(m["published_at"] or "" for m in members),
        })
    stories.sort(key=lambda s: s["latest"], reverse=True)
    return stories[:MAX_PER_COMPANY]


def fetch_stories(ticker: str, days: int = LOOKBACK_DAYS, after: str | None = None, before: str | None = None) -> list[dict]:
    """최근 days일 뉴스. after·before(YYYY-MM-DD)를 주면 그 사이 기간만 검색한다 (피드가 한 번에 약 100건만 주므로 지난 기간은 나눠 받는다)."""
    window = f"after:{after} before:{before}" if after and before else f"when:{days}d"
    query = quote(f"{COMPANIES[ticker]['query']} {window}")
    request = urllib.request.Request(RSS_URL.format(query=query), headers={"User-Agent": "Mozilla/5.0"})
    xml_bytes = urllib.request.urlopen(request, timeout=30).read()
    return select_major_stories(parse_feed(xml_bytes, ticker, datetime.now(UTC), days))


def digest(stories: list[dict], api_key: str, model: str, cache_path: Path) -> dict:
    """Claude가 쓴 핵심 요약과 기사별 한 줄 요약. 같은 기사 묶음이면 6시간 동안 저장본을 쓴다."""
    fingerprint = [{k: s.get(k) for k in ("id", "title", "source", "published_at", "ticker")}
                   for s in sorted(stories, key=lambda s: s["id"])]
    key = hashlib.sha256(json.dumps([model, DIGEST_SYSTEM, fingerprint], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (isinstance(cached, dict) and cached.get("key") == key
                and isinstance(cached.get("highlights"), list) and isinstance(cached.get("summaries"), dict)
                and timedelta(0) <= datetime.now(UTC) - datetime.fromisoformat(cached["generated_at"]) < DIGEST_TTL):
            return cached
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass

    lines = [
        {"id": s["id"], "company": COMPANIES[s["ticker"]]["name"], "headline": s["title"],
         "publisher": s["source"], "published_at": s["published_at"]}
        for s in stories
    ]
    body = json.dumps({
        "model": model,
        "max_tokens": 6000,
        "system": DIGEST_SYSTEM,
        "messages": [{"role": "user", "content": json.dumps(lines, ensure_ascii=False)}],
    }).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    response = json.loads(urllib.request.urlopen(request, timeout=120).read())
    text = "".join(block.get("text", "") for block in response.get("content", []) if block.get("type") == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"요약 응답을 읽지 못했습니다 (stop_reason={response.get('stop_reason')})")
    parsed = json.loads(match.group(0))
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("highlights"), list)
            or not isinstance(parsed.get("summaries"), dict)):
        raise ValueError("요약 응답의 형식이 올바르지 않습니다")
    known = {s["id"] for s in stories}
    result = {
        "key": key,
        "model": model,
        "generated_at": datetime.now(UTC).isoformat(),
        "highlights": [str(h).strip() for h in parsed.get("highlights", []) if str(h).strip()][:5],
        "summaries": {k: str(v).strip() for k, v in (parsed.get("summaries") or {}).items() if k in known},
    }
    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
