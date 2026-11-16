"""Volkswagen Danmark - privatleasing. Bruger den delte Semler-scraper (_semler.py)."""
import importlib.util, pathlib, re

_spec = importlib.util.spec_from_file_location("_semler", pathlib.Path(__file__).with_name("_semler.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

BASE = "https://www.volkswagen.dk"
OVERVIEW = BASE + "/app/da/leasingpriser/"
FALLBACK_SLUGS = ["id-tiguan"]          # tilføj flere modelsider her ved behov


async def urls(page):
    try:
        await page.goto(OVERVIEW, wait_until="networkidle", timeout=60000)
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        found = {m.group(1) for h in hrefs if (m := re.search(r"/prisliste/([^/]+)/", h or ""))}
    except Exception as e:
        print(f"[vw] kunne ikke finde modeller automatisk: {e}")
        found = set()
    return [(slug, f"{BASE}/app/da/prisliste/{slug}/leasingpriser/") for slug in sorted(found | set(FALLBACK_SLUGS))]


async def scrape(page):
    return await S.scrape_site(page, "vw", urls)
