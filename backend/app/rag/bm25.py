"""
BM25 Sparse Retrieval — keyword matching for hybrid search.

BM25 (Best Match 25) is the gold-standard keyword ranking function used
internally by Elasticsearch and Solr. It excels at exact-term matching:
  - Serial numbers ("SN-48291")
  - Policy references ("Policy #882")
  - Proper nouns / acronyms that dense embeddings conflate

Role in the hybrid pipeline:
  Dense retrieval  → semantic meaning  (what the user *means*)
  BM25 retrieval   → exact terminology (what the user *said*)
  RRF fusion       → best-of-both-worlds recall

Implementation — late-fusion BM25:
  Build the BM25 index over the dense retriever's candidate set (top-100).
  This avoids a separate search cluster while capturing 80% of the benefit.
  For full corpus BM25 (true hybrid), replace _bm25_search() in
  HybridRetriever with an Elasticsearch/OpenSearch query.

Dependencies:
  pip install rank-bm25>=0.2.2
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from app.vectorstore.base import QueryResult


# ---------------------------------------------------------------------------
# Minimal English stopword list for BM25 tokenisation
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "be", "was", "are",
    "that", "this", "which", "have", "has", "had", "not", "no", "can",
    "will", "would", "could", "should", "may", "might", "do", "does",
    "did", "its", "their", "our", "your", "my", "his", "her",
})

_PUNCT_TABLE = str.maketrans("", "", string.punctuation.replace("-", ""))


def _tokenize(text: str) -> list[str]:
    """
    Lightweight tokeniser: lowercase → strip punctuation → drop stopwords.

    Preserves hyphens (important for identifiers like "SN-48291", "GPT-4o").
    Returns at least one token so BM25Okapi never receives an empty list.
    """
    text   = text.lower().translate(_PUNCT_TABLE)
    tokens = [t for t in text.split() if t and t not in _STOPWORDS]
    return tokens or ["<empty>"]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class BM25SearchResult:
    """One BM25 result linked back to the original QueryResult."""
    query_result: QueryResult
    bm25_score:   float
    rank:         int        # 1-based rank in the BM25 result list


# ---------------------------------------------------------------------------
# TenantBM25Index
# ---------------------------------------------------------------------------

class TenantBM25Index:
    """
    In-memory BM25 index over a corpus of QueryResult objects.

    Build from the dense retriever's candidate set (late-fusion pattern),
    then call search() to score each candidate for the user's query.

    This class is stateless after construction and safe to use concurrently.

    Example::

        corpus = await vector_store.query(vector=query_vec, top_k=100)
        index  = TenantBM25Index.build(corpus)
        hits   = index.search(query_text, top_k=20)
    """

    __slots__ = ("_corpus", "_bm25")

    def __init__(self, corpus: list[QueryResult], bm25: BM25Okapi) -> None:
        self._corpus = corpus
        self._bm25   = bm25

    # -----------------------------------------------------------------------
    # Factory
    # -----------------------------------------------------------------------

    @classmethod
    def build(cls, corpus: list[QueryResult]) -> "TenantBM25Index":
        """
        Build a BM25Okapi index from a list of QueryResult objects.

        Args:
            corpus: Candidate documents (from dense retrieval or direct DB fetch).
                    Must be non-empty.

        Returns:
            A ready-to-query TenantBM25Index.

        Raises:
            ValueError: If corpus is empty.
        """
        if not corpus:
            raise ValueError("TenantBM25Index.build() requires a non-empty corpus")

        tokenized = [_tokenize(doc.text) for doc in corpus]
        bm25      = BM25Okapi(tokenized)
        return cls(corpus, bm25)

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def search(self, query: str, top_k: int = 20) -> list[BM25SearchResult]:
        """
        Score all corpus documents against the query, return top_k ranked results.

        Args:
            query:  Raw user query string (tokenised internally).
            top_k:  Maximum results to return (capped at corpus size).

        Returns:
            List of BM25SearchResult sorted by bm25_score descending.
        """
        query_tokens = _tokenize(query)
        scores       = self._bm25.get_scores(query_tokens)

        # Sort by score descending, keep top_k
        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        top     = indexed[: min(top_k, len(self._corpus))]

        return [
            BM25SearchResult(
                query_result=self._corpus[idx],
                bm25_score=float(score),
                rank=rank,
            )
            for rank, (idx, score) in enumerate(top, start=1)
        ]

    # -----------------------------------------------------------------------
    # Dunder
    # -----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._corpus)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TenantBM25Index docs={len(self)}>"
