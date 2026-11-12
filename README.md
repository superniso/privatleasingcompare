# Leasing-radar

Daglig scraping af privatleasing-tilbud -> statisk hjemmeside + mail ved prisændringer.

## Opsætning (GitHub, gratis)
1. Læg mappen i et nyt repo. Slå GitHub Pages til (Settings -> Pages -> Source: GitHub Actions).
2. Settings -> Secrets: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, MAIL_TO
   (Gmail: app-adgangskode; eller Brevo/Mailgun SMTP).
3. Udfyld `brands.yaml` pr. mærke: korrekt url, `card`-selector og felt-selectors.
   Mærker hvor der stadig står "TODO" springes over og vises som "Kunne ikke hentes".
   Sider der kræver mere end selectors: `scraper/custom/<id>.py`.
4. Mangler en leasingside nypris, så læg den i `new_prices.yaml`.
5. Kør workflowet manuelt (Actions -> Daglig opdatering -> Run). Derefter kører det kl. 05:00 UTC hver dag.

## Tilføj et mærke
Ny blok i `brands.yaml`. Ingen kodeændringer.

## Lokalt
pip install -r requirements.txt && playwright install chromium
python scraper/run.py && python -m http.server -d site
