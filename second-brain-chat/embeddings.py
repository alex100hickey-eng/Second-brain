"""
embeddings.py — a tiny, local, torch-free embedding layer for semantic search.

Everything semantic in the Second Brain runs on ONE small static embedding model
(model2vec's `potion-base-8M`, ~30 MB, 256-dim). It is a *static* model: token
embeddings are looked up and averaged — no transformer forward pass, so it needs
neither torch nor a GPU and runs happily on this box's Python 3.14 (where torch has
no wheels). The model weights are vendored into the project at
`models/potion-base-8M/` (gitignored) so nothing is fetched at runtime after the
first download.

Design:
  * Lazy singleton. The model loads on first use (~1s) and is cached; app startup
    never blocks on it.
  * Fail-soft. If the model can't load (missing files, missing package), `available()`
    returns False and callers fall back to keyword search. Nothing ever crashes because
    embeddings are unavailable — semantic is an *upgrade*, keyword is the floor.
  * No network on the hot path. If the vendored model dir is missing we try a one-time
    download to that dir; if that fails we degrade to keyword search.

Public API:
    available() -> bool
    embed(texts: list[str]) -> np.ndarray            # L2-normalized rows
    embed_one(text: str) -> np.ndarray | None
    cosine_rank(query, docs) -> list[(idx, score)]   # docs already embedded (matrix)
    rerank(query, items, text_of, kw_of=None, alpha=0.7) -> reordered items
"""

import os
import threading

MODEL_ID = os.environ.get("EMBED_MODEL_ID", "minishlab/potion-base-8M")
MODEL_DIR = os.environ.get(
    "EMBED_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "potion-base-8M"),
)

_LOCK = threading.Lock()
_STATE = {"tried": False, "model": None, "np": None}


def _load():
    """Load the model + numpy once. Sets _STATE['model'] to None on any failure."""
    if _STATE["tried"]:
        return
    with _LOCK:
        if _STATE["tried"]:
            return
        try:
            import numpy as np
            _STATE["np"] = np
            from model2vec import StaticModel
            if os.path.isdir(MODEL_DIR) and os.path.exists(os.path.join(MODEL_DIR, "model.safetensors")):
                model = StaticModel.from_pretrained(MODEL_DIR)
            else:
                # One-time download to the vendored dir; after this it's fully local.
                model = StaticModel.from_pretrained(MODEL_ID)
                try:
                    model.save_pretrained(MODEL_DIR)
                except Exception:
                    pass
            _STATE["model"] = model
        except Exception as e:  # missing package / weights / numpy — degrade gracefully
            print(f"embeddings: semantic model unavailable, falling back to keyword ({e})")
            _STATE["model"] = None
        finally:
            _STATE["tried"] = True


def available() -> bool:
    _load()
    return _STATE["model"] is not None


def _np():
    return _STATE["np"]


def embed(texts):
    """Return an (N, dim) float32 matrix of L2-normalized embeddings, or None if the
    model is unavailable. Empty/whitespace strings map to a zero row (never matches)."""
    if not available():
        return None
    np = _np()
    cleaned = [(t or "").strip() or " " for t in texts]
    vecs = _STATE["model"].encode(cleaned)
    vecs = np.asarray(vecs, dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def embed_one(text):
    m = embed([text])
    return None if m is None else m[0]


def cosine_rank(query_vec, doc_matrix, limit=None):
    """Given a query vector and an (N, dim) normalized doc matrix, return
    [(index, similarity), ...] sorted high→low. Similarities are cosine (dot, since
    both sides are normalized)."""
    np = _np()
    sims = doc_matrix @ query_vec
    order = np.argsort(-sims)
    if limit is not None:
        order = order[:limit]
    return [(int(i), float(sims[i])) for i in order]


def rerank(query, items, text_of, kw_of=None, alpha=0.7):
    """Semantically re-rank a candidate list.

    items   — list of candidate objects (dicts, etc.)
    text_of — fn(item) -> the representative text to embed for that item
    kw_of   — optional fn(item) -> an existing keyword score (blended in)
    alpha   — weight on semantic similarity vs. the normalized keyword score.

    Returns a NEW list ordered best-first. Each item gets a `_sem` (cosine) and
    `_blend` score attached (non-destructive copies are not made; we annotate in place
    only on dict items). If the model is unavailable, returns items unchanged (caller's
    keyword order is preserved — the graceful fallback).
    """
    if not items or not available():
        return items
    np = _np()
    texts = [text_of(it) for it in items]
    doc_m = embed(texts)
    if doc_m is None:
        return items
    q = embed_one(query)
    if q is None:
        return items
    sims = doc_m @ q  # cosine per item

    # Normalize keyword scores to 0..1 for a stable blend.
    if kw_of is not None:
        kws = [float(kw_of(it) or 0.0) for it in items]
        kmax = max(kws) if kws else 0.0
        kws = [(k / kmax) if kmax > 0 else 0.0 for k in kws]
    else:
        kws = [0.0] * len(items)

    scored = []
    for i, it in enumerate(items):
        sem = float(sims[i])
        blend = alpha * sem + (1 - alpha) * kws[i]
        if isinstance(it, dict):
            it["_sem"] = round(sem, 4)
            it["_blend"] = round(blend, 4)
        scored.append((blend, i, it))
    # Stable-ish: sort by blend desc, then original index to break ties deterministically.
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _b, _i, it in scored]
