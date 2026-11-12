# Energovision B2C cenovka generator — Render deploy

Webhook server ktorý generuje PDF cenovej ponuky z Notion.

## Endpointy

- `GET /health` — healthcheck
- `POST /webhook/prepocet` — Notion Button "🔄 Prepočítaj cenu"
- `POST /webhook/generate-pdf` — Notion Button "🖨 Vytlač ponuku"

## POST /webhook/generuj-dokumenty-inline (zmluva, splnomocnenie, GDPR súhlas)

Pre server-to-server volajúceho (partnerský portál), ktorý má dáta u seba. Vyplní **tú istú šablónu tou istou
funkciou** (`naplnif_zmluvu` / `naplnif_splnomocnenie` / `naplnif_gdpr` zo `templates_zmluvy/`) ako CRM, v rámci
požiadavky ju skonvertuje do PDF a vráti DOCX aj PDF v odpovedi. **Nič neukladá**, nevolá Notion, Supabase ani žiadnu
URL od volajúceho a nelogguje payload (len druh, veľkosti, trvanie, skrátené hashe).

Implementácia: `inline_documents.py`. Kontrakt (zdieľaný s portálom): `tests/fixtures/inline_documents_contract.json`.

**Autentifikácia (fail-closed):** hlavička `X-Webhook-Secret` = `WEBHOOK_SECRET`. Bez nastaveného `WEBHOOK_SECRET`
vráti route 503, pri chýbajúcej/zlej hlavičke 401 (porovnanie v konštantnom čase, pred čítaním tela).

**Požiadavka** (max. 16 KiB, JSON):

```json
{"kind": "zmluva" | "splnomocnenie" | "gdpr", "lead_data": {...}, "allow_docx_only": false}
```

Polia `lead_data` (neznáme polia a polia z iného druhu dokumentu = 422; dátumy `DD.MM.RRRR`, texty bez `XXX`,
`___`, `....` a bez riadiacich znakov, čísla najviac 2 desatinné miesta):

| druh | povinné | voliteľné |
|---|---|---|
| `zmluva` | `meno_priezvisko`, `adresa`, `telefon`, `email`, `vykon_kwp` (0,1–1000), `cislo_cp`, `datum_cp`, `miesto_vykonu`, `cena_eur` (s DPH, 1–5 000 000), `datum_dnes`, `payment_terms` (`60_30_10`\|`60_40`\|`50_50`\|`30_70`) | buď `panel_sku` (`PAN-001`\|`PAN-002`), alebo OBE `zaruka_panely_produkt` (2–40), `zaruka_panely_linear` (2–50) — nie oboje, nie ani jedno |
| `splnomocnenie` | `meno_priezvisko`, `cislo_op`, `datum_narodenia`, `datum_dnes` | `trvale_bydlisko` a/alebo `adresa` (aspoň jedno) |
| `gdpr` | `meno_priezvisko`, `datum_narodenia`, `datum_dnes` | — |

Cena slovom sa odvodzuje z `cena_eur`; záruky zo `panel_sku` podľa rovnakého pravidla ako
`/webhook/generuj-dokumenty-supabase` (`generuj_dokumenty.panel_params_from_sku`). Volajúci posiela hodnoty tak,
ako majú byť v dokumente; generátor nič nedopočítava ani nedoplňuje predvolenými hodnotami.

**Odpoveď 200:** `{ok, kind, filename, docx_base64, pdf_base64, docx_sha256, pdf_sha256, converter, template_sha256}`.
`filename` je názov DOCX (`Zmluva_o_dielo.docx`, `Splnomocnenie.docx`, `GDPR_suhlas.docx`, bez osobných údajov);
`converter` je `gotenberg-libreoffice` (ak je `GOTENBERG_URL`) alebo `libreoffice` (lokálny `soffice`). Stratový
fallback (mammoth/WeasyPrint) sa pre právne dokumenty nepoužíva: bez verného konvertora je odpoveď **503**
`pdf_converter_unavailable`; s `"allow_docx_only": true` vráti 200 s `pdf_base64`, `pdf_sha256`, `converter` = `null`.

**Chyby:** 401 `unauthorized`, 503 `server misconfigured`, 413 `payload_too_large`,
422 `validation_failed` (`fields`: meno poľa → správa; hodnoty sa neopisujú),
422 `document_incomplete` (`problems`: po vyplnení ostala značka šablóny alebo sa odoslaná hodnota v dokumente
nenašla — napr. šablóna má menej značiek než hodnôt), 500 `render_failed`.

**Čo musí volajúci poslať:** hotové hodnoty podľa tabuľky vyššie (už s DPH po zľave, dátumy `DD.MM.RRRR`) a po
prijatí overiť, že `pdf_base64` začína `%PDF-`, `kind` sedí a SHA-256 zodpovedajú bajtom. Produkcia vyžaduje
`WEBHOOK_SECRET` a verný konvertor (`GOTENBERG_URL` [+ `GOTENBERG_USER`/`GOTENBERG_PASS`] alebo `soffice` v image).

Testy: `python3 -m pytest -q -o addopts="" tests/test_inline_documents.py tests/test_inline_documents_app.py`
(CI job `inline-documents-tests`).

## Deploy na Render

1. **Push do GitHub** repo (verejný alebo súkromný):
   ```
   git init
   git add .
   git commit -m "initial deploy"
   git remote add origin git@github.com:lukasbago/energovision-cp-generator.git
   git push -u origin main
   ```

2. **Render dashboard** → New → Web Service → Connect repo
3. Render automaticky deteguje `render.yaml` a postaví službu

4. **Env premenné** v Render dashboard:
   - `NOTION_TOKEN` — secret token z https://www.notion.so/my-integrations
   - `NOTION_DATABASE_ID` — `ba7a1d6c-63a9-43da-b66d-2b1c7e8660da`
   - `WEBHOOK_SECRET` — auto-vygenerovaný

## Notion integrácia

1. Vytvor integráciu na https://www.notion.so/my-integrations
2. Pridaj do nej "Read content" + "Update content" + "Insert content" capabilities
3. V Notion DB "Zákazníci B2C" → ⋯ → Connect to → vyber tvoju integráciu
4. Skopíruj **Internal Integration Secret** do Render env `NOTION_TOKEN`

## Notion Buttons

V DB "Zákazníci B2C" pridaj 2 button properties:

**🔄 Prepočítaj cenu**
- Type: Button
- Action: Open URL
- URL: `https://YOUR-RENDER-URL.onrender.com/webhook/prepocet`
- Method: POST
- Body: `{"page_id": "{{page.id}}"}`
- Headers: `X-Webhook-Secret: <secret z Render env>`

**🖨 Vytlač ponuku (Variant A)**
- Type: Button
- URL: `https://YOUR-RENDER-URL.onrender.com/webhook/generate-pdf`
- Method: POST
- Body: `{"page_id": "{{page.id}}", "variant": "A"}`

## Lokálny test

```bash
pip install -r requirements.txt
export NOTION_TOKEN=secret_xxx
python3 app.py
# Server beží na http://localhost:5000
```

## Náklady

- Render Starter: $7/mes (always-on, 512 MB RAM)
- Free plán spí po 15 min nečinnosti — prvý request po prebudení = 30 sec delay
