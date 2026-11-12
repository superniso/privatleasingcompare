"""Volkswagen Danmark – privatleasing. Læser kortene (.sf-card-auto) på modelsiderne.
Nypris udledes af checkbox-id'et insuranceCheckbox_<nypris>_<førstegangsydelse>.
Ydelsen læses UDEN forsikring (så den kan sammenlignes med andre mærker)."""
import re

BASE = "https://www.volkswagen.dk"
OVERVIEW = BASE + "/app/da/leasingpriser/"
FALLBACK_SLUGS = ["id-tiguan"]          # tilføj flere modelsider her ved behov
KMS = [10000, 15000, 20000, 25000, 30000, 40000, 50000, 55000]   # bruges hvis siden ikke oplyser dem
DOWNS = [20000, 45000]


def digits(s):
    d = re.sub(r"\D", "", (s or "").split(",")[0])
    return int(d) if d else 0


async def discover(page):
    try:
        await page.goto(OVERVIEW, wait_until="networkidle", timeout=60000)
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        found = {m.group(1) for h in hrefs if (m := re.search(r"/prisliste/([^/]+)/", h or ""))}
    except Exception as e:
        print(f"[vw] kunne ikke finde modeller automatisk: {e}")
        found = set()
    return sorted(found | set(FALLBACK_SLUGS))


async def read_cards(page):
    rows = []
    for card in await page.query_selector_all(".sf-card-auto"):
        if not await card.is_visible():  # filtrerede/skjulte kort giver forkert tekst
            continue
        title = await card.query_selector(".sf-card-auto-title-item")
        desc = await card.query_selector(".sf-card-auto-description-item")
        box = await card.query_selector("input[id^='insuranceCheckbox_']")
        items = await card.query_selector_all(".sf-card-auto-financing-details-wrapper-item")
        if not (title and box and items):
            continue
        f = {}
        for it in items:
            s = await it.query_selector(".sf-card-auto-financing-details-wrapper-item-subject")
            d = await it.query_selector(".sf-card-auto-financing-details-wrapper-item-description")
            if s and d:
                f[(await s.inner_text()).strip()] = (await d.inner_text()).strip()
        if "Leasingperiode" not in f:
            continue
        monthly_key = next((k for k in f if k.startswith("Månedlig ydelse")), None)
        if not monthly_key or "inkl" in monthly_key:
            raise RuntimeError("kortet viser ydelse inkl. forsikring – afkryds forsikring fra før læsning")
        _, new_price, down = (await box.get_attribute("id")).split("_")
        text = await card.inner_text()
        fee = re.search(r"Afleveringsgebyr:\s*([\d.]+)", text)
        link = await card.query_selector("a.btn-secondary")
        row = {
            "model": (await title.inner_text()).strip(),
            "variant": (await desc.inner_text()).strip() if desc else "",
            "monthly": f[monthly_key], "down": down, "months": f["Leasingperiode"],
            "km": f.get("Km pr. år", ""), "new_price": new_price,
            "fee": fee.group(1) if fee else "0",
            "link": (await link.get_attribute("href")) if link else None,
        }
        calc = digits(row["down"]) + digits(row["monthly"]) * digits(row["months"]) + digits(row["fee"])
        if "Totalomkostninger" in f and calc != digits(f["Totalomkostninger"]):
            print(f"[vw] ADVARSEL {row['model']} {row['variant']}: beregnet {calc} != side {f['Totalomkostninger']}")
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
    print(f"[vw] kunne ikke vælge {rid}. Status: {await page.evaluate(DIAG_JS, [rid, field])}")
    return "fejl"


async def scrape_model(page, seen, out):
    """Henter alle kombinationer af førstegangsydelse og km på den aktuelle modelside."""
    combos = set()

    async def collect():
        for r in await read_cards(page):
            key = (r["model"], r["variant"], r["km"], r["down"], r["months"])
            if key not in seen:
                seen.add(key); out.append(r)
            combos.add((digits(r["down"]), digits(r["km"])))

    await collect()  # standardvisning først
    downs = await options(page, "downPayment", DOWNS)
    kms = await options(page, "annualKm", KMS)
    worked = False
    for d in downs:
        res = await set_radio(page, "downPayment", d)
        if res == "fejl" and not worked:
            break  # mekanismen virker ikke på denne side – spar tid
        if res != "ok":
            continue
        worked = True
        for k in kms:
            res = await set_radio(page, "annualKm", k)
            if res == "ok":
                await collect()
    return downs, kms, combos


async def scrape(page):
    seen, out = set(), []
    for slug in await discover(page):
        await page.goto(f"{BASE}/app/da/prisliste/{slug}/leasingpriser/", wait_until="networkidle", timeout=60000)
        await page.wait_for_selector(".sf-card-auto-financing-details-wrapper", timeout=30000)
        before = len(out)
        downs, kms, combos = await scrape_model(page, seen, out)
        print(f"[vw] {slug}: {len(out) - before} tilbud, {len(combos)} af {len(downs) * len(kms)} kombinationer "
              f"(udbetaling {downs}, km {kms})")
    if not out:
        raise RuntimeError("ingen leasingkort fundet")
    return out
