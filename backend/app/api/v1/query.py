"""
Query API — RAG Q&A Endpoints

POST /api/v1/query          → JSON response (non-streaming)
POST /api/v1/query/stream   → Server-Sent Events (SSE) streaming

Both endpoints:
  - Require a valid JWT (minimum role: viewer)
  - Are tenant-scoped (all retrieval is isolated by tenant_id from JWT)
  - Use the full hybrid retrieval pipeline (Dense + BM25 + Cohere ReRank)
  - Use the LLM Gateway with automatic provider failover
  - Write an audit log entry for every query (SOC2 compliance)

Streaming (SSE) response format:
  event: token
  data: <delta_text>

  event: done
  data: {"latency_ms": 1234, "model": "gpt-4o", "chunks_used": 5}

  event: error
  data: {"message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_tenant_db, get_tenant_vector_store
from app.auth.rbac import require_role
from app.auth.token import TokenPayload
from app.core.config import settings
from app.llm.gateway import LLMGateway
from app.llm.router import ModelRequirements, PrivacyLevel, RoutingStrategy
from app.models.documents import AuditLog
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.pipeline import get_embedding_model
from app.rag.prompt_manager import PromptManager
from app.rag.reranker import CohereReranker
from app.vectorstore.base import VectorStoreBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/query", tags=["Query"])

# ---------------------------------------------------------------------------
# Shared singletons
# ---------------------------------------------------------------------------

_gateway:  LLMGateway | None = None
_reranker: CohereReranker | None = None


def _get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def _get_reranker() -> CohereReranker:
    global _reranker
    if _reranker is None:
        _reranker = CohereReranker()
    return _reranker


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Incoming query payload."""
    question: str = Field(
        ...,
        min_length=1,
        max_length=2_000,
        description="The user's natural-language question.",
        examples=["What is our refund policy?"],
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of context chunks to retrieve (1-20).",
    )
    privacy: PrivacyLevel = Field(
        default=PrivacyLevel.STANDARD,
        description=(
            "Data privacy requirement: standard | sensitive | private. "
            "'private' forces local Ollama inference."
        ),
    )
    strategy: RoutingStrategy = Field(
        default=RoutingStrategy.HIGHEST_QUALITY,
        description="LLM routing strategy: highest_quality | lowest_cost | lowest_latency.",
    )
    document_permissions: list[str] = Field(
        default_factory=list,
        description="ACL tags to filter retrieval. Only chunks matching these tags are returned.",
    )


class QueryResponse(BaseModel):
    """Non-streaming query response."""
    answer:        str
    question:      str
    model_used:    str
    provider:      str
    chunks_used:   int
    input_tokens:  int
    output_tokens: int
    latency_ms:    float
    request_id:    str


class ChunkCitation(BaseModel):
    """One retrieved chunk included in the response."""
    source_key:    str
    page_number:   int | None
    heading:       str | None
    relevance:     float
    excerpt:       str    # first 200 chars of the chunk text


# ---------------------------------------------------------------------------
# Dependency: resolve tenant_name from token
# ---------------------------------------------------------------------------

def _tenant_name_from_token(token: TokenPayload) -> str:
    """Extract human-readable tenant name from JWT claims (best-effort)."""
    return getattr(token, "tenant_name", None) or str(token.tenant_id)


# ---------------------------------------------------------------------------
# POST /api/v1/query  (non-streaming JSON)
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=QueryResponse,
    summary="Ask a question (non-streaming)",
    description="Hybrid retrieval → LongContextReorder → LLM. Returns JSON.",
)
async def query(
    request:     Request,
    body:        QueryRequest,
    token:       TokenPayload      = Depends(require_role("viewer")),
    db:          AsyncSession      = Depends(get_tenant_db),
    vec_store:   VectorStoreBase   = Depends(get_tenant_vector_store),
) -> QueryResponse:
    """
    Full RAG pipeline (non-streaming):
      1. Hybrid retrieval (Dense + BM25 + Cohere ReRank)
      2. LongContextReorder
      3. DB-versioned prompt injection
      4. LLM via gateway (routing + fallback)
      5. Audit log
    """
    t0          = time.perf_counter()
    request_id  = str(uuid.uuid4())
    tenant_id   = UUID(str(token.tenant_id))
    user_id     = UUID(str(token.user_id))
    tenant_name = _tenant_name_from_token(token)

    # ── Build metadata filter (document permissions) ─────────────────────────
    metadata_filter: dict | None = None
    if body.document_permissions:
        metadata_filter = {"document_permissions": body.document_permissions}

    # ── Hybrid retrieval ──────────────────────────────────────────────────────
    embedder  = get_embedding_model()
    retriever = HybridRetriever(
        vector_store=vec_store,
        embedder=embedder,
        reranker=_get_reranker(),
        dense_candidates=max(body.top_k * 4, 20),
        rerank_top_n=body.top_k,
    )
    docs = await retriever.retrieve(
        query=body.question,
        top_k=body.top_k,
        metadata_filter=metadata_filter,
    )

    if not docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "NO_CONTEXT",
                "message":    "No relevant documents found for your question.",
            },
        )

    # ── Prompt building ───────────────────────────────────────────────────────
    pm = PromptManager(prompt_name="rag_system")

    reordered_docs = pm.reorder_context(docs)
    context_str    = pm.format_context(reordered_docs)

    system_prompt = await pm.get_system_prompt(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        db=db,
        context=context_str,
    )

    # ── LLM Gateway ───────────────────────────────────────────────────────────
    gateway  = _get_gateway()
    messages = gateway.build_messages(
        system_prompt=system_prompt,
        user_question=body.question,
    )
    requirements = ModelRequirements(
        privacy=body.privacy,
        strategy=body.strategy,
        max_input_tokens=len(context_str) // 4 + 1_000,
    )

    llm_response = await gateway.invoke(
        messages=messages,
        tenant_id=tenant_id,
        user_id=user_id,
        requirements=requirements,
    )

    latency_ms = (time.perf_counter() - t0) * 1000

    # ── Audit log ─────────────────────────────────────────────────────────────
    audit = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action="query.rag",
        resource=f"query:{request_id}",
        doc_metadata={
            "question":       body.question[:500],
            "model":          llm_response.model_used,
            "provider":       llm_response.provider,
            "chunks_used":    len(docs),
            "input_tokens":   llm_response.input_tokens,
            "output_tokens":  llm_response.output_tokens,
            "latency_ms":     round(latency_ms, 1),
        },
        ip_address=request.client.host if request.client else None,
        success=True,
    )
    db.add(audit)
    await db.commit()

    return QueryResponse(
        answer        = llm_response.content,
        question      = body.question,
        model_used    = llm_response.model_used,
        provider      = llm_response.provider,
        chunks_used   = len(docs),
        input_tokens  = llm_response.input_tokens,
        output_tokens = llm_response.output_tokens,
        latency_ms    = round(latency_ms, 1),
        request_id    = request_id,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/query/stream  (SSE streaming)
# ---------------------------------------------------------------------------

@router.post(
    "/stream",
    summary="Ask a question (SSE streaming)",
    description=(
        "Returns a Server-Sent Events stream. "
        "Events: 'token' (content delta), 'done' (metadata), 'error'."
    ),
    response_class=StreamingResponse,
)
async def query_stream(
    request:   Request,
    body:      QueryRequest,
    token:     TokenPayload    = Depends(require_role("viewer")),
    db:        AsyncSession    = Depends(get_tenant_db),
    vec_store: VectorStoreBase = Depends(get_tenant_vector_store),
) -> StreamingResponse:
    """
    SSE streaming RAG pipeline.

    Response format::

        event: token
        data: The

        event: token
        data:  answer

        event: done
        data: {"latency_ms": 1234, "model": "gpt-4o", "chunks_used": 5, "request_id": "..."}

        event: error
        data: {"message": "..."}
    """
    tenant_id   = UUID(str(token.tenant_id))
    user_id     = UUID(str(token.user_id))
    tenant_name = _tenant_name_from_token(token)

    async def event_generator() -> AsyncIterator[str]:
        request_id  = str(uuid.uuid4())
        t0          = time.perf_counter()

        try:
            # ── Hybrid retrieval ─────────────────────────────────────────────
            metadata_filter = None
            if body.document_permissions:
                metadata_filter = {"document_permissions": body.document_permissions}

            embedder  = get_embedding_model()
            retriever = HybridRetriever(
                vector_store=vec_store,
                embedder=embedder,
                reranker=_get_reranker(),
                dense_candidates=max(body.top_k * 4, 20),
                rerank_top_n=body.top_k,
            )
            docs = await retriever.retrieve(
                query=body.question,
                top_k=body.top_k,
                metadata_filter=metadata_filter,
            )

            if not docs:
                yield _sse_event("error", {"message": "No relevant documents found."})
                return

            # ── Prompt ──────────────────────────────────────────────────────
            pm            = PromptManager(prompt_name="rag_system")
            reordered     = pm.reorder_context(docs)
            context_str   = pm.format_context(reordered)
            system_prompt = await pm.get_system_prompt(
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                db=db,
                context=context_str,
            )

            # ── Stream tokens ────────────────────────────────────────────────
            gateway  = _get_gateway()
            messages = gateway.build_messages(system_prompt, body.question)
            requirements = ModelRequirements(
                privacy=body.privacy,
                strategy=body.strategy,
                require_streaming=True,
            )

            full_content  = []
            total_out_tok = 0

            async for token_text in gateway.stream(
                messages=messages,
                tenant_id=tenant_id,
                user_id=user_id,
                requirements=requirements,
            ):
                full_content.append(token_text)
                total_out_tok += len(token_text) // 4 + 1
                yield _sse_event("token", token_text)

            latency_ms = (time.perf_counter() - t0) * 1000

            # ── Done event ───────────────────────────────────────────────────
            yield _sse_event("done", {
                "latency_ms":    round(latency_ms, 1),
                "chunks_used":   len(docs),
                "output_tokens": total_out_tok,
                "request_id":    request_id,
            })

            # ── Audit log (async, after stream) ─────────────────────────────
            audit = AuditLog(
                tenant_id=tenant_id,
                user_id=user_id,
                action="query.rag.stream",
                resource=f"query:{request_id}",
                doc_metadata={
                    "question":    body.question[:500],
                    "chunks_used": len(docs),
                    "latency_ms":  round(latency_ms, 1),
                },
                ip_address=request.client.host if request.client else None,
                success=True,
            )
            db.add(audit)
            await db.commit()

        except Exception as exc:
            logger.error("QueryStream | error: %s", exc, exc_info=True)
            yield _sse_event("error", {"message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",    # disable nginx buffering for SSE
            "Connection":        "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# SSE serialisation helper
# ---------------------------------------------------------------------------

def _sse_event(event: str, data: str | dict) -> str:
    """
    Serialise a Server-Sent Event.

    For "token" events, data is a raw string.
    For "done" / "error" events, data is a dict serialised to JSON.

    Format::
        event: <event>\\n
        data: <payload>\\n
        \\n
    """
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False)
    else:
        payload = data
    return f"event: {event}\ndata: {payload}\n\n"
