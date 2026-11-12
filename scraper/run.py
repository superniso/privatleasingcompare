"""Henter privatleasing-tilbud, beregner nøgletal, finder prisændringer og sender mail."""
import asyncio, importlib.util, json, os, pathlib, re, smtplib, sys, datetime as dt
from email.message import EmailMessage
import yaml
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
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
            return await mod.scrape(page)
        if "TODO" in (b["card"], *b["fields"].values()):
            raise RuntimeError("selectors er ikke udfyldt endnu")
        return await generic(page, b, cfg)
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
        total = down + monthly * months + (num(r.get("fee")) or 0)  # fee = afleveringsgebyr
        ratio = round(total / new_price, 4) if new_price else None
        link = r.get("link") or b["url"]
        if link.startswith("/"):
            link = re.match(r"https?://[^/]+", b["url"]).group(0) + link
        out.append({
            "id": f"{b['id']}|{model}|{variant}|{months}|{km}|{down}",
            "brand": b["name"], "brand_id": b["id"], "model": model, "variant": variant,
            "monthly": monthly, "down": down, "months": months, "km": km,
            "total": total, "new_price": new_price, "ratio": ratio,
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
                got = build(b, await scrape_brand(browser, b, cfg), cfg, new_prices)
                offers += got
                status[b["id"]] = {"ok": True, "count": len(got)}
            except Exception as e:  # behold gårsdagens data for mærker der fejler
                offers += [{**o, "stale": True} for o in old["offers"] if o["brand_id"] == b["id"]]
                status[b["id"]] = {"ok": False, "error": str(e)[:200]}
                print(f"[{b['id']}] {e}", file=sys.stderr)
        await browser.close()

    changes = []
    for o in offers:
        prev = old_by_id.get(o["id"])
        if prev and prev["monthly"] != o["monthly"]:
            changes.append({**o, "old": prev["monthly"], "new": o["monthly"]})
        o["first_seen"] = prev["first_seen"] if prev else dt.date.today().isoformat()
        o["prev_monthly"] = prev["monthly"] if prev and prev["monthly"] != o["monthly"] else (prev or {}).get("prev_monthly")

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "offers.json").write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "status": status, "offers": offers}, ensure_ascii=False, indent=1))
    # Prishistorik: én post pr. bil/konfiguration. En ny pris tilføjes, den gamle bliver stående.
    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {}
    today = dt.date.today().isoformat()
    for o in offers:
        if o.get("stale"):
            continue
        h = hist.get(o["id"])
        h = {"prices": h} if isinstance(h, list) else (h or {"prices": []})  # migrér gammelt format
        h.update({k: o[k] for k in ("brand", "brand_id", "model", "variant", "km", "down", "months", "new_price", "link")})
        h["last_seen"] = today
        if not h["prices"] or h["prices"][-1][1] != o["monthly"]:
            h["prices"].append([today, o["monthly"], o["total"]])  # [dato, pr. md., samlet]
        hist[o["id"]] = h
    hist_path.write_text(json.dumps(hist, ensure_ascii=False))
    if changes:
        send_mail(changes)
    print(f"{len(offers)} tilbud, {len(changes)} prisændringer")


asyncio.run(main())
