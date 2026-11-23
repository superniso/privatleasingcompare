"""Cupra Danmark - privatleasing. Flow: oversigt (/online-privatleasing/) -> model (?type=configuration) ->
udstyrsvariant (/<model>/<variant>/konfigurator/). På konfiguratorsiden vælges km (rullemenu) og førstegangsydelse
(kort med radioknap), og opsummeringen til højre viser ydelse, gebyrer og totalomkostninger.
Bruger hjælpefunktioner fra den delte Semler-scraper (_semler.py)."""
import importlib.util, pathlib, re

_spec = importlib.util.spec_from_file_location("_semler", pathlib.Path(__file__).with_name("_semler.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

BASE = "https://www.cupradanmark.dk"
OVERVIEW = BASE + "/online-privatleasing/"
PRICE_BASE = "https://www.cupra.dk/prisliste"   # vejledende priser; fx /prisliste/raval/ (modelnavn uden "cupra-")
CARD = ".card-model-leasing-list-item"
digits = S.digits

SUMMARY_JS = r"""() => {
  const root = document.querySelector('.configuration-sticky-wrapper') || document;
  const out = {};
  root.querySelectorAll('.summary-content-item, .summary-content-header-lg').forEach(i => {
    const s = i.querySelectorAll(':scope > span');
    if (s.length >= 2) out[s[0].innerText.trim()] = s[1].innerText.trim();
  });
  const h3 = root.querySelector('.summary-header h3'), p = root.querySelector('.summary-header p');
  out['_model'] = h3 ? h3.innerText.trim() : '';
  out['_variant'] = p ? p.innerText.trim() : '';
  return out;
}"""

DOWN_CARDS_JS = r"""() => [...document.querySelectorAll('.card-model-configuration')].map(c => {
  const input = c.querySelector('input.card-model-input');
  const prices = [...c.querySelectorAll('.card-prices-item')].map(i => i.innerText.replace(/\s+/g, ' ').trim());
  return {id: input ? input.id : null, prices};
}).filter(c => c.id)"""


async def goto(page, url):
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        pass  # networkidle kan time ud; vi venter på indholdet i stedet
    await S.click_consent(page, S.REJECT)


async def scroll(page):
    for _ in range(5):
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(400)


async def slugs_on_page(page):
    hrefs = await page.eval_on_selector_all(f"{CARD} a[href]", "els => els.map(e => e.getAttribute('href'))")
    return {m.group(1) for h in hrefs if (m := re.search(r"/online-privatleasing/([^/?#]+)/?(?:[?#]|$)", h or ""))}


async def variants_on_page(page, slug):
    hrefs = await page.eval_on_selector_all(f"{CARD} a[href]", "els => els.map(e => e.getAttribute('href'))")
    return {m.group(1) for h in hrefs if (m := re.search(rf"/online-privatleasing/{re.escape(slug)}/([^/?#]+)/konfigurator", h or ""))}


def norm(text):
    return " ".join((text or "").split()).casefold()


def match_prices(rows, prices):
    """Prisboksen har titel 'CUPRA Raval Plus' + '135 hk'; opsummeringen har model 'CUPRA Raval' + variant 'Plus 135 hk'.
    Sat sammen er begge 'CUPRA Raval Plus 135 hk', og det er det, vi matcher på."""
    by_name = {norm(f"{t} {d}"): price for (t, d), price in prices.items() if d}
    for r in rows:
        price = by_name.get(norm(f"{r['model']} {r['variant']}"))
        if price:
            r["new_price"] = str(price)
    return sorted({f"{r['model']} {r['variant']}" for r in rows if not r["new_price"]})


async def read_summary(page, want_down, want_km, timeout=6000):
    """Venter til opsummeringen viser den valgte udbetaling og km og er holdt op med at ændre sig."""
    t = 0
    while t < timeout:
        s = await page.evaluate(SUMMARY_JS)
        if digits(s.get("Førstegangsydelse", "")) == want_down and digits(s.get("Km pr. år", "")) == want_km and s.get("Samlet mdl. ydelse"):
            await page.wait_for_timeout(250)
            if await page.evaluate(SUMMARY_JS) == s:
                return s
        await page.wait_for_timeout(300)
        t += 300
    return None


async def scrape_configurator(page, url, out):
    """Henter alle kombinationer af km og førstegangsydelse for én udstyrsvariant."""
    await goto(page, url)
    await page.wait_for_selector("#kilometer-options", timeout=30000)
    await page.wait_for_selector(".card-model-configuration", timeout=30000)
    kms = [int(v) for v in await page.eval_on_selector_all(
        "#kilometer-options option", "els => els.map(e => e.value).filter(v => /^\\d+$/.test(v))")]
    got = 0
    for km in kms:
        await page.select_option("#kilometer-options", str(km))
        await page.wait_for_timeout(500)
        for i, card in enumerate(await page.evaluate(DOWN_CARDS_JS)):
            want_down = digits(card["prices"][1]) if len(card["prices"]) > 1 else None
            await page.evaluate("id => document.getElementById(id).click()", card["id"])
            s = await read_summary(page, want_down, km)
            if not s:
                print(f"[cupra] kunne ikke aflæse opsummeringen for {url} ved {km} km, udbetaling {want_down}")
                continue
            months = digits(s.get("Leasingperiode", ""))
            fee = sum(digits(v) for k, v in s.items() if re.search(r"gebyr|stiftelse|etablering", k, re.I))
            total = next((digits(v) for k, v in s.items() if k.startswith("Totalomkostninger")), None)
            down = digits(s["Førstegangsydelse"])
            calc = down + digits(s["Samlet mdl. ydelse"]) * months + fee
            if total is not None and calc != total:
                print(f"[cupra] ADVARSEL {s['_model']} {s['_variant']}: beregnet {calc} != side {total}")
            out.append({"model": s["_model"], "variant": s["_variant"], "monthly": s["Samlet mdl. ydelse"], "down": str(down),
                        "months": f"{months} mdr.", "km": f"{km:,}".replace(",", "."), "new_price": "", "fee": str(fee), "link": url})
            got += 1
    return got


async def scrape(page):
    S.TAG = "cupra"
    S.WARNINGS.clear()
    await goto(page, OVERVIEW)
    await page.wait_for_selector(CARD, timeout=30000)
    await scroll(page)
    models = sorted(await slugs_on_page(page))
    print(f"[cupra] fandt {len(models)} modeller på oversigten: {models}")
    out = []
    for slug in models:
        try:
            await goto(page, f"{BASE}/online-privatleasing/{slug}/?type=configuration")
            await page.wait_for_selector(CARD, timeout=30000)
            await scroll(page)
            variants = sorted(await variants_on_page(page, slug))
        except Exception as e:
            S.WARNINGS.append(f"{slug}: {(str(e).splitlines() or [repr(e)])[0][:120]}"); print(f"[cupra] FEJL ved model {S.WARNINGS[-1]}"); continue
        print(f"[cupra] {slug}: {len(variants)} udstyrsvarianter: {variants}")
        before = len(out)
        for v in variants:
            try:
                n = await scrape_configurator(page, f"{BASE}/online-privatleasing/{slug}/{v}/konfigurator/", out)
                print(f"[cupra] {slug}/{v}: {n} tilbud")
            except Exception as e:
                S.WARNINGS.append(f"{slug}/{v}: {(str(e).splitlines() or [repr(e)])[0][:120]}"); print(f"[cupra] FEJL ved {S.WARNINGS[-1]}")
        # vejledende nypris fra modellens prisside (ligger på cupra.dk, ikke cupradanmark.dk)
        price_url = f"{PRICE_BASE}/{slug.removeprefix('cupra-')}/"
        try:
            await goto(page, price_url)
            await page.wait_for_selector(".sf-card-auto-pricing-details-item", timeout=20000)
            prices = await S.list_prices(page)
            print(f"[cupra] {slug}: {len({k for k in prices if k[1]})} vejledende priser fundet på {price_url}")
            missing = match_prices(out[before:], prices)
        except Exception as e:
            print(f"[cupra] {slug}: ingen priser fundet på {price_url} ({(str(e).splitlines() or [repr(e)])[0][:80]})")
            missing = sorted({f"{r['model']} {r['variant']}" for r in out[before:]})
        if missing:
            print(f"[cupra] {slug}: nypris mangler for {missing} - læg dem i new_prices.yaml")
    if not out:
        raise RuntimeError("ingen leasingtilbud fundet" + (f" ({'; '.join(S.WARNINGS)})" if S.WARNINGS else ""))
    return out
