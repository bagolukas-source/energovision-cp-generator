"""
Energovision — self-hosted embedding service (NIČ NEVOLÁ VON).
Model z env EMBED_MODEL (default multilingual MiniLM, 384-dim). Stiahne sa raz pri builde,
beží lokálne na CPU. Za behu žiadne externé API.
"""
import os
from typing import List
from fastapi import FastAPI
from pydantic import BaseModel
from fastembed import TextEmbedding

MODEL_NAME = os.environ.get("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
IS_E5 = "e5" in MODEL_NAME.lower()
app = FastAPI(title="Energovision Embeddings", version="1.1")
_model = None

def model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(MODEL_NAME)
    return _model

class EmbedReq(BaseModel):
    texts: List[str]
    kind: str = "query"   # pre e5 modely: 'query'/'passage'; inak ignorované

def _prep(texts, kind):
    if IS_E5:
        p = "query: " if kind == "query" else "passage: "
        return [p + (t or "") for t in texts]
    return [(t or "") for t in texts]

@app.get("/health")
def health():
    v = next(model().embed(["test"]))
    return {"ok": True, "model": MODEL_NAME, "dim": len(v)}

@app.post("/embed")
def embed(req: EmbedReq):
    vecs = [v.tolist() for v in model().embed(_prep(req.texts, req.kind))]
    return {"vectors": vecs, "dim": (len(vecs[0]) if vecs else 0)}
