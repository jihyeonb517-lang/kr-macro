"""Package the public dashboard and the per-company files it references."""

import html
import json
from pathlib import Path
import re
from urllib.parse import quote, quote_plus, unquote


def prepare_site(root, site):
    output = root / "output"
    page = (output / "dashboard.html").read_bytes()
    body = page.decode("utf-8")
    if not all(marker in body for marker in ("에이피알", "화장품 수출", "id=\"sector-heatmap\"", "</html>")):
        raise SystemExit("Incomplete dashboard.")
    catalog = re.search(r'<script type="application/json" id="company-directory">([\s\S]+?)</script>', body)
    if not catalog:
        raise SystemExit("Company directory metadata is missing.")
    companies = json.loads(catalog[1])
    files = {Path("index.html"): page}
    for folder, callback in (("company-data", "dashboardCompanyPayload"), ("report-data", "dashboardReportPayload")):
        for company in companies:
            if company.get("builtin"):
                continue
            ticker = company["ticker"]
            if not re.fullmatch(r"[0-9A-Z]{6}", ticker):
                raise SystemExit("Invalid company ticker in the directory.")
            path = Path(folder) / (ticker + ".js")
            source = output / path
            if not source.is_file():
                if folder == "company-data" and company.get("available"):
                    raise SystemExit("A listed stock chart is missing its data file.")
                continue
            data = source.read_bytes()
            if not data.startswith(f'window.{callback}("{ticker}",'.encode()):
                raise SystemExit("Unexpected stock or report file format.")
            files[path] = data

    credentials = set()
    for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if not sep or name.lstrip().startswith("#") or name.strip() == "NEWS_SUMMARY_MODEL":
            continue
        value = value.strip().strip("\"'")
        if value:
            credentials.update(variant.encode() for variant in (
                value, html.escape(value), quote(value, safe=""), quote_plus(value), unquote(value),
            ))
    if any(credential in data for data in files.values() for credential in credentials):
        raise SystemExit("A configured credential appears in the generated public files.")

    if site.exists():
        old = {p.relative_to(site) for p in site.rglob("*") if p.is_file()}
        if old - files.keys():
            raise SystemExit("Unexpected files in the publication directory.")
    for relative, data in files.items():
        destination = site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    print(f"Staged {len(files)} public assets.")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    prepare_site(root, root.parent / "site")
