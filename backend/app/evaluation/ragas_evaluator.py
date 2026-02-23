"""
RAGAS-style RAG Quality Evaluator — LLM-as-Judge

Implements the three core RAGAS metrics using an LLM judge:

  Faithfulness (hallucination detection):
    "Is every claim in the answer supported by the provided context?"
    Method: Ask the judge LLM to decompose the answer into atomic claims,
    then verify each claim against the context. Score = claims supported / total claims.
    A score of 1.0 means the answer is fully grounded; 0.0 means fully fabricated.

  Answer Relevance (usefulness):
    "Does the answer actually address the user's question?"
    Method: Embed the answer, generate N reverse-questions from the answer,
    measure cosine similarity of those questions to the original question.
    A relevant answer should generate questions similar to the original.

  Context Precision (retrieval quality):
    "Were the retrieved context chunks relevant to the question?"
    Method: Ask the judge LLM to rate each retrieved chunk on a 0-3 scale.
    Score = (sum of ratings) / (len(chunks) × 3).
    A score of 1.0 means all retrieved chunks were perfectly relevant.

Why LLM-as-judge instead of the ragas library?
  - The ragas PyPI package has heavy dependencies (datasets, transformers, etc.)
    that conflict with the platform's existing stack.
  - LLM-as-judge is more controllable: we can tune the prompts, model, and
    scoring rubric without library version constraints.
  - Results are comparable to ragas on standard benchmarks.

Evaluation is run ASYNCHRONOUSLY after each query response is returned to
the user (fire-and-forget via Celery task) so it never affects p95 latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Judge model (cheaper than the production model — evaluation is async)
# ---------------------------------------------------------------------------

_JUDGE_MODEL       = "gpt-4o-mini"
_JUDGE_TEMPERATURE = 0.0
_JUDGE_MAX_TOKENS  = 1_024


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EvaluationMetrics:
    """
    RAGAS-style metric scores for one RAG query-response pair.

    All scores are in [0.0, 1.0]:
      1.0 = perfect
      0.0 = completely wrong / irrelevant
      None = evaluation could not be computed (e.g. judge LLM failed)
    """
    faithfulness:      Optional[float] = None
    answer_relevance:  Optional[float] = None
    context_precision: Optional[float] = None

    @property
    def composite(self) -> Optional[float]:
        """Simple average of the three metrics. None if any metric is missing."""
        scores = [s for s in [self.faithfulness, self.answer_relevance, self.context_precision] if s is not None]
        return round(sum(scores) / len(scores), 4) if scores else None

    def to_dict(self) -> dict:
        return {
            "faithfulness":      self.faithfulness,
            "answer_relevance":  self.answer_relevance,
            "context_precision": self.context_precision,
            "composite":         self.composite,
        }


# ---------------------------------------------------------------------------
# Judge prompts
# ---------------------------------------------------------------------------

_FAITHFULNESS_PROMPT = """\
You are evaluating whether an AI assistant's answer is faithful to the provided context.

CONTEXT:
{context}

QUESTION:
{question}

ANSWER:
{answer}

Task:
1. List every factual claim made in the answer (as a JSON array of strings).
2. For each claim, judge whether it is supported by the CONTEXT above.
3. Return a JSON object with this exact schema:
{{
  "claims": ["claim 1", "claim 2", ...],
  "supported": [true, false, ...],
  "score": <float 0.0-1.0>
}}

"score" must equal the fraction of claims that are supported.
Output ONLY valid JSON. Do not include any explanation outside the JSON.
"""

_ANSWER_RELEVANCE_PROMPT = """\
You are evaluating whether an AI assistant's answer is relevant to the user's question.

QUESTION:
{question}

ANSWER:
{answer}

Task: Generate {n_questions} questions that the given ANSWER is trying to answer.
These should be the questions a reader would naturally ask after reading the answer.

Return a JSON array of strings (the generated questions).
Output ONLY valid JSON. Do not include any explanation outside the JSON.
"""

_CONTEXT_PRECISION_PROMPT = """\
You are evaluating the relevance of retrieved context chunks for answering a question.

QUESTION:
{question}

Rate each context chunk below on this scale:
  0 = completely irrelevant
  1 = slightly relevant
  2 = mostly relevant
  3 = highly relevant (contains the answer or key supporting information)

CHUNKS:
{chunks}

Return a JSON array of integers (one rating per chunk, in order).
Output ONLY valid JSON. Do not include any explanation outside the JSON.
"""


# ---------------------------------------------------------------------------
# RAGASEvaluator
# ---------------------------------------------------------------------------

class RAGASEvaluator:
    """
    Async LLM-as-judge evaluator for RAG quality metrics.

    Usage (typically called from a Celery task, not the hot path)::

        evaluator = RAGASEvaluator()
        metrics   = await evaluator.evaluate(
            question="What is the refund policy?",
            answer="Refunds are processed within 30 days.",
            contexts=["Our policy: refunds take 30 days..."],
        )
        # metrics.faithfulness, .answer_relevance, .context_precision

    Never raises — returns partial metrics on any judge LLM failure.
    """

    def __init__(
        self,
        judge_model: str = _JUDGE_MODEL,
        api_key:     str | None = None,
    ) -> None:
        self._llm = ChatOpenAI(
            model=judge_model,
            api_key=api_key or settings.openai_api_key,
            temperature=_JUDGE_TEMPERATURE,
            max_tokens=_JUDGE_MAX_TOKENS,
        )
        self._embedder = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=api_key or settings.openai_api_key,
            dimensions=settings.embedding_dimensions,
        )

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    async def evaluate(
        self,
        question: str,
        answer:   str,
        contexts: list[str],
    ) -> EvaluationMetrics:
        """
        Evaluate a RAG query-response pair on all three metrics concurrently.

        Args:
            question: The user's original question.
            answer:   The LLM's generated answer.
            contexts: List of retrieved chunk texts used as context.

        Returns:
            EvaluationMetrics with scores in [0.0, 1.0] or None on failure.
        """
        # Run all three metrics concurrently — they're independent LLM calls
        faithfulness, relevance, precision = await asyncio.gather(
            self._score_faithfulness(question, answer, contexts),
            self._score_answer_relevance(question, answer),
            self._score_context_precision(question, contexts),
            return_exceptions=True,
        )

        metrics = EvaluationMetrics()

        if isinstance(faithfulness, float):
            metrics.faithfulness = round(faithfulness, 4)
        elif isinstance(faithfulness, Exception):
            logger.warning("Faithfulness eval failed: %s", faithfulness)

        if isinstance(relevance, float):
            metrics.answer_relevance = round(relevance, 4)
        elif isinstance(relevance, Exception):
            logger.warning("Answer relevance eval failed: %s", relevance)

        if isinstance(precision, float):
            metrics.context_precision = round(precision, 4)
        elif isinstance(precision, Exception):
            logger.warning("Context precision eval failed: %s", precision)

        logger.info(
            "EvalMetrics | faithfulness=%.3f relevance=%.3f precision=%.3f composite=%.3f",
            metrics.faithfulness or 0,
            metrics.answer_relevance or 0,
            metrics.context_precision or 0,
            metrics.composite or 0,
        )
        return metrics

    # -----------------------------------------------------------------------
    # Metric implementations
    # -----------------------------------------------------------------------

    async def _score_faithfulness(
        self,
        question: str,
        answer:   str,
        contexts: list[str],
    ) -> float:
        """
        Faithfulness score: fraction of answer claims supported by context.
        """
        if not contexts:
            return 0.0

        context_text = "\n\n---\n\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts))
        prompt = _FAITHFULNESS_PROMPT.format(
            context=context_text,
            question=question,
            answer=answer,
        )

        raw    = await self._call_judge(prompt)
        parsed = _parse_json(raw)

        if not parsed or "score" not in parsed:
            raise ValueError(f"Faithfulness judge returned unparseable response: {raw[:200]}")

        score = float(parsed["score"])
        return max(0.0, min(1.0, score))   # clamp to [0, 1]

    async def _score_answer_relevance(
        self,
        question: str,
        answer:   str,
        n_questions: int = 3,
    ) -> float:
        """
        Answer relevance: cosine similarity of reverse-generated questions to original.
        """
        prompt = _ANSWER_RELEVANCE_PROMPT.format(
            question=question,
            answer=answer,
            n_questions=n_questions,
        )
        raw     = await self._call_judge(prompt)
        gen_qs  = _parse_json(raw)

        if not gen_qs or not isinstance(gen_qs, list):
            raise ValueError(f"Answer relevance judge returned bad response: {raw[:200]}")

        # Embed original question + generated questions, compute mean cosine similarity
        all_texts  = [question] + [str(q) for q in gen_qs]
        embeddings = await self._embedder.aembed_documents(all_texts)

        orig_vec  = embeddings[0]
        gen_vecs  = embeddings[1:]
        sims      = [_cosine_similarity(orig_vec, gv) for gv in gen_vecs]
        score     = sum(sims) / len(sims) if sims else 0.0

        return max(0.0, min(1.0, score))

    async def _score_context_precision(
        self,
        question: str,
        contexts: list[str],
    ) -> float:
        """
        Context precision: average relevance rating of retrieved chunks (0-3 scale).
        """
        if not contexts:
            return 0.0

        chunks_text = "\n\n".join(
            f"[Chunk {i+1}]:\n{c[:800]}"   # truncate long chunks for the judge
            for i, c in enumerate(contexts)
        )
        prompt = _CONTEXT_PRECISION_PROMPT.format(
            question=question,
            chunks=chunks_text,
        )
        raw     = await self._call_judge(prompt)
        ratings = _parse_json(raw)

        if not ratings or not isinstance(ratings, list):
            raise ValueError(f"Context precision judge returned bad response: {raw[:200]}")

        scores    = [float(r) / 3.0 for r in ratings if isinstance(r, (int, float))]
        precision = sum(scores) / len(scores) if scores else 0.0
        return max(0.0, min(1.0, precision))

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    async def _call_judge(self, prompt: str) -> str:
        """Call the judge LLM with the given prompt, return raw string content."""
        from langchain_core.messages import HumanMessage
        response = await self._llm.ainvoke([HumanMessage(content=prompt)])
        return response.content.strip()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict | list | None:
    """
    Extract and parse the first JSON object or array from a string.
    LLMs sometimes wrap JSON in markdown code fences.
    """
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first JSON structure
        match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
