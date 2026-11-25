"""BMW Danmark - privatleasing.
Tilbud: karrusel på bmw.dk/.../privatleasing-og-lan.html. Hver slide = én udstyrsvariant (ét tilbud):
  overskrift (model, fx 'iX1 xDrive30'), tekst under (udstyrslinje, fx 'M Sport') og en liste med tal.
Administrationsgebyret pr. måned lægges til ydelsen, og etableringsgebyret regnes som engangsgebyr.
  (Kontrol: udbetaling + måneder x (ydelse + admin) + etablering = BMW's 'Totalomkostninger'.)
Nypris: tabel på priser-bmw-specialmodeller.html, som skifter, når man trykker på en modelknap (button.serie).
Bruger hjælpefunktioner fra den delte Semler-scraper (_semler.py)."""
import importlib.util, pathlib, re

_spec = importlib.util.spec_from_file_location("_semler", pathlib.Path(__file__).with_name("_semler.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

LEASING_URL = "https://www.bmw.dk/da/bmw-financial-services-oversikt/tilbud/privatleasing-og-lan.html"
PRICE_URL = "https://www.bmw.dk/da/find-kob/privat/priser-bmw-specialmodeller.html"
digits = S.digits

# Alle slides i karrusellen (også dem der ikke er synlige). Findes karrusellen ikke, bruges de mindste containere,
# der både har en overskrift og tallene.
SLIDES_JS = r"""() => {
  const clean = x => (x || '').replace(/\s+/g, ' ').trim();
  const titles = el => [...el.querySelectorAll('.cmp-title__text')].map(e => clean(e.textContent)).filter(t => t && !/^Privatleasing$/i.test(t));
  const ok = el => /Månedlig ydelse/i.test(el.textContent) && /Førstegangsbetaling/i.test(el.textContent) && titles(el).length > 0;
  let slides = [...document.querySelectorAll('.cmp-carousel__item')].filter(ok);
  if (!slides.length) {
    slides = [...document.querySelectorAll('.cmp-container')].filter(ok)
      .filter(c => ![...c.querySelectorAll('.cmp-container')].some(ok));
  }
  return slides.map(s => {
    const title = titles(s)[0] || '';
    const tEl = [...s.querySelectorAll('.cmp-title__text')].find(e => clean(e.textContent) === title);
    const box = tEl ? tEl.closest('.cmp-container') : null;
    const para = box ? box.querySelector('.cmp-text__paragraph') : null;
    return {id: s.id || '', title, variant: clean(para ? para.textContent : ''), text: clean(s.textContent)};
  });
}"""

PRICE_ROWS_JS = r"""() => [...document.querySelectorAll('.model')].map(r => {
  const t = s => { const e = r.querySelector(s); return e ? e.textContent.replace(/\s+/g, ' ').trim() : ''; };
  return {name: t('.model-name'), price: t('.vejledende_kampagne_pris')};
}).filter(x => x.name && /\d/.test(x.price))"""


def norm(text):
    return " ".join((text or "").split()).casefold()


def parse_slide(sl):
    """Én række ud fra en slide, eller None hvis de vigtigste tal mangler."""
    t = sl["text"]

    def num(pattern):
        m = re.search(pattern, t, re.I)
        return digits(m.group(1)) if m else None

    base = num(r"Månedlig ydelse\s*([\d.]+)\s*kr")
    down = num(r"Førstegangsbetaling:?\s*([\d.]+)")
    months = num(r"Leasing\s*periode:?\s*(\d+)")
    km = num(r"Km i perioden:?\s*([\d.]+)")  # pr. år
    total = num(r"Totalomkostninger:?\s*([\d.]+)")
    setup = num(r"Etableringsgebyr:?\s*([\d.]+)") or 0
    admin = num(r"Administrationsgebyr pr\.?\s*md\.?:?\s*([\d.]+)") or 0
    if None in (base, down, months, km) or not sl["title"]:
        return None
    monthly = base + admin  # administrationsgebyret opkræves hver måned
    calc = down + months * monthly + setup
    if total is not None and calc != total:
        print(f"[bmw] ADVARSEL {sl['title']} {sl['variant']}: beregnet {calc} != BMW's totalomkostninger {total}")
    return {"model": sl["title"], "variant": sl["variant"], "monthly": str(monthly), "down": str(down),
            "months": f"{months} mdr.", "km": f"{km:,}".replace(",", "."), "new_price": "", "fee": str(setup), "link": LEASING_URL}


async def goto(page, url):
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        pass  # networkidle kan time ud; vi venter på indholdet i stedet
    await S.click_consent(page, S.REJECT)


async def find_frame(page, selector, timeout_ms=20000):
    """Finder siden/iframen, hvor selectoren findes (modelknapperne kan ligge i en iframe)."""
    waited = 0
    while waited < timeout_ms:
        for fr in page.frames:
            try:
                if await fr.query_selector(selector):
                    return fr
            except Exception:
                pass
        try:
            await page.mouse.wheel(0, 800)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        waited += 1000
    return None


async def settled_rows(frame, prev_sig, timeout=6000):
    """Venter til tabellen er skiftet efter et klik på en modelknap og er holdt op med at ændre sig."""
    last, t = None, 0
    while t < timeout:
        rows = await frame.evaluate(PRICE_ROWS_JS)
        sig = [(r["name"], r["price"]) for r in rows]
        if rows and sig == last and (sig != prev_sig or t >= 3000):
            return rows, sig
        last = sig
        await frame.wait_for_timeout(400)
        t += 400
    return [], last


async def collect_prices(page):
    """Vejledende priser for alle modeller: tryk på hver modelknap og læs tabellen. Returnerer {normaliseret navn: pris}."""
    await goto(page, PRICE_URL)
    frame = await find_frame(page, "button.serie")
    if not frame:
        print(f"[bmw] fandt ingen modelknapper på {PRICE_URL} (sidetitel: {await page.title()!r})")
        return {}
    slugs = await frame.eval_on_selector_all("button.serie", "els => els.map(e => e.dataset.slug)")
    prices, prev = {}, None
    for slug in slugs:
        await frame.evaluate("s => document.querySelector(`button.serie[data-slug='${s}']`).click()", slug)
        rows, prev = await settled_rows(frame, prev)
        for r in rows:
            prices[norm(r["name"])] = digits(r["price"])
    print(f"[bmw] vejledende priser: {len(prices)} modeller/varianter fra {len(slugs)} modelknapper")
    return prices


def match_prices(rows, prices):
    for r in rows:
        key = norm(f"{r['model']} {r['variant']}")
        price = prices.get(key) or prices.get(key.removeprefix("bmw "))
        if price:
            r["new_price"] = str(price)
    return sorted({f"{r['model']} {r['variant']}" for r in rows if not r["new_price"]})


async def scrape(page):
    S.TAG = "bmw"
    S.WARNINGS.clear()
    await goto(page, LEASING_URL)
    title = await page.title()
    if "denied" in title.lower():
        raise RuntimeError("BMW afviser forespørgslen (Access Denied) - sandsynligvis blokering af serverens IP")
    try:
        await page.wait_for_selector(".cmp-carousel__item, .cmp-container", timeout=30000)
    except Exception:
        raise RuntimeError(f"ingen indhold på {LEASING_URL} (sidetitel: {title!r})")
    for _ in range(6):  # scroll, så alt indhold bliver indlæst
        await page.mouse.wheel(0, 1500)
        await page.wait_for_timeout(400)
    slides = await page.evaluate(SLIDES_JS)
    out, seen = [], set()
    for sl in slides:
        row = parse_slide(sl)
        if not row:
            S.WARNINGS.append(f"slide kunne ikke aflæses: {sl['title'] or sl['id']}")
            print(f"[bmw] kunne ikke aflæse slide: {sl['title']!r} {sl['variant']!r} ({sl['text'][:120]})")
            continue
        key = (row["model"], row["variant"], row["km"], row["down"], row["months"])
        if key not in seen:
            seen.add(key); out.append(row)
    print(f"[bmw] fandt {len(slides)} slides, {len(out)} tilbud: {[(r['model'], r['variant']) for r in out]}")
    if not out:
        raise RuntimeError("ingen leasingtilbud fundet")
    try:
        missing = match_prices(out, await collect_prices(page))
    except Exception as e:
        print(f"[bmw] kunne ikke hente vejledende priser: {(str(e).splitlines() or [repr(e)])[0][:100]}")
        missing = sorted({f"{r['model']} {r['variant']}" for r in out})
    if missing:
        print(f"[bmw] nypris mangler for {missing} - læg dem i new_prices.yaml")
    return out
