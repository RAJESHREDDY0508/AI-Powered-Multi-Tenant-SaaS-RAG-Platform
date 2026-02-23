"""
Evaluation Package — RAGAS-style LLM-as-judge metrics.

Provides:
  RAGASEvaluator    — async evaluator that scores RAG responses
  EvaluationMetrics — result dataclass
  EvalDashboard     — FastAPI router for the quality metrics API

Usage::

    from app.evaluation import RAGASEvaluator, EvaluationMetrics

    evaluator = RAGASEvaluator()
    metrics   = await evaluator.evaluate(
        question="What is the refund policy?",
        answer="Refunds are processed within 30 days.",
        contexts=["Our refund policy states 30-day processing time..."],
    )
    print(metrics.faithfulness, metrics.answer_relevance, metrics.context_precision)
"""

from app.evaluation.ragas_evaluator import EvaluationMetrics, RAGASEvaluator

__all__ = ["RAGASEvaluator", "EvaluationMetrics"]
