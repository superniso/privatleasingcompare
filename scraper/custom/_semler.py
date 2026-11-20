"""Delt scraper til leasingsider bygget på Semlers system (Volkswagen, Audi m.fl.).
Kort: .sf-card-auto. Filtre: radioknapper annualKm_<km> og downPayment_<beløb>.
Mærkespecifikke filer (vw.py, audi.py) angiver kun, hvilke sider der skal hentes."""
import json, re

TAG = "semler"                   # bruges i loggen, sættes af scrape_site
WARNINGS = []                    # fejl ved enkeltmodeller (mærket regnes så som 'delvist hentet')
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
        await page.wait_for_timeout(300)
    return n


# Alle synlige kort læses i ÉT trin (ingen håndtag, der kan miste forbindelsen til siden midt i aflæsningen)
CARDS_JS = r"""() => [...document.querySelectorAll('.sf-card-auto')].map(c => {
  const t = el => el ? el.innerText.trim() : '';
  const shown = c.getClientRects().length > 0 && getComputedStyle(c).visibility !== 'hidden';
  if (!shown || !c.querySelector('.sf-card-auto-financing-details-wrapper')) return null;
  const box = c.querySelector("input[id^='insuranceCheckbox_']");
  return {
    title: t(c.querySelector('.sf-card-auto-title-item')),
    desc: t(c.querySelector('.sf-card-auto-description-item')),
    box: box ? {id: box.id, checked: box.checked} : null,
    items: [...c.querySelectorAll('.sf-card-auto-financing-details-wrapper-item')].map(i =>
      [t(i.querySelector('.sf-card-auto-financing-details-wrapper-item-subject')),
       t(i.querySelector('.sf-card-auto-financing-details-wrapper-item-description'))]),
    text: c.innerText
  };
}).filter(Boolean)"""


async def stable_cards(page, tries=12):
    """Læser kortene og gentager, til to aflæsninger i træk er ens (siden regner priserne async)."""
    last, data = None, []
    for _ in range(tries):
        try:
            data = await page.evaluate(CARDS_JS)
        except Exception:  # siden opdaterer eller navigerer: prøv igen
            await page.wait_for_timeout(400)
            continue
        sig = json.dumps([(c["title"], c["desc"], c["box"], c["items"]) for c in data], ensure_ascii=False)
        if sig == last:
            return data
        last = sig
        await page.wait_for_timeout(350)
    return data


async def read_cards(page, prices=None):
    """Aflæser alle synlige leasingkort. Kort, der ikke kan læses uden forsikring, springes over (og tælles)."""
    await uncheck_insurance(page)
    rows = []
    for c in await stable_cards(page):
        f = {k.strip(): v.strip() for k, v in c["items"] if k}
        if "Leasingperiode" not in f or not c["title"]:
            continue
        box = c["box"]
        monthly_key = next((k for k in f if k.startswith(("Månedlig ydelse", "Mdl. ydelse"))), None)
        ins_key = next((k for k in f if "forsikring" in k.lower() and not k.startswith(("Månedlig ydelse", "Mdl. ydelse"))), None)
        ins = digits(f[ins_key]) if ins_key else 0
        incl = bool(monthly_key) and "inkl" in monthly_key
        if incl and box and ins == 0 and not box["checked"]:
            incl = False  # begge ydelsesnavne er i kortet, men forsikringen er fra (0 kr.): ydelsen er uden forsikring
        if not monthly_key or incl:
            if c["title"] not in SKIPPED:  # diagnose én gang pr. kort
                state = ("afkrydset" if box["checked"] else "ikke afkrydset") if box else "findes ikke"
                print(f"[{TAG}] springer kort over: {c['title']!r}. afkrydsning: {state}, ydelsesnavn: {monthly_key!r}, "
                      f"forsikringsrækker: {[(k, v) for k, v in f.items() if 'forsikring' in k.lower()]}")
            SKIPPED[c["title"]] = SKIPPED.get(c["title"], 0) + 1
            continue
        # Nypris står kun i checkbox-id'et. Mangler den, udfyldes nypris via prisbokse eller new_prices.yaml
        new_price, down_id = "", ""
        if box and len(box["id"].split("_")) == 3:
            _, new_price, down_id = box["id"].split("_")
        raw_down = f.get("Førstegangsydelse", "")
        down = str(digits(raw_down)) if re.search(r"\d", raw_down) else down_id  # 0 kr. er en gyldig udbetaling
        if not new_price and prices:  # kort uden afkrydsning: slå nyprisen op i prisboksene
            t, d = " ".join(c["title"].split()), " ".join(c["desc"].split())
            # kun præcist match på titel + variant (titel alene bruges kun, hvis kortet ikke har nogen variant)
            new_price = str(prices.get((t, d)) or (prices.get((t, "")) if not d else None) or "")
        # engangsgebyrer i betingelserne: afleveringsgebyr og (hos Audi) stiftelsesomkostninger
        fee = sum(digits(m.group(1)) for m in re.finditer(r"(?:Afleveringsgebyr|Stiftelsesomkostninger):?\s*([\d.]+)", c["text"]))
        row = {"down_id": down_id, "model": c["title"], "variant": c["desc"], "monthly": f[monthly_key], "down": down,
               "months": f["Leasingperiode"], "km": f.get("Km pr. år", ""), "new_price": new_price, "fee": str(fee),
               "link": (LINK_URL or page.url).split("#")[0]}
        calc = digits(row["down"]) + digits(row["monthly"]) * digits(row["months"]) + fee
        if "Totalomkostninger" in f and calc != digits(f["Totalomkostninger"]):
            print(f"[{TAG}] ADVARSEL {row['model']} {row['variant']}: beregnet {calc} != side {f['Totalomkostninger']}")
        rows.append(row)
    return rows


FIELDS = {"annualKm": "Km pr. år", "downPayment": "Førstegangsydelse", "contractLength": "Leasingperiode"}
GROUPS = {"annualKm": "annualKilometer", "downPayment": "downPayment", "contractLength": "contractLength"}  # name-attribut på radioknapperne

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
        f"input[name='{GROUPS[group]}']", "els => els.map(e => parseInt(e.value)).filter(Number.isFinite)")
    return sorted(set(vals)) or fallback  # 0 er en gyldig værdi (fx 0 kr. i udbetaling)


async def set_radio(page, group, value, fast=False):
    """Vælger radioknappen <group>_<value>. Returnerer 'ok', 'mangler' eller 'fejl'."""
    rid, field, want = f"{group}_{value}", FIELDS[group], str(value)
    if not await page.evaluate("id => !!document.getElementById(id)", rid):
        return "mangler"
    for name, js in (HOWS[:2] if fast else HOWS):  # fast: færre forsøg og ingen diagnose, når et valg kan mangle med vilje
        try:
            if js:
                await page.evaluate(js, rid)
            elif name == "label":
                await page.locator(f"label[for='{rid}']").click(force=True, timeout=3000)
            else:
                await page.locator(f"#{rid}").check(force=True, timeout=3000)
            await page.wait_for_function(WAIT_JS, arg=[field, want], timeout=2500 if fast else 4000)
            await page.wait_for_timeout(600)  # lad resten af kortene nå at opdatere
            return "ok"
        except Exception:
            continue
    if not fast:
        print(f"[{TAG}] kunne ikke vælge {rid}. Status: {await page.evaluate(DIAG_JS, [rid, field])}")
    return "fejl"


def kr(n):
    if not n:
        return "ukendt nypris"
    return f"{int(n):,}".replace(",", ".") + " kr."


async def scrape_model(page, seen, out, extra_prices=None):
    """Henter alle kombinationer af leasingperiode, førstegangsydelse og km (de filtre siden har)."""
    combos, snaps = set(), []
    bases = {k[:5] for k in seen}
    prices = {**(extra_prices or {}), **await list_prices(page)}  # prisboksene på selve siden går forud
    print(f"[{TAG}] {len({k for k in prices if k[1]})} vejledende priser fundet i prisboksene")

    async def collect():
        rows = await read_cards(page, prices)
        for r in rows:
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
            combos.add((digits(r["months"]), digits(r["down"]), digits(r["km"])))
        return rows

    await collect()  # standardvisning først
    lengths = await options(page, "contractLength", [])  # tom liste = siden har intet filter for den dimension
    all_downs, all_kms, grid, worked = set(), set(), set(), False
    expected = 0

    async def km_pass(L, d, fast=False):
        """Læser alle kort for hvert km-trin i den aktuelle tilstand (d = valgt udbetaling, None = urørt)."""
        nonlocal expected
        kms = await options(page, "annualKm", [])
        expected += len(kms or [None])
        first = True
        for k in (kms or [None]):
            if k is not None:
                res = await set_radio(page, "annualKm", k, fast)
                if res == "fejl":
                    break  # siden reagerer ikke på km-valg i den aktuelle tilstand
                if res != "ok":
                    continue
            rows = await collect()
            grid.add((d, k)); all_downs.add(d); all_kms.add(k)
            if first:  # lille øjebliksbillede til fejlsøgning
                snaps.append((L, d, [(r["variant"], r["down"], r["down_id"], r["monthly"]) for r in rows])); first = False

    async def pick_length(L):
        nonlocal worked
        res = await set_radio(page, "contractLength", L)
        if res == "mangler":
            print(f"[{TAG}] radioknappen contractLength_{L} findes ikke")
        worked = worked or res == "ok"
        return res

    if lengths:
        # Fase 1: pr. leasingperiode med udbetalingsfilteret urørt. Siden viser så alle tilbud for det valgte km.
        for L in lengths:
            if await pick_length(L) == "ok":
                await km_pass(L, None)
    # Fase 2: udbetalinger (ved flere leasingperioder kun dem, der endnu ikke er set)
    for L in (lengths or [None]):
        if L is not None and await pick_length(L) != "ok":
            continue
        downs = await options(page, "downPayment", [])
        if lengths:
            seen_here = {dn for (m, dn, _) in combos if m == L}
            todo = [d for d in downs if d not in seen_here]
        else:
            todo = downs or [None]  # tom liste = siden har intet udbetalingsfilter
        for d in todo:
            if d is not None:
                res = await set_radio(page, "downPayment", d, fast=bool(lengths))
                if res == "mangler":
                    print(f"[{TAG}] radioknappen downPayment_{d} findes ikke"); continue
                if res == "fejl" and not worked and not lengths:
                    break  # mekanismen virker ikke på denne side - spar tid
                if res != "ok":
                    continue
                worked = True
            await km_pass(L, d, fast=bool(lengths) and d is not None)
    all_downs.discard(None); all_kms.discard(None)
    if lengths:
        per = {}
        for (m, _, _) in combos:
            per[m] = per.get(m, 0) + 1
        print(f"[{TAG}] kombinationer pr. løbetid (mdr.: antal): {dict(sorted(per.items()))}")
        expected = len(combos)  # antal kombinationer pr. løbetid kan ikke udledes af filtrene
    elif all_downs and all_kms:
        extras = {(dn, km) for (_, dn, km) in combos} - grid
        if extras:
            print(f"[{TAG}] kombinationer uden for filtrene (udbetaling, km): {sorted(extras)}")
    expected = max(1, expected)
    if len(combos) < expected:  # ufuldstændig: vis hvad siden faktisk viste
        for L, d, rows in snaps:
            print(f"[{TAG}] ved løbetid {L}, udbetaling {d} ses {len(rows)} kort (variant, vist, id, ydelse): {rows}")
    return sorted(all_downs), sorted(all_kms), combos, expected, lengths


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


async def scrape_one(page, item, seen, out):
    global LINK_URL
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
    downs, kms, combos, expected, lengths = await scrape_model(frame, seen, out, extra)
    fill_prices(out[before:])
    missing = sorted({f"{r['model']} {r['variant']}" for r in out[before:] if not r["new_price"]})
    if missing:
        print(f"[{TAG}] {name}: nypris mangler for {missing} - læg dem i new_prices.yaml")
    if SKIPPED:
        print(f"[{TAG}] {name}: sprang kortaflæsninger over (kort: antal): {dict(SKIPPED)}")
        WARNINGS.append(f"{name}: {sum(SKIPPED.values())} kortaflæsninger sprunget over")
        SKIPPED.clear()
    print(f"[{TAG}] {name}: {len(out) - before} tilbud, {len(combos)} af {expected} kombinationer "
          f"(løbetid {lengths or 'intet filter'}, udbetaling {downs or 'intet filter'}, km {kms or 'intet filter'})")


async def scrape_site(page, tag, urls):
    """urls: async funktion der returnerer [(navn, url[, prisside]), ...]. Fejler en model, fortsætter de øvrige."""
    global TAG
    TAG = tag
    WARNINGS.clear()
    SKIPPED.clear()
    seen, out = {}, []
    for item in await urls(page):
        try:
            await scrape_one(page, item, seen, out)
        except Exception as e:
            msg = f"{item[0]}: {(str(e).splitlines() or [repr(e)])[0][:150]}"
            print(f"[{TAG}] FEJL ved model {msg}")
            WARNINGS.append(msg)
            SKIPPED.clear()
    if not out:
        raise RuntimeError("ingen leasingtilbud fundet" + (f" ({'; '.join(WARNINGS)})" if WARNINGS else ""))
    return out
