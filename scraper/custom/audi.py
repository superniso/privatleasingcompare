"""Audi Danmark - privatleasing. Alle modeller ligger på én side. Bruger den delte Semler-scraper (_semler.py)."""
import importlib.util, pathlib

_spec = importlib.util.spec_from_file_location("_semler", pathlib.Path(__file__).with_name("_semler.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

URL = "https://ww4.audi.dk/privatleasing/"


async def urls(page):
    return [("privatleasing", URL)]


async def scrape(page):
    return await S.scrape_site(page, "audi", urls)
