"""MemoryPort protocol and in-memory catalog with redaction, dedup, and trust transitions."""

from collections.abc import Sequence
from typing import Protocol
from uuid import uuid4

from app.core.clock import WallClock
from app.core.window import _as_utc
from app.memory.models import (
    FeedbackVerdict,
    MemoryConfig,
    MemoryItem,
    MemoryQuery,
    MemoryRecallRecord,
    MemoryWrite,
    ScoredMemory,
    TrustLevel,
)
from app.memory.retrieval import embed_text, rank_memories, text_similarity
from app.tools.redact import redact_text


class MemoryPort(Protocol):
    @property
    def config(self) -> MemoryConfig: ...

    async def search(self, query: MemoryQuery) -> list[ScoredMemory]: ...

    async def write(self, body: MemoryWrite) -> tuple[MemoryItem, bool]: ...


class InMemoryCatalog:
    """In-memory MemoryPort implementation for unit tests and isolated ablation runs."""

    def __init__(
        self,
        items: Sequence[MemoryItem] | None = None,
        *,
        config: MemoryConfig | None = None,
    ) -> None:
        self._config = config or MemoryConfig()
        self._items: dict[str, MemoryItem] = {}
        self._produced_by_run: dict[str, list[str]] = {}
        self._recalls_by_run: dict[str, list[MemoryRecallRecord]] = {}
        for item in items or ():
            self._items[item.id] = item
            if item.source_run_id:
                self._record_produced(item.source_run_id, item.id)

    def _record_produced(self, run_id: str | None, memory_id: str) -> None:
        if not run_id:
            return
        ids = self._produced_by_run.setdefault(run_id, [])
        if memory_id not in ids:
            ids.append(memory_id)

    @property
    def config(self) -> MemoryConfig:
        return self._config

    def all_items(self) -> list[MemoryItem]:
        return list(self._items.values())

    def get_item(self, memory_id: str) -> MemoryItem | None:
        return self._items.get(memory_id)

    async def search(self, query: MemoryQuery) -> list[ScoredMemory]:
        return rank_memories(list(self._items.values()), query, self._config)

    async def write(self, body: MemoryWrite) -> tuple[MemoryItem, bool]:
        aliases: dict[str, str] = {}
        clean_summary = redact_text(body.summary, aliases)
        clean_content = redact_text(body.content, aliases)
        combined = f"{clean_summary} {clean_content}"
        embedding = embed_text(combined)
        now = _as_utc(body.created_at) if body.created_at is not None else WallClock().now()

        # Check deduplication: same service + same layer + same category + similarity >= threshold
        for existing in self._items.values():
            if existing.superseded_by is not None or existing.trust_level == TrustLevel.REJECTED:
                continue
            if (
                existing.service == body.service
                and existing.layer == body.layer
                and existing.category == body.category
                and existing.service_version == body.service_version
            ):
                sim = text_similarity(
                    combined,
                    f"{existing.summary} {existing.content}",
                    embedding,
                    existing.embedding,
                )
                if sim >= self._config.similarity_dedup_threshold:
                    merged_trust = (
                        TrustLevel.CONFIRMED
                        if TrustLevel.CONFIRMED in {existing.trust_level, body.trust_level}
                        else existing.trust_level
                    )
                    updated = existing.model_copy(
                        update={
                            "usage_count": existing.usage_count + 1,
                            "last_used_at": now,
                            "trust_level": merged_trust,
                        }
                    )
                    self._items[existing.id] = updated
                    self._record_produced(body.source_run_id, existing.id)
                    return updated, False

        item = MemoryItem(
            id=str(uuid4()),
            layer=body.layer,
            content=clean_content,
            summary=clean_summary,
            service=body.service,
            category=body.category,
            cause=body.cause,
            trust_level=body.trust_level,
            source_run_id=body.source_run_id,
            source_feedback_id=body.source_feedback_id,
            embedding=embedding,
            valid_from=_as_utc(body.valid_from) if body.valid_from else None,
            valid_until=_as_utc(body.valid_until) if body.valid_until else None,
            service_version=body.service_version,
            usage_count=1,
            helpful_count=0,
            last_used_at=None,
            superseded_by=None,
            created_at=now,
        )
        self._items[item.id] = item
        self._record_produced(body.source_run_id, item.id)
        return item, True

    def record_recalls(self, run_id: str, recalls: Sequence[MemoryRecallRecord]) -> None:
        self._recalls_by_run[run_id] = list(recalls)

    def apply_feedback(
        self,
        run_id: str,
        verdict: FeedbackVerdict,
    ) -> dict[str, list[str]]:
        recalls = self._recalls_by_run.get(run_id, [])
        promoted: list[str] = []
        rejected: list[str] = []
        unchanged: list[str] = []

        for rec in recalls:
            if not rec.cited:
                unchanged.append(rec.memory_id)
                continue
            item = self._items.get(rec.memory_id)
            if item is None:
                continue
            if verdict == FeedbackVerdict.CORRECT:
                self._items[item.id] = item.model_copy(
                    update={
                        "trust_level": TrustLevel.CONFIRMED,
                        "helpful_count": item.helpful_count + 1,
                    }
                )
                promoted.append(item.id)
            elif verdict == FeedbackVerdict.INCORRECT:
                self._items[item.id] = item.model_copy(update={"trust_level": TrustLevel.REJECTED})
                rejected.append(item.id)
            else:
                unchanged.append(item.id)

        for mid in self._produced_by_run.get(run_id, []):
            if mid in promoted or mid in rejected:
                continue
            item = self._items.get(mid)
            if item is None:
                continue
            if verdict == FeedbackVerdict.CORRECT:
                self._items[item.id] = item.model_copy(
                    update={
                        "trust_level": TrustLevel.CONFIRMED,
                        "helpful_count": item.helpful_count + 1,
                    }
                )
                promoted.append(item.id)
            elif verdict == FeedbackVerdict.INCORRECT:
                self._items[item.id] = item.model_copy(update={"trust_level": TrustLevel.REJECTED})
                rejected.append(item.id)

        return {
            "promoted_memory_ids": promoted,
            "rejected_memory_ids": rejected,
            "unchanged_memory_ids": unchanged,
        }
