"""Delt scraper til leasingsider bygget på Semlers system (Volkswagen, Audi m.fl.).
Kort: .sf-card-auto. Filtre: radioknapper annualKm_<km> og downPayment_<beløb>.
Mærkespecifikke filer (vw.py, audi.py) angiver kun, hvilke sider der skal hentes."""
import re

TAG = "semler"                   # bruges i loggen, sættes af scrape_site
SKIPPED = {}                     # kort der blev sprunget over i den aktuelle model (navn -> antal)
LINK_URL = None                  # siden tilbuddene vises på (bruges som link, også hvis kortene ligger i en iframe)
KMS = [10000, 15000, 20000, 25000, 30000, 40000, 50000, 55000]   # bruges hvis siden ikke oplyser dem
DOWNS = [20000, 45000]


def digits(s):
    d = re.sub(r"\D", "", (s or "").split(",")[0])
    return int(d) if d else 0


PRICE_JS = r"""() => [...document.querySelectorAll('.sf-card-auto')].map(c => {
  const t = s => { const e = c.querySelector(s); return e ? e.textContent.replace(/\s+/g, ' ').trim() : ''; };
  const item = [...c.querySelectorAll('.sf-card-auto-pricing-details-item')].find(i => /Vejledende pris/i.test(i.textContent));
  const d = item && item.querySelector('.sf-card-auto-pricing-details-item-description');
  return d ? {title: t('.sf-card-auto-title-item'), desc: t('.sf-card-auto-description-item'), price: d.textContent} : null;
}).filter(Boolean)"""


async def list_prices(page):
    """'Vejledende pris' fra prisboksene på siden. Nøgle: (titel, effekt). Kun titel bruges, hvis den er entydig."""
    found, by_title = {}, {}
    for x in await page.evaluate(PRICE_JS):
        price = digits(x["price"])
        if price:
            found[(x["title"], x["desc"])] = price
            by_title.setdefault(x["title"], set()).add(price)
    for title, prices in by_title.items():
        if len(prices) == 1:
            found.setdefault((title, ""), next(iter(prices)))
    return found


async def uncheck_insurance(page):
    """Fjerner flueben i 'Priser inkl. forsikring' på alle kort, så ydelsen kan sammenlignes mellem mærker."""
    n = await page.evaluate("""() => { let n = 0;
      document.querySelectorAll("input[id^='insuranceCheckbox_']").forEach(e => { if (e.checked) { e.click(); n++; } });
      return n; }""")
    if n:
        await page.wait_for_timeout(800)  # lad priserne opdatere
    return n


DIAG_CARD_JS = """c => {
  const rows = [...c.querySelectorAll('.sf-card-auto-financing-details-wrapper-item')];
  const isM = i => /^(Månedlig|Mdl)/i.test(i.textContent.trim());
  const it = rows.find(isM);
  const sub = it && it.querySelector('.sf-card-auto-financing-details-wrapper-item-subject');
  const box = c.querySelector("input[id^='insuranceCheckbox_']");
  return {afkrydsning: box ? (box.checked ? 'afkrydset' : 'ikke afkrydset') : 'findes ikke',
          ydelsesnavn_html: sub ? sub.innerHTML.slice(0, 250) : null,
          forsikringsraekker: rows.filter(i => /forsikring/i.test(i.textContent) && !isM(i))
                                  .map(i => i.textContent.replace(/\\s+/g, ' ').trim())};
}"""


async def read_cards(page, prices=None):
    rows = []
    await uncheck_insurance(page)
    for card in await page.query_selector_all(".sf-card-auto"):
        if not await card.is_visible():  # filtrerede/skjulte kort giver forkert tekst
            continue
        title = await card.query_selector(".sf-card-auto-title-item")
        desc = await card.query_selector(".sf-card-auto-description-item")
        box = await card.query_selector("input[id^='insuranceCheckbox_']")
        items = await card.query_selector_all(".sf-card-auto-financing-details-wrapper-item")
        if not (title and items):  # forsikrings-afkrydsningen (box) mangler på nogle kort
            continue
        f = {}
        for it in items:
            s = await it.query_selector(".sf-card-auto-financing-details-wrapper-item-subject")
            d = await it.query_selector(".sf-card-auto-financing-details-wrapper-item-description")
            if s and d:
                f[(await s.inner_text()).strip()] = (await d.inner_text()).strip()
        if "Leasingperiode" not in f:
            continue
        monthly_key = next((k for k in f if k.startswith(("Månedlig ydelse", "Mdl. ydelse"))), None)
        ins_key = next((k for k in f if "forsikring" in k.lower() and not k.startswith(("Månedlig ydelse", "Mdl. ydelse"))), None)
        ins = digits(f[ins_key]) if ins_key else 0
        incl = bool(monthly_key) and "inkl" in monthly_key
        if incl and box and ins == 0 and not await box.is_checked():
            incl = False  # begge ydelsesnavne er i kortet, men forsikringen er fra (0 kr.): ydelsen er uden forsikring
        if not monthly_key or incl:
            # kan ikke aflæses uden forsikring: spring kortet over i stedet for at stoppe hele mærket
            name = (await title.inner_text()).strip()
            if name not in SKIPPED:  # diagnose én gang pr. kort
                print(f"[{TAG}] springer kort over: {name!r}. {await card.evaluate(DIAG_CARD_JS)}")
            SKIPPED[name] = SKIPPED.get(name, 0) + 1
            continue
        # Nypris står kun i checkbox-id'et. Mangler den, udfyldes nypris via new_prices.yaml
        new_price, down_id = ("", "")
        if box:
            _, new_price, down_id = (await box.get_attribute("id")).split("_")
        shown = digits(f.get("Førstegangsydelse", ""))
        down = str(shown) if shown else down_id
        text = await card.inner_text()
        if not new_price and prices:  # kort uden forsikrings-afkrydsning: slå nyprisen op i prisboksene
            t = " ".join((await title.inner_text()).split())
            d = " ".join((await desc.inner_text()).split()) if desc else ""
            # kun præcist match på titel + variant (titel alene bruges kun, hvis kortet ikke har nogen variant)
            new_price = str(prices.get((t, d)) or (prices.get((t, "")) if not d else None) or "")
        # engangsgebyrer i betingelserne: afleveringsgebyr og (hos Audi) stiftelsesomkostninger
        fee = sum(digits(m.group(1)) for m in re.finditer(r"(?:Afleveringsgebyr|Stiftelsesomkostninger):?\s*([\d.]+)", text))
        row = {
            "down_id": down_id,
            "model": (await title.inner_text()).strip(),
            "variant": (await desc.inner_text()).strip() if desc else "",
            "monthly": f[monthly_key], "down": down, "months": f["Leasingperiode"],
            "km": f.get("Km pr. år", ""), "new_price": new_price,
            "fee": str(fee),
            "link": (LINK_URL or page.url).split("#")[0],  # modellens privatleasing-side (kilden til priserne)
        }
        calc = digits(row["down"]) + digits(row["monthly"]) * digits(row["months"]) + digits(row["fee"])
        if "Totalomkostninger" in f and calc != digits(f["Totalomkostninger"]):
            print(f"[{TAG}] ADVARSEL {row['model']} {row['variant']}: beregnet {calc} != side {f['Totalomkostninger']}")
        rows.append(row)
    return rows


FIELDS = {"annualKm": "Km pr. år", "downPayment": "Førstegangsydelse"}
GROUPS = {"annualKm": "annualKilometer", "downPayment": "downPayment"}  # name-attribut på radioknapperne

CARD_JS = """() => [...document.querySelectorAll('.sf-card-auto')]
  .filter(c => c.querySelector('.sf-card-auto-financing-details-wrapper'))"""

VAL_OF = """(c, field) => { const it = [...c.querySelectorAll('.sf-card-auto-financing-details-wrapper-item')]
  .find(i => i.textContent.includes(field));
  const d = it && it.querySelector('.sf-card-auto-financing-details-wrapper-item-description');
  return d ? d.textContent.replace(/\\D/g, '') : '?'; }"""

# Færdig når mindst ét SYNLIGT kort viser den valgte værdi (skjulte kort ignoreres)
WAIT_JS = """([field, want]) => {
  const val_of = %s;
  return (%s)().some(c => c.getClientRects().length > 0 && val_of(c, field) === want);
}""" % (VAL_OF, CARD_JS)

DIAG_JS = """([id, field]) => {
  const val_of = %s, r = document.getElementById(id), cards = (%s)();
  return {radio_valgt: r ? r.checked : null, kort: cards.length,
          synlige: cards.filter(c => c.getClientRects().length > 0).length,
          vist: [...new Set(cards.map(c => val_of(c, field)))]};
}""" % (VAL_OF, CARD_JS)

# Fire måder at skifte en radioknap på, prøvet i rækkefølge
HOWS = [
    ("js-klik", """id => document.getElementById(id).click()"""),
    ("events",  """id => { const el = document.getElementById(id); el.checked = true;
                  for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true})); }"""),
    ("label",   None),
    ("check",   None),
]


async def options(page, group, fallback):
    vals = await page.eval_on_selector_all(
        f"input[name='{GROUPS[group]}']", "els => els.map(e => parseInt(e.value))")
    return sorted({v for v in vals if v}) or fallback


async def set_radio(page, group, value):
    """Vælger radioknappen <group>_<value>. Returnerer 'ok', 'mangler' eller 'fejl'."""
    rid, field, want = f"{group}_{value}", FIELDS[group], str(value)
    if not await page.evaluate("id => !!document.getElementById(id)", rid):
        return "mangler"
    for name, js in HOWS:
        try:
            if js:
                await page.evaluate(js, rid)
            elif name == "label":
                await page.locator(f"label[for='{rid}']").click(force=True, timeout=3000)
            else:
                await page.locator(f"#{rid}").check(force=True, timeout=3000)
            await page.wait_for_function(WAIT_JS, arg=[field, want], timeout=4000)
            await page.wait_for_timeout(600)  # lad resten af kortene nå at opdatere
            return "ok"
        except Exception:
            continue
    print(f"[{TAG}] kunne ikke vælge {rid}. Status: {await page.evaluate(DIAG_JS, [rid, field])}")
    return "fejl"


def kr(n):
    if not n:
        return "ukendt nypris"
    return f"{int(n):,}".replace(",", ".") + " kr."


async def scrape_model(page, seen, out, extra_prices=None):
    """Henter alle kombinationer af førstegangsydelse og km på den aktuelle modelside."""
    combos, snaps = set(), []
    prices = {**(extra_prices or {}), **await list_prices(page)}  # prisboksene på selve siden går forud
    print(f"[{TAG}] {len({k for k in prices if k[1]})} vejledende priser fundet i prisboksene")
    bases = {k[:5] for k in seen}

    async def collect():
        for r in await read_cards(page, prices):
            base = (r["model"], r["variant"], r["km"], r["down"], r["months"])
            k2 = base + (r["new_price"],)
            if k2 in seen:
                out[seen[k2]] = r  # samme kort læst igen: nyeste aflæsning vinder
            else:
                if base in bases:  # andet kort med samme navn (fx anden pris): behold begge
                    print(f"[{TAG}] to kort med samme navn: {r['model']} {r['variant']} ({kr(r['new_price'])})")
                    r = {**r, "variant": f"{r['variant']} ({kr(r['new_price'])})"}
                bases.add(base)
                seen[k2] = len(out); out.append(r)
            combos.add((digits(r["down"]), digits(r["km"])))

    await collect()  # standardvisning først
    downs = await options(page, "downPayment", [])  # tom liste = siden har intet filter (ét tilbud pr. kort)
    kms = await options(page, "annualKm", [])
    worked = False
    for d in (downs or [None]):
        if d is not None:
            res = await set_radio(page, "downPayment", d)
            if res == "mangler":
                print(f"[{TAG}] radioknappen downPayment_{d} findes ikke"); continue
            if res == "fejl" and not worked:
                break  # mekanismen virker ikke på denne side - spar tid
            if res != "ok":
                continue
            worked = True
            snaps.append((d, [(r["variant"], r["down"], r["down_id"], r["monthly"]) for r in await read_cards(page, prices)]))
        for k in (kms or [None]):
            if k is not None:
                if await set_radio(page, "annualKm", k) != "ok":
                    continue
            await collect()
    if downs and kms:
        extras = combos - {(d, k) for d in downs for k in kms}
        if extras:
            print(f"[{TAG}] kombinationer uden for filtrene (udbetaling, km): {sorted(extras)}")
    if len(combos) < max(1, len(downs)) * max(1, len(kms)):  # ufuldstændig: vis hvad siden faktisk viste
        for d, rows in snaps:
            print(f"[{TAG}] ved udbetaling {d} ses {len(rows)} kort (variant, vist, id, ydelse): {rows}")
    return downs, kms, combos


CARDS = ".sf-card-auto-financing-details-wrapper"
REJECT = re.compile(r"afvis|kun nødvendige|nødvendige cookies|reject|necessary only|decline", re.I)
ACCEPT = re.compile(r"accept|accepter|acceptér|tillad alle|allow all", re.I)


async def click_consent(page, rx):
    """Klikker på en knap i samtykkebanneret (også hvis det ligger i en iframe)."""
    for fr in page.frames:
        try:
            await fr.get_by_role("button", name=rx).first.click(timeout=1500)
            return True
        except Exception:
            pass
    return False


async def find_cards_frame(page, timeout_ms):
    """Finder den side/iframe, hvor leasingkortene ligger. Scroller undervejs, så sent indlæst indhold vises."""
    waited = 0
    while waited < timeout_ms:
        for fr in page.frames:
            try:
                if await fr.query_selector(CARDS):
                    return fr
            except Exception:
                pass
        try:
            await page.mouse.wheel(0, 1200)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        waited += 1000
    return None


async def diagnose_frames(page):
    info = []
    for fr in page.frames:
        try:
            info.append({"url": fr.url[:90], "kort": await fr.locator(".sf-card-auto").count(),
                         "radioknapper": await fr.locator("input[type=radio]").count(),
                         "kort_i_document": await fr.evaluate("document.querySelectorAll('.sf-card-auto').length")})
        except Exception as e:
            info.append({"url": fr.url[:90], "fejl": str(e)[:60]})
    print(f"[{TAG}] rammer på siden: {info}")
    try:
        btns = [b.strip() for b in await page.locator("button:visible").all_inner_texts() if b.strip()][:25]
        print(f"[{TAG}] synlige knapper: {btns}")
    except Exception:
        pass


async def locate_cards(page):
    await click_consent(page, REJECT)  # først "afvis/kun nødvendige", så vi ikke giver mere samtykke end nødvendigt
    frame = await find_cards_frame(page, 20000)
    if not frame and await click_consent(page, ACCEPT):
        frame = await find_cards_frame(page, 20000)
    return frame


def fill_prices(rows):
    """Kort uden nypris låner nyprisen fra kort af samme model og variant, hvis den er entydig."""
    known = {}
    for r in rows:
        if r["new_price"]:
            known.setdefault((r["model"], r["variant"]), set()).add(r["new_price"])
    for r in rows:
        if not r["new_price"]:
            v = known.get((r["model"], r["variant"]))
            if v and len(v) == 1:
                r["new_price"] = next(iter(v))


async def scrape_site(page, tag, urls):
    """urls: async funktion der returnerer [(navn, url), ...]."""
    global TAG, LINK_URL
    TAG = tag
    seen, out = {}, []
    for item in await urls(page):
        name, url = item[:2]
        price_url = item[2] if len(item) > 2 else None  # valgfri side med 'Vejledende pris' for modellen
        extra = {}
        if price_url:
            try:
                await page.goto(price_url, wait_until="networkidle", timeout=45000)
            except Exception:
                pass
            try:
                await page.wait_for_selector(".sf-card-auto-pricing-details-item", timeout=15000)
                extra = await list_prices(page)
                print(f"[{TAG}] {name}: {len({k for k in extra if k[1]})} vejledende priser fundet på prissiden")
            except Exception:
                print(f"[{TAG}] {name}: ingen priser fundet på {price_url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            pass  # networkidle kan time ud på sider med løbende sporing; vi venter på kortene i stedet
        frame = await locate_cards(page)
        if not frame:
            await diagnose_frames(page)
            raise RuntimeError(f"ingen leasingkort på {url} (sidetitel: {await page.title()!r})")
        LINK_URL = url
        before = len(out)
        downs, kms, combos = await scrape_model(frame, seen, out, extra)
        fill_prices(out[before:])
        missing = sorted({f"{r['model']} {r['variant']}" for r in out[before:] if not r["new_price"]})
        if missing:
            print(f"[{TAG}] {name}: nypris mangler for {missing} - læg dem i new_prices.yaml")
        if SKIPPED:
            print(f"[{TAG}] {name}: sprang kortaflæsninger over (kort: antal): {dict(SKIPPED)}")
            SKIPPED.clear()
        print(f"[{TAG}] {name}: {len(out) - before} tilbud, {len(combos)} af {max(1, len(downs)) * max(1, len(kms))} kombinationer "
              f"(udbetaling {downs or 'intet filter'}, km {kms or 'intet filter'})")
    if not out:
        raise RuntimeError("ingen leasingtilbud fundet")
    return out
