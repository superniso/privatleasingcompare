"""Volkswagen Danmark – privatleasing. Læser kortene (.sf-card-auto) på modelsiderne.
Nypris udledes af checkbox-id'et insuranceCheckbox_<nypris>_<førstegangsydelse>.
Ydelsen læses UDEN forsikring (så den kan sammenlignes med andre mærker)."""
import re

BASE = "https://www.volkswagen.dk"
OVERVIEW = BASE + "/app/da/leasingpriser/"
FALLBACK_SLUGS = ["id-tiguan"]          # tilføj flere modelsider her ved behov
KMS = [10000, 15000, 20000, 25000, 30000, 40000, 50000, 55000]


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


WAIT_JS = """km => {
  const cards = [...document.querySelectorAll('.sf-card-auto')]
    .filter(c => c.querySelector('.sf-card-auto-financing-details-wrapper'));
  return cards.length > 0 && cards.every(c => {
    const it = [...c.querySelectorAll('.sf-card-auto-financing-details-wrapper-item')]
      .find(i => i.textContent.includes('Km pr. år'));
    const d = it && it.querySelector('.sf-card-auto-financing-details-wrapper-item-description');
    return d && d.textContent.trim() === km;
  });
}"""


async def set_km(page, km):
    """Vælger radioknappen annualKm_<km> og venter til alle kort viser den valgte km."""
    label = f"{km:,}".replace(",", ".")
    try:
        lab = page.locator(f"label[for='annualKm_{km}']")
        if not await lab.is_visible():  # filteret kan ligge bag en Filter-knap
            await page.get_by_role("button", name=re.compile("Filter")).first.click(timeout=2000)
        await lab.click(timeout=3000)
        await page.wait_for_function(WAIT_JS, arg=label, timeout=8000)
        return True
    except Exception as e:
        print(f"[vw] kunne ikke vælge {label} km: {str(e).splitlines()[0][:120]}")
        return False


async def scrape(page):
    seen, out = set(), []
    for slug in await discover(page):
        await page.goto(f"{BASE}/app/da/prisliste/{slug}/leasingpriser/", wait_until="networkidle", timeout=60000)
        await page.wait_for_selector(".sf-card-auto-financing-details-wrapper", timeout=30000)
        got_km = set()
        for km in KMS:
            if not await set_km(page, km):
                continue
            for r in await read_cards(page):
                key = (r["model"], r["variant"], r["km"], r["down"], r["months"])
                if key not in seen:
                    seen.add(key); out.append(r); got_km.add(r["km"])
        print(f"[vw] {slug}: {len(got_km)} km-trin hentet")
    if not out:
        raise RuntimeError("ingen leasingkort fundet")
    return out
