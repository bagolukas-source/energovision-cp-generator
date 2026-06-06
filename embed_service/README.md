# Energovision — vlastná embedding služba (self-hosted, NIČ NEVOLÁ VON)

Sémantické vyhľadávanie pre Rozpočtár/TIP **u vás**. Model `intfloat/multilingual-e5-large`
(1024-dim, multilingválny vrátane SK) sa stiahne **raz pri builde**, potom beží lokálne na
CPU vo vašej Render službe. Za behu žiadne externé API, žiadny token.

## Súbory
- `main.py` — FastAPI služba (`/embed`, `/health`)
- `bulk_embed.py` — jednorazové naplnenie vektorov do `tip.item_embeddings`
- `requirements.txt`, `render.yaml`

## 1. Deploy služby na Render (vaša infra)
1. Pridaj tento priečinok do GitHub repa (napr. `energovision-tip/embed_service`).
2. Render → New → Blueprint (z `render.yaml`) alebo Web Service:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Plan: Starter+ (model ~2 GB v pamäti)
3. Po nábehu over: `GET https://<sluzba>.onrender.com/health` → `{"ok":true,"dim":1024}`.

## 2. Naplnenie vektorov (raz)
Na Render shelli služby (alebo lokálne s tým istým modelom):
```bash
export SUPABASE_URL=https://uzwajrpebblafuhrtuwn.supabase.co
export SUPABASE_SERVICE_KEY=<service_role>
python bulk_embed.py historia hagard schrack      # cenkros voliteľne (221k, dlhšie)
```
Naplní `tip.item_embeddings` (pgvector). Potom doplň HNSW index pre rýchlosť:
```sql
CREATE INDEX ON tip.item_embeddings USING hnsw (embedding vector_cosine_ops);
```

## 3. Napojenie v CRM (Rozpočtár)
DB má RPC `match_semantic(q text, p_source text, match_count int)` — `q` je vektor ako JSON `'[...]'`.
Tok pri mapovaní položky:
```
popis položky → POST <embed>/embed {"texts":[popis],"kind":"query"} → vektor
            → supabase.rpc('match_semantic', { q: JSON.stringify(vec), match_count: 5 })
            → najlepšie sémantické zhody (zlúčiť s trigram match_one, vyšší z toho)
```
Pridaj do CRM env: `EMBED_URL=https://<sluzba>.onrender.com`. (Volá interne backend CRM, nie prehliadač.)

## Prečo e5-large
1024-dim presne sedí na `tip.item_embeddings.embedding vector(1024)` aj `tip.document_chunks`.
Alternatíva `BAAI/bge-m3` (tiež 1024) — zmena len `EMBED_MODEL`. Oboje otvorené, bez tokenu.

## Náklady
Render CPU služba (~7–25 €/mes podľa planu) — **vaša infra**. Žiadne platby za API, žiadne volania von.
```
```
