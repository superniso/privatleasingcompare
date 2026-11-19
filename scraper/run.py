"""Henter privatleasing-tilbud, beregner nøgletal, finder prisændringer og sender mail."""
import asyncio, importlib.util, json, os, pathlib, re, smtplib, sys, datetime as dt
from email.message import EmailMessage
import yaml
from playwright.async_api import async_playwright

sys.stdout.reconfigure(line_buffering=True)  # log og fejl i den rækkefølge, de sker


class NotConfigured(RuntimeError):
    pass


ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
NORM_BELOW = 36        # aftaler kortere end dette (fx 12 mdr.) omregnes
NORM_TERMS = (36, 48)  # ...til gennemsnit af disse løbetider
GOOD_RATIO = float(os.getenv("GOOD_RATIO", "0.50"))  # godt tilbud: samlet tilbagebetaling < 50 % af nypris


def num(s):
    if s in (None, ""):
        return None
    s = str(s).split(",")[0].replace(".", "")
    d = re.sub(r"\D", "", s)
    return int(d) if d else None


async def generic(page, b, cfg):
    await page.goto(b["url"], wait_until="networkidle", timeout=60000)
    rows = []
    for card in await page.query_selector_all(b["card"]):
        row = {}
        for key, sel in b["fields"].items():
            if not sel:
                continue
            el = await card.query_selector(sel)
            if not el:
                continue
            row[key] = (await el.get_attribute("href")) if key == "link" else (await el.inner_text()).strip()
        rows.append(row)
    return rows


async def scrape_brand(browser, b, cfg):
    custom = ROOT / "scraper" / "custom" / f"{b['id']}.py"
    page = await browser.new_page(locale="da-DK")
    try:
        if custom.exists():
            spec = importlib.util.spec_from_file_location(b["id"], custom)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            rows = await mod.scrape(page)
            return rows, list(getattr(getattr(mod, "S", None), "WARNINGS", []))  # advarsler fra delte scraper
        if "TODO" in (b["card"], *b["fields"].values()):
            raise NotConfigured("selectors er ikke udfyldt endnu")
        return await generic(page, b, cfg), []
    finally:
        await page.close()


def build(b, raw, cfg, new_prices):
    out = []
    for r in raw:
        model = r.get("model", "").strip()
        variant = r.get("variant", "").strip()
        monthly, down = num(r.get("monthly")), num(r.get("down")) or 0
        months = num(r.get("months")) or cfg["default_months"]
        km = num(r.get("km")) or cfg["default_km"]
        new_price = (num(r.get("new_price"))
                     or new_prices.get(f"{b['id']}|{model} {variant}".strip())
                     or new_prices.get(f"{b['id']}|{model}"))
        if not model or not monthly:
            continue
        fee = num(r.get("fee")) or 0  # afleveringsgebyr
        total = down + monthly * months + fee
        ratio, basis, terms = None, "faktisk", None
        if new_price:
            if months < NORM_BELOW:
                # Korte aftaler ser kunstigt billige ud. Antag samme udbetaling og ydelse over 36 og 48 mdr.
                # og brug gennemsnittet af de to andele.
                terms = {str(n): round((down + monthly * n + fee) / new_price, 4) for n in NORM_TERMS}
                ratio, basis = round(sum(terms.values()) / len(terms), 4), "omregnet"
            else:
                ratio = round(total / new_price, 4)
        link = r.get("link") or b["url"]
        if link.startswith("/"):
            link = re.match(r"https?://[^/]+", b["url"]).group(0) + link
        out.append({
            "id": f"{b['id']}|{model}|{variant}|{months}|{km}|{down}",
            "brand": b["name"], "brand_id": b["id"], "model": model, "variant": variant,
            "monthly": monthly, "down": down, "months": months, "km": km,
            "total": total, "new_price": new_price, "ratio": ratio,
            "ratio_basis": basis, "ratio_terms": terms,
            "good": ratio is not None and ratio < GOOD_RATIO, "link": link,
        })
    return out


def send_mail(changes):
    host, to = os.getenv("SMTP_HOST"), os.getenv("MAIL_TO")
    if not (host and to):
        print("SMTP_HOST/MAIL_TO mangler - springer mail over"); return
    lines = [f"{c['brand']} {c['model']} {c['variant']}: {c['old']} -> {c['new']} kr./md. "
             f"({c['new'] - c['old']:+d})  {c['link']}" for c in changes]
    msg = EmailMessage()
    msg["Subject"] = f"Leasing: {len(changes)} prisændring(er)"
    msg["From"] = os.getenv("SMTP_USER", to); msg["To"] = to
    msg.set_content("\n".join(lines))
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT") or "587")) as s:
        s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)


async def main():
    cfg = yaml.safe_load((ROOT / "brands.yaml").read_text())
    new_prices = yaml.safe_load((ROOT / "new_prices.yaml").read_text()) or {}
    old_path = DATA / "offers.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {"offers": []}
    old_by_id = {o["id"]: o for o in old["offers"]}

    offers, status = [], {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for b in cfg["brands"]:
            try:
                raw, warns = await scrape_brand(browser, b, cfg)
                got = build(b, raw, cfg, new_prices)
                if warns:  # delvist: behold tidligere tilbud, der ikke blev set denne gang
                    seen_ids = {g["id"] for g in got}
                    got += [{**o, "stale": True} for o in old["offers"] if o["brand_id"] == b["id"] and o["id"] not in seen_ids]
                offers += got
                fresh = sum(1 for g in got if not g.get("stale"))
                status[b["id"]] = {"name": b["name"], "state": "delvist" if warns else "ok", "ok": True,
                                   "count": fresh, "warnings": warns[:5]}
                print(f"[{b['id']}] {'DELVIST' if warns else 'OK'}: {fresh} tilbud hentet"
                      + (f", {len(got) - fresh} beholdt fra tidligere kørsler" if len(got) > fresh else ""))
            except NotConfigured:
                status[b["id"]] = {"name": b["name"], "state": "ikke_sat_op", "ok": False}
                print(f"[{b['id']}] ikke sat op endnu")
            except Exception as e:  # behold tidligere data for mærker der fejler
                carried = [{**o, "stale": True} for o in old["offers"] if o["brand_id"] == b["id"]]
                offers += carried
                err = (str(e) or repr(e))[:300]
                status[b["id"]] = {"name": b["name"], "state": "fejl", "ok": False, "error": err, "carried": len(carried)}
                print(f"[{b['id']}] FEJL: {err} - beholder {len(carried)} tilbud fra tidligere kørsler")
        await browser.close()

    changes = []
    for o in offers:
        prev = old_by_id.get(o["id"])
        if prev and prev["monthly"] != o["monthly"]:
            changes.append({**o, "old": prev["monthly"], "new": o["monthly"]})
        o["first_seen"] = prev["first_seen"] if prev else dt.date.today().isoformat()
        o["prev_monthly"] = prev["monthly"] if prev and prev["monthly"] != o["monthly"] else (prev or {}).get("prev_monthly")

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    for o in offers:  # tilbud hentet i denne kørsel får tidsstempel; beholdte tilbud (stale) bevarer deres gamle
        if not o.get("stale"):
            o["fetched"] = now_iso
    # Prishistorik: én post pr. bil/konfiguration. En ny pris tilføjes, den gamle bliver stående.
    DATA.mkdir(parents=True, exist_ok=True)
    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {}
    today = dt.date.today().isoformat()
    for o in offers:
        if o.get("stale"):
            continue
        h = hist.get(o["id"])
        h = {"prices": h} if isinstance(h, list) else (h or {"prices": []})  # migrér gammelt format
        h.update({k: o[k] for k in ("brand", "brand_id", "model", "variant", "km", "down", "months", "link")})
        np = o.get("new_price")
        if np:
            h["new_price"] = np
        h["last_seen"] = today
        last = h["prices"][-1] if h["prices"] else None
        # Ny post ved ændret månedlig ydelse eller ændret nypris: [dato, pr. md., samlet, nypris]
        if last is None or last[1] != o["monthly"] or (np and len(last) > 3 and last[3] and last[3] != np):
            h["prices"].append([today, o["monthly"], o["total"], np])
        elif np and (len(last) < 4 or not last[3]):  # ældre poster uden nypris: udfyld seneste post
            while len(last) < 3:
                last.append(None)
            last[3:] = [np]
        hist[o["id"]] = h
        lc = h["prices"][0][0]  # dato for seneste ændring af den månedlige ydelse
        for a, b in zip(h["prices"], h["prices"][1:]):
            if a[1] != b[1]:
                lc = b[0]
        o["last_changed"] = lc
    hist_path.write_text(json.dumps(hist, ensure_ascii=False))
    for bid, st in status.items():  # hvornår blev mærket senest hentet korrekt?
        if st["state"] in ("ok", "delvist"):
            st["last_ok"] = today
        elif st["state"] == "fejl":
            dates = [h.get("last_seen") for h in hist.values() if isinstance(h, dict) and h.get("brand_id") == bid and h.get("last_seen")]
            st["last_ok"] = max(dates) if dates else (old.get("status", {}).get(bid) or {}).get("last_ok")
    print("\n=== Resultat pr. mærke ===")
    for bid, st in status.items():
        txt = {"ok": f"OK, {st.get('count')} tilbud", "delvist": f"DELVIST, {st.get('count')} tilbud (fejl: {'; '.join(st.get('warnings', []))})",
               "fejl": f"FEJL: {st.get('error')} (seneste vellykkede hentning: {st.get('last_ok') or 'ukendt'}, {st.get('carried', 0)} tilbud vises)",
               "ikke_sat_op": "ikke sat op endnu"}[st["state"]]
        print(f"  {st['name']}: {txt}")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "offers.json").write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "status": status, "offers": offers}, ensure_ascii=False, indent=1))
    if changes:
        send_mail(changes)
    fresh = sum(1 for o in offers if not o.get("stale"))
    print(f"{len(offers)} tilbud: {fresh} hentet i denne kørsel, {len(offers) - fresh} beholdt fra tidligere kørsler. {len(changes)} prisændringer")


asyncio.run(main())
