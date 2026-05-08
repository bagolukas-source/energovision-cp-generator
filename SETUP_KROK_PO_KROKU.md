# Setup: Notion Button → Render → PDF

Tento návod prevedie Lukáša celým setupom. Ide krok-po-kroku v Chrome.

---

## ✅ Stav (čo už máme)

- [x] Render účet (workspace **Energovision**)
- [x] Notion Internal Integration token (uložené v Render env iba)
- [x] Deploy balík (12 súborov v `V2/render_deploy/`)

---

## 1. GitHub účet (~ 2 min)

1. Otvor https://github.com/signup
2. Klikni **Continue with Google** (najrýchlejšie) — alebo email+heslo
3. Username návrh: `lukas-energovision` (krátke, identifikuje firmu)
4. Country: **Slovakia**
5. Verify cez email link

---

## 2. Vytvor GitHub repo (~ 1 min)

1. Po prihlásení klik **"+"** vpravo hore → **New repository**
2. Repository name: `energovision-cp-generator`
3. Description: `B2C cenovka generator pre Notion → PDF`
4. **Public** (zadarmo, viditeľné len pri vyhľadaní)
5. Add README ❌ (necháme prázdny)
6. Add .gitignore ❌
7. Klik **Create repository**

---

## 3. Upload deploy súborov (drag&drop) (~ 2 min)

1. Na novom prázdnom repo strane → **"uploading an existing file"** link
2. **Drag&drop** všetky súbory z `V2/render_deploy/`:
   - `app.py`
   - `Procfile`
   - `render.yaml`
   - `requirements.txt`
   - `README.md`
   - `.gitignore`
   - `generate_cp.py`
   - `generate_cp_html.py`
   - `generate_from_notion.py`
   - `cp_template.html`
   - `energovision_header.png`
   - `energovision_footer.png`
   - `Cennik_v2.xlsx`
3. **Commit message:** "initial deploy"
4. Klik **Commit changes**

---

## 4. Render: Connect GitHub repo (~ 3 min)

1. Otvor https://dashboard.render.com (už si prihlásený)
2. Klik **+ New** vpravo hore → **Web Service**
3. **Connect a repository** → Connect GitHub → autorizuj Render
4. Vyber **energovision-cp-generator** repo
5. Render automaticky deteguje `render.yaml` a navrhne setup
6. **Region:** Frankfurt (najbližšie k SR)
7. **Plan:** Free na štart (po teste prepneme na Starter $7)
8. Klik **Create Web Service**

Render začne build — trvá ~ 3-5 minút (inštalácia Python deps + WeasyPrint).

---

## 5. Render: Pridaj env premenné (~ 1 min)

Počas builduje pridaj do **Environment** tab:

| Key | Value |
|---|---|
| `NOTION_TOKEN` | `ntn_L7262771875futgaE7Y6E7iVsbTKtijcOthfgjXjtwefLe` (z Lukášovho tokenu) |
| `NOTION_DATABASE_ID` | `ba7a1d6c-63a9-43da-b66d-2b1c7e8660da` |
| `WEBHOOK_SECRET` | (auto-vygenerovaný — zostane) |

Po pridaní → klik **Save**, Render redeployne.

---

## 6. Test deploya — healthcheck

Po dokončení buildu Render ti dá URL ako:
`https://energovision-cp-generator.onrender.com`

Otvor v Chrome:
- `https://energovision-cp-generator.onrender.com/health`

Mal by si vidieť:
```json
{
  "status": "ok",
  "service": "energovision-cp-generator",
  "notion_token_set": true
}
```

✅ Ak áno — **server beží**.
❌ Ak `notion_token_set: false` → znova pridaj NOTION_TOKEN env premennú.

---

## 7. Notion Integration: pripoj integráciu k DB (~ 1 min)

Aby webhook server mohol čítať/písať do tvojej Notion DB:

1. Otvor https://www.notion.so/my-integrations
2. Vidíš svoju integráciu (tú, z ktorej je token `ntn_...`)
3. Skontroluj capabilities: **Read content + Update content + Insert content** ✅
4. Otvor Notion DB **Zákazníci B2C**
5. Pravý horný roh → **⋯** → **Connect to** → vyber tvoju integráciu

---

## 8. Notion Buttons: pridaj 2 buttony do DB (~ 3 min)

V Notion DB **Zákazníci B2C**:

### 🔄 Prepočítaj cenu

1. Pravý horný roh → **+ Add property** → **Button**
2. Name: `🔄 Prepočítaj cenu`
3. **Configure action:**
   - Action: **Open URL**
   - URL: `https://energovision-cp-generator.onrender.com/webhook/prepocet`
   - **Hmm — tu je háčik:** Notion Button pre POST webhook potrebuje **Notion Automations** (placené feature) ALEBO cez **Make.com** ako prostredník.

**Riešenie cez Make.com (zadarmo do 1000 ops/mes):**

1. Make.com → New Scenario
2. Trigger: **Notion** → "Watch Database Items" (alebo button trigger)
3. Action: **HTTP** → POST na Render webhook URL
4. Body: `{"page_id": "{{1.id}}"}`
5. Headers: `X-Webhook-Secret: <z Render env>`

### 🖨 Vytlač ponuku

Rovnako, ale URL: `https://energovision-cp-generator.onrender.com/webhook/generate-pdf`
Body: `{"page_id": "{{1.id}}", "variant": "A"}`

---

## 9. Test end-to-end

1. V Notion DB otvor zákazníka (napr. Sarközi)
2. Klik **🔄 Prepočítaj cenu**
3. Po ~5 sek → ceny v stĺpcoch Cena A s DPH, Zisk A €, Suma CP s DPH sa zaktualizujú
4. Klik **🖨 Vytlač ponuku**
5. Po ~30 sek → PDF dostaneš (cez Make do Notion attachment alebo cez ďalšie scenáriá)

---

## Náklady (mesačne)

| Položka | Free | Pro |
|---|---:|---:|
| GitHub | $0 | $0 |
| Render Web Service | $0 (spí 15 min) | $7 (always-on) |
| Make.com | $0 (1000 ops) | $9 (10 000 ops) |
| **Spolu** | **$0** | **$16** |

---

## Čo sa stane keď Free plán nestačí

**Príznaky:**
- Prvý klik po dlhej pauze trvá 30-50 sek (Render Free spí)
- Zákazník v aute čaká → nepríjemné

**Riešenie:** Render dashboard → Service → **Upgrade to Starter $7/mes**.

Tým Render beží 24/7 a klik = 3 sek namiesto 30.

---

## Troubleshooting

| Problém | Riešenie |
|---|---|
| `notion_token_set: false` | Pridaj NOTION_TOKEN do Render env |
| 401 Unauthorized | WEBHOOK_SECRET mismatch — porovnaj Make headers vs Render env |
| Notion 403 | Integration nepripojená k DB → Krok 7 |
| Build fail v Render | Pozri Logs — možno chýba dependency v requirements.txt |
| PDF prázdne | WeasyPrint potrebuje Pango libs — render.yaml ich apt-get installne |
