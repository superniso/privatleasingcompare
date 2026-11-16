"""Audi Danmark - privatleasing. Oversigten (/privatleasing/) har ét kort pr. model; selve leasingkortene og
filtrene ligger på hver models side (/priser/<model>/leasingpriser/). Bruger den delte Semler-scraper (_semler.py)."""
import importlib.util, pathlib, re

_spec = importlib.util.spec_from_file_location("_semler", pathlib.Path(__file__).with_name("_semler.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

BASE = "https://ww4.audi.dk"
OVERVIEW = BASE + "/privatleasing/"
FALLBACK_SLUGS = ["a2-e-tron"]           # bruges hvis oversigten ikke kan læses; tilføj evt. flere her


async def collect_slugs(page):
    """Modelnavne (slugs) fra linkene i oversigtens kort, fx /priser/a2-e-tron/leasingpriser/?tab=leasingpriser."""
    for _ in range(6):  # scroll, så alle kort bliver indlæst
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(500)
    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    return {m.group(1) for h in hrefs if (m := re.search(r"/priser/([^/?#]+)/leasingpriser", h or ""))}


async def urls(page):
    found = set()
    try:
        try:
            await page.goto(OVERVIEW, wait_until="networkidle", timeout=45000)
        except Exception:
            pass
        await page.wait_for_selector(".sf-card-auto", timeout=30000)
        found = await collect_slugs(page)
        print(f"[audi] fandt {len(found)} modeller på oversigten: {sorted(found)}")
    except Exception as e:
        print(f"[audi] kunne ikke læse oversigten: {str(e).splitlines()[0][:100]}")
    return [(slug, f"{BASE}/priser/{slug}/leasingpriser/") for slug in sorted(found | set(FALLBACK_SLUGS))]


async def scrape(page):
    return await S.scrape_site(page, "audi", urls)
