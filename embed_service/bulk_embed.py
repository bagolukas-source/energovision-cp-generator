"""
Jednorazové (alebo periodické) naplnenie tip.item_embeddings.
Beží LOKÁLNE alebo na Render shelli — embeduje model na CPU (nič nevolá von),
výsledky zapisuje do Supabase cez REST (Content-Profile: tip).

ENV: SUPABASE_URL, SUPABASE_SERVICE_KEY
Spusti:  python bulk_embed.py historia hagard schrack    (alebo aj cenkros)
"""
import os, sys, json, math, urllib.request
from fastembed import TextEmbedding

SB = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")

# zdroj -> (tabuľka, stĺpec ref, stĺpec textu, stĺpec ceny, schema)
SRC = {
    "historia": ("historicke_polozky", "popis", "popis", "jc", "public"),
    "hagard":   ("hagard_items", "feed_id", "name", "price_purchase", "public"),
    "schrack":  ("schrack_items", "code", "name", "price_net", "public"),
    "cenkros":  ("cenkros_items", "code", "description", "price", "public"),
}

def fetch(schema, table, cols, offset, limit):
    url = f"{SB}/rest/v1/{table}?select={cols}&offset={offset}&limit={limit}"
    req = urllib.request.Request(url, headers={
        "apikey": KEY, "Authorization": f"Bearer {KEY}", "Accept-Profile": schema})
    return json.load(urllib.request.urlopen(req, timeout=120))

def upsert(rows):
    data = json.dumps(rows, allow_nan=False).encode()
    req = urllib.request.Request(f"{SB}/rest/v1/item_embeddings?on_conflict=source,ref",
        data=data, method="POST", headers={
            "apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
            "Content-Profile": "tip", "Prefer": "resolution=merge-duplicates,return=minimal"})
    urllib.request.urlopen(req, timeout=180).read()

def run(source, model):
    table, refc, txtc, cenac, schema = SRC[source]
    cols = f"{refc},{txtc},{cenac}"
    off, total = 0, 0
    PAGE = 1000
    seen = set()
    while True:
        rows = fetch(schema, table, cols, off, PAGE)
        if not rows: break
        batch, texts = [], []
        for r in rows:
            ref = str(r.get(refc) or "").strip()
            txt = str(r.get(txtc) or "").strip()
            if not ref or not txt or ref in seen: continue
            seen.add(ref); batch.append((ref, txt, r.get(cenac)))
        if batch:
            texts = ["passage: " + b[1] for b in batch]
            vecs = [v.tolist() for v in model.embed(texts)]
            recs = [{"source": source, "ref": b[0], "txt": b[1][:1000],
                     "cena": b[2], "embedding": vecs[i]} for i, b in enumerate(batch)]
            for i in range(0, len(recs), 500):
                upsert(recs[i:i+500])
            total += len(recs)
        off += PAGE
        print(f"  {source}: {total} embeddings...", flush=True)
    print(f"HOTOVO {source}: {total}")

if __name__ == "__main__":
    sources = sys.argv[1:] or ["historia", "hagard", "schrack"]
    m = TextEmbedding(MODEL)
    for s in sources:
        if s in SRC: run(s, m)
        else: print("neznámy zdroj:", s)
