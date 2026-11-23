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

# Kortene til valg af førstegangsydelse indeholder alt: navn, variant, ydelse, udbetaling, løbetid og total.
CARDS_JS = r"""() => [...document.querySelectorAll('.card-model-configuration')].map(c => {
  const t = el => el ? el.innerText.replace(/\s+/g, ' ').trim() : '';
  return {model: t(c.querySelector('.card-headline h4')), variant: t(c.querySelector('.card-model')),
          period: t(c.querySelector('.card-leasing-period')),
          prices: [...c.querySelectorAll('.card-prices-item')].map(i => t(i)),
          disclaimer: t(c.querySelector('.card-disclaimer'))};
})"""


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


async def settled_cards(page, prev_sig, timeout=6000):
    """Venter til kortene er opdateret efter et km-skift (ydelserne ændrer sig) og er holdt op med at ændre sig."""
    last, t = None, 0
    while t < timeout:
        cards = await page.evaluate(CARDS_JS)
        sig = [(c["prices"], c["disclaimer"]) for c in cards]
        if cards and sig == last and (sig != prev_sig or t >= 3000):
            return cards, sig
        last = sig
        await page.wait_for_timeout(400)
        t += 400
    return [], last


def card_row(c, km, url):
    """Én række ud fra et kort. Gebyr = samlet pris i leasingperioden - udbetaling - ydelse x måneder."""
    monthly = next((digits(x) for x in c["prices"] if x.startswith("Mdl")), None)
    down = next((digits(x) for x in c["prices"] if x.startswith("Første")), None)
    months = digits(c["period"])
    m = re.search(r"Samlet pris i leasingperioden \((\d+) mdr\.\) fra:\s*([\d.]+)", c["disclaimer"])
    if monthly is None or down is None or not months:
        return None
    fee = 0
    if m:
        fee = digits(m.group(2)) - down - monthly * (digits(m.group(1)) or months)
        if not 0 <= fee <= 10000:
            print(f"[cupra] ADVARSEL {c['model']} {c['variant']}: usædvanligt gebyr ({fee}) udledt af samlet pris {m.group(2)}")
            fee = max(0, fee)
    return {"model": c["model"], "variant": c["variant"], "monthly": str(monthly), "down": str(down),
            "months": f"{months} mdr.", "km": f"{km:,}".replace(",", "."), "new_price": "", "fee": str(fee), "link": url}


async def scrape_configurator(page, url, out):
    """Henter alle kombinationer af km og førstegangsydelse for én udstyrsvariant (aflæst direkte fra kortene)."""
    await goto(page, url)
    await page.wait_for_selector("#kilometer-options", timeout=30000)
    kms = [int(v) for v in await page.eval_on_selector_all(
        "#kilometer-options option", "els => els.map(e => e.value).filter(v => /^\\d+$/.test(v))")]
    got, prev_sig, bad = 0, None, []
    for km in kms:
        await page.select_option("#kilometer-options", str(km))
        cards, prev_sig = await settled_cards(page, prev_sig)
        if not cards:
            bad.append(km)
            continue
        for c in cards:
            row = card_row(c, km, url)
            if row:
                out.append(row); got += 1
            else:
                print(f"[cupra] kunne ikke aflæse kort ved {km} km: {c}")
    if bad:
        print(f"[cupra] ingen kort for udbetaling fundet ved {bad} km på {url} (valgt km: "
              f"{await page.eval_on_selector('#kilometer-options', 'e => e.value')!r}, "
              f"antal kort: {await page.locator('.card-model-configuration').count()})")
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
