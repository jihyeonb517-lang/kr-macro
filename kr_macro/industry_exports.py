"""Monthly exports grouped by the Korea Customs Service HSK export classification."""
from functools import lru_cache
import json
from pathlib import Path

import cosmetics


TAB_PREFIX = "industry-"
DESCRIPTIONS = {
    "steel": "열연·냉연강판·강관 실적",
    "passenger": "전기차·하이브리드 등 실적",
    "petroleum": "휘발유·경유·항공유 등 실적",
    "wireless": "스마트폰·휴대전화 부품 실적",
    "ships": "탱커·화물선·여객선 등 실적",
    "auto-parts": "변속기·브레이크 등 실적",
    "precision": "의료·광학·측정기기 실적",
    "appliances": "냉장고·세탁기·에어컨 등 실적",
}


@lru_cache(maxsize=1)
def _config():
    return json.loads(Path(__file__).with_name("industry_export_codes.json").read_text(encoding="utf-8"))


def navigation():
    return [
        (f"{TAB_PREFIX}{group['id']}", f"{group['name']} 수출", DESCRIPTIONS[group["id"]])
        for group in _config()["groups"]
    ]


def collect(output, today, fetch_rows):
    definitions = _config()
    yearly = definitions["classification"]
    years = sorted(yearly)
    config = {"title": "품목별 수출", "years": 1, "items": definitions["items"], "classification": yearly}

    def fetch_month(month, groups):
        rows = fetch_rows(month)
        if rows is None:
            return None
        amounts = {code: value for code, value in rows}
        year = max((year for year in years if year <= month[:4]), default=years[0])
        return {item: sum(amounts.get(code, 0) for code in yearly[year][item]) for item in groups}

    return cosmetics.collect(output, today, config, fetch_month, cache_prefix="industry_exports")


SOURCE = (
    '<p class="note">자료 '
    '<a href="https://www.data.go.kr/data/15101609/openapi.do">관세청 품목별 수출입실적</a> · '
    '<a href="https://www.data.go.kr/data/15049720/fileData.do">2025·2026년 관세청 HSK 분류표</a> · '
    '2024년 이전은 공개된 2025년 분류표 기준. '
    '품목군 전체는 분류표의 HSK 코드를 합산한 값으로, 별도 집계되는 수출 속보와 차이가 있을 수 있습니다.</p>'
)


def views(data, note=None, overview_cards=None):
    overview_cards = overview_cards or {}
    markup, scripts = [], []
    for tab_id, title, _ in navigation():
        view_data = None
        if data is not None:
            group_id = tab_id.removeprefix(TAB_PREFIX)
            items = [
                {key: value for key, value in item.items() if key != "group"}
                for item in data["items"] if item["group"] == group_id
            ]
            view_data = {**data, "items": items}
        chart = cosmetics.render(view_data, note, namespace=tab_id, title=title)
        markup.append(f'<div class="tab" id="{tab_id}">{overview_cards.get(tab_id, "")}{chart}{SOURCE if view_data else ""}</div>')
        scripts.append(cosmetics.script(view_data, namespace=tab_id))
    return "\n".join(markup), "\n".join(scripts)
