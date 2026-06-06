"""
Energovision — self-hosted embedding service (NIČ NEVOLÁ VON).
Model intfloat/multilingual-e5-large (1024-dim, multilingválny vrátane SK).
Stiahne sa RAZ pri štarte, potom beží lokálne na CPU. Žiadne externé API za behu.

Beh:  uvicorn main:app --host 0.0.0.0 --port $PORT
Endpointy:
  GET  /health           -> {"ok":true,"model":...,"dim":1024}
  POST /embed  {"texts":[...], "kind":"query|passage"} -> {"vectors":[[...],...]}
"""
import os
from typing import List
from fastapi import FastAPI
from pydantic import BaseModel
from fastembed import TextEmbedding

MODEL_NAME = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")
app = FastAPI(title="Energovision Embeddings", version="1.0")
_model = None

def model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(MODEL_NAME)  # stiahne váhy raz, cache v kontajneri
    return _model

class EmbedReq(BaseModel):
    texts: List[str]
    kind: str = "query"   # e5 konvencia: 'query' alebo 'passage'

@app.get("/health")
def health():
    m = model()
    return {"ok": True, "model": MODEL_NAME, "dim": 1024}

@app.post("/embed")
def embed(req: EmbedReq):
    prefix = "query: " if req.kind == "query" else "passage: "
    texts = [prefix + (t or "") for t in req.texts]
    vecs = [v.tolist() for v in model().embed(texts)]
    return {"vectors": vecs, "dim": (len(vecs[0]) if vecs else 0)}
