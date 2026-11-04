"""Search and import M6 episodic and semantic memories (FR-7, FR-10).

GET /v1/memory/search defaults to returning CONFIRMED memories only, with
explicit `include_candidate=true` opt-in for CANDIDATE memories.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.auth import require_api_key
from app.memory.models import MemoryLayer, MemoryQuery, MemoryWrite, ScoredMemory
from app.persistence.store import search_memory_items, write_memory_item

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _serialize_scored(scored: ScoredMemory) -> dict[str, Any]:
    item = scored.item
    return {
        "id": item.id,
        "layer": item.layer.value,
        "service": item.service,
        "category": None if item.category is None else item.category.value,
        "cause": None if item.cause is None else item.cause.value,
        "trust_level": item.trust_level.value,
        "summary": item.summary,
        "content": item.content,
        "service_version": item.service_version,
        "usage_count": item.usage_count,
        "helpful_count": item.helpful_count,
        "source_run_id": item.source_run_id,
        "source_feedback_id": item.source_feedback_id,
        "created_at": item.created_at.isoformat(),
        "score": scored.score,
        "similarity_score": scored.similarity_score,
        "time_decay_weight": scored.time_decay_weight,
        "trust_weight": scored.trust_weight,
        "version_weight": scored.version_weight,
        "stale": scored.stale,
    }


@router.get("/search")
async def get_memory_search(
    q: str = Query(default=""),
    service: str | None = Query(default=None),
    layer: MemoryLayer | None = Query(default=None),
    include_candidate: bool = Query(default=False),
    include_rejected: bool = Query(default=False),
    service_version: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    _: None = Depends(require_api_key),
) -> JSONResponse:
    layers = (layer,) if layer is not None else (MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC)
    results = await search_memory_items(
        MemoryQuery(
            query=q,
            service=service,
            layers=layers,
            include_candidate=include_candidate,
            include_rejected=include_rejected,
            service_version=service_version,
            limit=limit,
        )
    )
    return JSONResponse(
        status_code=200,
        content={"items": [_serialize_scored(r) for r in results]},
    )


@router.post("")
async def post_memory_item(
    body: MemoryWrite,
    _: None = Depends(require_api_key),
) -> JSONResponse:
    item, created = await write_memory_item(body)
    return JSONResponse(
        status_code=201 if created else 200,
        content={
            "created": created,
            "item": item.model_dump(mode="json", exclude={"embedding"}),
        },
    )
