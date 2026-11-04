"""Hybrid vector + BM25 retrieval with time decay, trust, and service_version weighting."""

import hashlib
import math
import re
from collections.abc import Sequence
from datetime import datetime

from app.core.clock import WallClock
from app.core.window import _as_utc
from app.memory.models import (
    MemoryConfig,
    MemoryItem,
    MemoryQuery,
    ScoredMemory,
    TrustLevel,
)

_TOKEN = re.compile(r"[a-z0-9_]+")
EMBEDDING_DIMS = 64


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def embed_text(text: str, dims: int = EMBEDDING_DIMS) -> list[float]:
    """Deterministic feature-hashed L2-normalized vector (words + character 3-grams)."""
    vec = [0.0] * dims
    tokens = tokenize(text)
    if not tokens:
        return vec
    features: list[tuple[str, float]] = [(tok, 1.0) for tok in tokens]
    for i in range(len(tokens) - 1):
        features.append((f"{tokens[i]}_{tokens[i + 1]}", 1.25))
    for tok in tokens:
        if len(tok) >= 4:
            for j in range(len(tok) - 2):
                features.append((f"#{tok[j : j + 3]}", 0.35))
    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dims
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        return vec
    return [round(v / norm, 6) for v in vec]


def cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def text_similarity(
    text_a: str,
    text_b: str,
    vec_a: Sequence[float] | None = None,
    vec_b: Sequence[float] | None = None,
) -> float:
    """Combined cosine and token-overlap similarity used for deduplication."""
    emb_a = list(vec_a) if vec_a else embed_text(text_a)
    emb_b = list(vec_b) if vec_b else embed_text(text_b)
    cos = cosine_similarity(emb_a, emb_b)
    toks_a = set(tokenize(text_a))
    toks_b = set(tokenize(text_b))
    if not toks_a or not toks_b:
        return round(cos, 4)
    jaccard = len(toks_a & toks_b) / len(toks_a | toks_b)
    return round(0.6 * cos + 0.4 * jaccard, 4)


def bm25_scores(
    query: str, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """Compute normalized Okapi BM25 scores in [0.0, 1.0] across candidate documents."""
    if not documents:
        return []
    q_tokens = tokenize(query)
    if not q_tokens:
        return [1.0 for _ in documents]
    doc_tokens = [tokenize(doc) for doc in documents]
    n_docs = len(doc_tokens)
    avg_dl = sum(len(toks) for toks in doc_tokens) / max(1, n_docs)
    if avg_dl <= 0.0:
        return [0.0 for _ in documents]

    df: dict[str, int] = {}
    for term in set(q_tokens):
        df[term] = sum(1 for toks in doc_tokens if term in toks)

    raw_scores: list[float] = []
    for toks in doc_tokens:
        dl = len(toks)
        tf_map: dict[str, int] = {}
        for tok in toks:
            tf_map[tok] = tf_map.get(tok, 0) + 1
        score = 0.0
        for term in q_tokens:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
            denom = tf + k1 * (1.0 - b + b * (dl / avg_dl))
            score += idf * ((tf * (k1 + 1.0)) / denom)
        raw_scores.append(score)

    max_score = max(raw_scores, default=0.0)
    if max_score <= 0.0:
        return [0.0 for _ in documents]
    return [round(s / max_score, 6) for s in raw_scores]


def rank_memories(
    items: Sequence[MemoryItem],
    query: MemoryQuery,
    config: MemoryConfig | None = None,
) -> list[ScoredMemory]:
    """Filter and rank MemoryItems by: similarity * time_decay * trust_weight * version_weight."""
    cfg = config or MemoryConfig()
    allowed_layers = set(query.layers) & set(cfg.enabled_layers)
    if not allowed_layers:
        return []

    allowed_trust = {TrustLevel.CONFIRMED}
    if query.include_candidate:
        allowed_trust.add(TrustLevel.CANDIDATE)
    if query.include_rejected:
        allowed_trust.add(TrustLevel.REJECTED)

    filtered: list[MemoryItem] = []
    for item in items:
        if item.superseded_by is not None:
            continue
        if item.layer not in allowed_layers:
            continue
        if item.trust_level not in allowed_trust:
            continue
        if query.service is not None and item.service != query.service:
            continue
        filtered.append(item)

    if not filtered:
        return []

    q_text = query.query.strip()
    q_vec = embed_text(q_text) if q_text else []
    docs = [f"{it.service} {it.summary} {it.content}" for it in filtered]
    lex_scores = bm25_scores(q_text, docs) if q_text else [1.0 for _ in filtered]

    ref_time = (
        _as_utc(query.reference_time) if query.reference_time is not None else WallClock().now()
    )
    scored: list[ScoredMemory] = []

    for item, bm25_val in zip(filtered, lex_scores, strict=True):
        if q_text:
            item_vec = item.embedding or embed_text(f"{item.summary} {item.content}")
            cos_val = cosine_similarity(q_vec, item_vec)
            sim = cfg.vector_weight * cos_val + cfg.bm25_weight * bm25_val
        else:
            sim = 1.0

        time_decay = _time_decay_weight(item.created_at, ref_time, cfg.half_life_days)
        trust_w = cfg.trust_weight(item.trust_level)
        version_w, is_stale = _version_weight(item, query.service_version, ref_time, cfg)
        total = round(sim * time_decay * trust_w * version_w, 4)
        scored.append(
            ScoredMemory(
                item=item,
                score=total,
                similarity_score=round(sim, 4),
                time_decay_weight=round(time_decay, 4),
                trust_weight=round(trust_w, 4),
                version_weight=round(version_w, 4),
                stale=is_stale,
            )
        )

    scored.sort(key=lambda entry: (-entry.score, entry.item.id))
    return scored[: query.limit]


def _time_decay_weight(
    created_at: datetime,
    reference_time: datetime | None,
    half_life_days: float,
) -> float:
    if reference_time is None:
        return 1.0
    created_utc = _as_utc(created_at)
    age_days = max(0.0, (reference_time - created_utc).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def _version_weight(
    item: MemoryItem,
    query_version: str | None,
    reference_time: datetime | None,
    cfg: MemoryConfig,
) -> tuple[float, bool]:
    stale = False
    if reference_time is not None and item.valid_until is not None:
        if _as_utc(item.valid_until) < reference_time:
            stale = True
    if query_version and item.service_version:
        if item.service_version != query_version:
            stale = True
        elif not stale:
            return cfg.matching_version_multiplier, False
    if stale:
        return cfg.stale_version_multiplier, True
    return 1.0, False
