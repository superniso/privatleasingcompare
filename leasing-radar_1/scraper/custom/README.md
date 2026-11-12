Valgfri: en fil pr. mærke (<id fra brands.yaml>.py) overstyrer den generiske scraper.

    async def scrape(page):
        await page.goto("https://...")
        return [{"model": "ID.3", "variant": "Pro", "monthly": "3.499 kr.", "down": "29.999",
                 "months": "36", "km": "15000", "new_price": "329.995", "link": "/..."}]
