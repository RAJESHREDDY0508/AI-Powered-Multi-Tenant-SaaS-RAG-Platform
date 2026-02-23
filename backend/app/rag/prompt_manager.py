"""
Prompt Manager — DB-backed versioned prompt loading with A/B routing.

Responsibilities:
  1. Load active prompt template(s) for a given (tenant, name) from the DB.
  2. Select one variant via weighted random sampling (A/B traffic split).
  3. Render the template with runtime variables (tenant_name, context, question).
  4. Apply LongContextReorder to combat the "Lost in the Middle" LLM problem.
  5. Cache prompt rows in-process for 60 s to avoid per-request DB roundtrips.

"Lost in the Middle" problem:
  LLMs attend strongly to the BEGINNING and END of their context window but
  poorly to text in the middle (Liu et al., 2023 — "Lost in the Middle").
  LongContextReorder puts the highest-relevance chunks at positions 1 and N,
  and lower-relevance chunks in the interior where they are less critical.

  Before reorder: [rank1, rank2, rank3, rank4, rank5]
  After reorder:  [rank5, rank3, rank1, rank4, rank2]   ← zigzag pattern

Fallback hierarchy (most-specific to least):
  1. Tenant-specific active template (tenant_id = <uuid>)
  2. Global active template          (tenant_id IS NULL)
  3. Hardcoded default               (no DB row found)
"""

from __future__ import annotations

import logging
import random
import time
from typing import Final
from uuid import UUID

from langchain_community.document_transformers import LongContextReorder
from langchain_core.documents import Document
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompts import PromptTemplate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded fallback (used when DB has no matching template)
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_TEMPLATE: Final[str] = """\
You are a private AI assistant for {tenant_name}.
You answer questions ONLY using the provided context from the company's documents.
If the answer is not in the context, say "I don't have enough information to answer that."
Do not fabricate information. Do not reference information outside the provided context.

Context:
{context}
"""

# ---------------------------------------------------------------------------
# In-process TTL cache — avoids one DB SELECT per user query
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: int = 60

# cache[cache_key] = (timestamp, list[PromptTemplate])
_PROMPT_CACHE: dict[str, tuple[float, list[PromptTemplate]]] = {}


def _cache_key(tenant_id: UUID | None, name: str) -> str:
    return f"{tenant_id}:{name}"


def _cache_get(key: str) -> list[PromptTemplate] | None:
    entry = _PROMPT_CACHE.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _cache_set(key: str, rows: list[PromptTemplate]) -> None:
    _PROMPT_CACHE[key] = (time.monotonic(), rows)


# ---------------------------------------------------------------------------
# Weighted random selection
# ---------------------------------------------------------------------------

def _select_variant(variants: list[PromptTemplate]) -> PromptTemplate:
    """
    Select one template from a list of active variants using their ab_weight
    as relative traffic weights.

    Example:
        variant A  ab_weight=80
        variant B  ab_weight=20
        → A is chosen ~80% of the time, B ~20%.

    Falls back to the first variant if all weights are zero.
    """
    if len(variants) == 1:
        return variants[0]

    total = sum(v.ab_weight for v in variants)
    if total == 0:
        return variants[0]

    r = random.uniform(0, total)
    cumulative = 0.0
    for v in variants:
        cumulative += v.ab_weight
        if r <= cumulative:
            return v
    return variants[-1]   # fallback — should not be reached


# ---------------------------------------------------------------------------
# PromptManager
# ---------------------------------------------------------------------------

class PromptManager:
    """
    Load, cache, and render DB-versioned system prompt templates.

    Usage (inside a FastAPI async handler or RAG pipeline)::

        pm = PromptManager()

        # Get the rendered system prompt string
        system_prompt = await pm.get_system_prompt(
            tenant_id=tenant_uuid,
            tenant_name="Acme Corp",
            db=db_session,
        )

        # Reorder context documents for LongContextReorder
        reordered_docs = pm.reorder_context(retrieved_docs)
    """

    def __init__(self, prompt_name: str = "rag_system") -> None:
        self._name = prompt_name

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    async def get_system_prompt(
        self,
        tenant_id:   UUID,
        tenant_name: str,
        db:          AsyncSession,
        context:     str = "{context}",   # placeholder — filled later by chain
    ) -> str:
        """
        Load the best active template for this tenant and render it.

        Resolution order:
          1. Per-tenant active template
          2. Global active template (tenant_id IS NULL)
          3. Hardcoded _DEFAULT_SYSTEM_TEMPLATE

        The returned string is a Python format-style template string with
        {context} and {question} left as placeholders for the LCEL chain.

        Args:
            tenant_id:   UUID from the authenticated JWT.
            tenant_name: Human-readable org name (for system prompt injection).
            db:          Async SQLAlchemy session (from dependency injection).
            context:     Placeholder string; leave as "{context}" for LCEL.

        Returns:
            Rendered system prompt with {tenant_name} substituted.
        """
        template_text = await self._load_template(tenant_id, db)

        # Render tenant_name; leave {context} and {question} for the chain
        try:
            rendered = template_text.format(
                tenant_name=tenant_name,
                context=context,
                question="{question}",   # keep as literal placeholder
            )
        except KeyError as exc:
            logger.warning(
                "PromptManager | template has unknown placeholder %s — using raw template",
                exc,
            )
            rendered = template_text

        return rendered

    @staticmethod
    def reorder_context(docs: list[Document]) -> list[Document]:
        """
        Apply LongContextReorder to combat "Lost in the Middle" attention bias.

        The highest-relevance documents are placed at positions 0 and -1;
        lower-relevance docs fill the interior of the context window.

        Call this AFTER hybrid retrieval, BEFORE building the prompt string.

        Args:
            docs: Retrieved documents, best-to-worst relevance order.

        Returns:
            Reordered documents (same objects, different sequence).
        """
        if len(docs) <= 2:
            return docs  # no benefit from reordering with 1-2 docs

        reorderer = LongContextReorder()
        reordered = reorderer.transform_documents(docs)

        logger.debug(
            "PromptManager | LongContextReorder applied | docs=%d", len(reordered)
        )
        return reordered

    @staticmethod
    def format_context(docs: list[Document]) -> str:
        """
        Serialise a list of Documents into the context string injected into the prompt.

        Each chunk is prefixed with its source and similarity score so the LLM
        can reference where information came from (for citation generation).
        """
        parts: list[str] = []
        for i, doc in enumerate(docs, start=1):
            source     = doc.metadata.get("source_key", "unknown")
            score      = doc.metadata.get("rerank_score") or doc.metadata.get("vector_score", 0.0)
            page       = doc.metadata.get("page_number", "?")
            heading    = doc.metadata.get("heading", "")
            header     = f"[{i}] Source: {source} | Page: {page}"
            if heading:
                header += f" | Section: {heading}"
            if isinstance(score, (int, float)):
                header += f" | Relevance: {score:.3f}"
            parts.append(f"{header}\n{doc.page_content}")

        return "\n\n---\n\n".join(parts)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _load_template(
        self,
        tenant_id: UUID,
        db:        AsyncSession,
    ) -> str:
        """
        Query the DB for the best active template, with TTL cache.

        Returns the raw template_text string (unparsed).
        """
        # 1. Try tenant-specific template
        tenant_rows = await self._fetch_active(tenant_id, db)
        if tenant_rows:
            chosen = _select_variant(tenant_rows)
            logger.debug(
                "PromptManager | using tenant template | name=%s version=%d",
                chosen.name, chosen.version,
            )
            return chosen.template_text

        # 2. Try global template (tenant_id IS NULL)
        global_rows = await self._fetch_active(None, db)
        if global_rows:
            chosen = _select_variant(global_rows)
            logger.debug(
                "PromptManager | using global template | name=%s version=%d",
                chosen.name, chosen.version,
            )
            return chosen.template_text

        # 3. Fallback
        logger.debug(
            "PromptManager | no DB template found for name=%s — using hardcoded default",
            self._name,
        )
        return _DEFAULT_SYSTEM_TEMPLATE

    async def _fetch_active(
        self,
        tenant_id: UUID | None,
        db:        AsyncSession,
    ) -> list[PromptTemplate]:
        """
        Load active template rows for (tenant_id, name) from DB (or cache).
        """
        key = _cache_key(tenant_id, self._name)
        cached = _cache_get(key)
        if cached is not None:
            return cached

        stmt = select(PromptTemplate).where(
            and_(
                PromptTemplate.name == self._name,
                PromptTemplate.is_active.is_(True),
                (
                    PromptTemplate.tenant_id == tenant_id
                    if tenant_id is not None
                    else PromptTemplate.tenant_id.is_(None)
                ),
            )
        )
        result = await db.execute(stmt)
        rows   = list(result.scalars().all())
        _cache_set(key, rows)
        return rows
