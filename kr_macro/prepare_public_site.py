"""Stage only the generated page after checking for configured credentials."""
import html
import urllib.parse
from pathlib import Path

root = Path(__file__).resolve().parent
page = (root / "output/dashboard.html").read_bytes()
body = page.decode("utf-8")
for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
    name, sep, value = line.partition("=")
    if not sep or name.startswith("#") or name.strip() == "NEWS_SUMMARY_MODEL":
        continue
    value = value.strip().strip("\"'")
    if value and any(item in body for item in (
        value, html.escape(value), urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value), urllib.parse.unquote(value),
    )):
        raise SystemExit("A configured credential appears in the generated page.")
if not all(marker in body for marker in ("에이피알", "화장품 수출", "</html>")):
    raise SystemExit("Incomplete dashboard.")
site = root.parent / "site"
site.mkdir(exist_ok=True)
if any(entry.name != "index.html" for entry in site.iterdir()):
    raise SystemExit("Unexpected files in site directory.")
(site / "index.html").write_bytes(page)
