"""Tesla Danmark - privatleasing (Model Y). Læser den estimerede månedlige pris i leasing-formularen
på designsiden for hver kombination af version, udbetaling og km pr. år.
Brændstofbesparelser er slået FRA, så prisen kan sammenlignes med andre mærker."""
import re

BASE = "https://www.tesla.com/da_dk"
MODELS = {"modely": "Model Y"}   # URL-slug -> modelnavn. Tilføj fx "model3": "Model 3" når siden er undersøgt
OPEN_FINANCE = re.compile(r"leasing|finansier|betalings|månedlig|beregn", re.I)
FEE = 600                        # tilbageleveringsgebyr. Medregnes i totalen (som hos VW). Sæt til 0 for at udelade det
DOWNS = [20000, 50000]           # bruges kun hvis siden ikke oplyser valgmulighederne
KMS = [10000, 15000, 20000, 25000, 30000, 35000]


def digits(s):
    d = re.sub(r"\D", "", (s or "").split(",")[0])
    return int(d) if d else 0


TRIMS_JS = r"""() => [...document.querySelectorAll('input[name="trim-category-COMBINED_TRIMS"]')].map((inp, i) => {
  const lab = document.querySelector('label[for="' + inp.id + '"]');
  const t = el => el ? el.textContent.replace(/\u00a0/g, ' ').trim() : '';
  const price = el => t(el && el.querySelector('[data-id$="-price"], .options-group--category-price'));
  return { i, main: t(lab.querySelector('.group--options_block--name--main')),
    sec: t(lab.querySelector('.group--options_block--name--secondary')),
    cap: t(lab.querySelector('.options-group--caption-inner')), price: price(lab),
    subs: [...lab.querySelectorAll('input[name="OptionChoice:TRIM"]')].map(s => {
      const sl = lab.querySelector('label[for="' + s.id + '"]');
      return { id: s.id, name: t(sl && sl.querySelector('.text-loader--content')), price: price(sl) }; }) };
})"""


async def accept_cookies(page):
    try:
        await page.get_by_role("button", name=re.compile(r"accept|accepter|tillad alle", re.I)).first.click(timeout=2000)
    except Exception:
        pass


async def click_input(page, input_id, corner=False):
    """Klik på label til en (skjult) radioknap. corner=True rammer hjørnet, så indlejrede valg ikke rammes."""
    try:
        lab = page.locator(f'label[for="{input_id}"]').first
        await lab.click(timeout=3000, **({"position": {"x": 12, "y": 12}} if corner else {"force": True}))
    except Exception:
        await page.evaluate("id => document.getElementById(id).click()", input_id)
    await page.wait_for_timeout(500)


async def pick_trim(page, v):
    ids = await page.eval_on_selector_all('input[name="trim-category-COMBINED_TRIMS"]', "els => els.map(e => e.id)")
    await click_input(page, ids[v["group"]], corner=True)
    if v["sub"]:
        await click_input(page, v["sub"])


async def open_finance(page):
    form = page.locator("form.financial-modal-grid").first
    if await form.count() and await form.is_visible():
        return True
    try:  # knappen i bunden af siden med prisen ("2.895 kr /md.")
        await page.locator("button.modal-trigger").first.click(timeout=3000)
        await page.wait_for_selector("form.financial-modal-grid", state="visible", timeout=5000)
        return True
    except Exception:
        pass
    cands = page.get_by_role("button", name=OPEN_FINANCE)
    for i in range(min(await cands.count(), 6)):
        try:
            await cands.nth(i).click(timeout=2000)
            await page.wait_for_selector("form.financial-modal-grid", state="visible", timeout=4000)
            return True
        except Exception:
            continue
    try:  # evt. fane i dialogen
        await page.get_by_text(re.compile(r"^leasing$", re.I)).first.click(timeout=2000)
        await page.wait_for_selector("form.financial-modal-grid", state="visible", timeout=4000)
        return True
    except Exception:
        return False


async def dd_values(page, fid):
    return await page.eval_on_selector_all(
        f"#{fid}-listbox li[data-tds-value]", "els => els.map(e => parseInt(e.dataset.tdsValue))")


async def dd_select(page, fid, value):
    """Vælger en værdi i Teslas egen rullemenu. Returnerer True hvis menuen viser værdien bagefter."""
    li = f'#{fid}-listbox li[data-tds-value="{value}"]'
    try:
        await page.locator(f"#{fid} .tds-dropdown-trigger").click(timeout=3000)
        await page.locator(li).click(timeout=3000)
    except Exception:
        await page.evaluate("s => document.querySelector(s).click()", li)
    await page.wait_for_timeout(300)
    shown = await page.locator(f"#{fid} .tds-dropdown-trigger").inner_text()
    return digits(shown) == value


async def read_price(page):
    t = await page.locator('[data-id="finance-modal-price-section"] .price-tag').first.inner_text()
    return digits(t.split("/")[0])


async def settled_price(page, prev):
    """Venter til prisen er holdt op med at ændre sig (siden regner async)."""
    last, t = None, 0
    while t < 5000:
        await page.wait_for_timeout(400); t += 400
        cur = await read_price(page)
        if cur == last and (cur != prev or t >= 2000):
            return cur
        last = cur
    return last


async def footer_price(page):
    """Købspris for den valgte version, som den vises i bunden af siden ('364.990 kr Købspris')."""
    try:
        t = await page.locator('[data-id="footer-price-disclaimer"]').first.inner_text(timeout=2000)
        return digits(t.split("Købspris")[0])
    except Exception:
        return 0


async def diagnose(page):
    try:
        btns = [b.strip() for b in await page.locator("button:visible").all_inner_texts() if b.strip()][:40]
        print(f"[tesla] side: {await page.title()!r}. Synlige knapper: {btns}")
    except Exception:
        pass


async def scrape_page(page, slug, model):
    out = []
    groups = await page.evaluate(TRIMS_JS)
    variants = []
    for g in groups:
        if g["subs"]:
            variants += [{"group": g["i"], "sub": s["id"], "variant": f'{g["sec"]} {s["name"]}'.strip(),
                          "price": digits(s["price"])} for s in g["subs"]]
        else:
            variants.append({"group": g["i"], "sub": None, "variant": f'{g["sec"]} {g["cap"]}'.strip(),
                             "price": digits(g["price"])})
    print(f"[tesla] {slug}: {len(variants)} versioner: {[(v['variant'], v['price']) for v in variants]}")
    for n, v in enumerate(variants):
        await page.keyboard.press("Escape")
        await pick_trim(page, v)
        for _ in range(6):  # vent til bundlinjen viser den valgte version
            if await footer_price(page) == v["price"]:
                break
            await page.wait_for_timeout(500)
        price = await footer_price(page) or v["price"]
        if price != v["price"]:
            print(f"[tesla] {v['variant']}: nypris {v['price']} i listen, men {price} i bundlinjen - bruger {price}")
        if not await open_finance(page):
            await diagnose(page)
            if n == 0:
                raise RuntimeError("kunne ikke åbne leasing-formularen")
            continue
        box = page.locator('input[name="SAVINGS"]')
        if await box.count() and await box.first.is_checked():
            await page.locator('label[for="' + await box.first.get_attribute("id") + '"]').first.click(force=True)
        months = digits(await page.input_value("#term")) or 36
        downs = await dd_values(page, "aldDownPaymentValue") or DOWNS
        kms = await dd_values(page, "distance") or KMS
        prev, got = await read_price(page), 0
        for d in downs:
            if not await dd_select(page, "aldDownPaymentValue", d):
                print(f"[tesla] kunne ikke vælge udbetaling {d}"); continue
            for k in kms:
                if not await dd_select(page, "distance", k):
                    print(f"[tesla] kunne ikke vælge {k} km"); continue
                prev = await settled_price(page, prev)
                out.append({"model": model, "variant": v["variant"], "monthly": str(prev), "down": str(d),
                            "months": str(months), "km": f"{k:,}".replace(",", "."), "new_price": str(price),
                            "fee": str(FEE), "link": f"{BASE}/{slug}/design"})
                got += 1
        print(f"[tesla] {v['variant']}: {got} af {len(downs) * len(kms)} kombinationer")
    return out


async def scrape(page):
    out = []
    for slug, model in MODELS.items():
        await page.goto(f"{BASE}/{slug}/design", wait_until="domcontentloaded", timeout=60000)
        if "denied" in (await page.title()).lower():
            raise RuntimeError("Tesla afviser forespørgslen (Access Denied) - sandsynligvis blokering af serverens IP")
        await accept_cookies(page)
        await page.wait_for_selector('input[name="trim-category-COMBINED_TRIMS"]', timeout=30000)
        out += await scrape_page(page, slug, model)
    if not out:
        raise RuntimeError("ingen priser fundet")
    return out
