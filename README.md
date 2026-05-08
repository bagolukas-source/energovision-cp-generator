# Energovision B2C cenovka generator — Render deploy

Webhook server ktorý generuje PDF cenovej ponuky z Notion.

## Endpointy

- `GET /health` — healthcheck
- `POST /webhook/prepocet` — Notion Button "🔄 Prepočítaj cenu"
- `POST /webhook/generate-pdf` — Notion Button "🖨 Vytlač ponuku"

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
