"""
Semantic Chunker  —  NLP-Based Text Segmentation
══════════════════════════════════════════════════

Why not fixed-size chunks?
──────────────────────────
  Fixed-size chunking (e.g. every 500 chars) splits mid-sentence and
  mid-paragraph, destroying the semantic coherence that makes RAG work:

    "The defendant pleaded guilty to
    [CHUNK BREAK]
    fraud charges in..."

  The retriever finds the first chunk, misses the verdict.
  Token-level splitting is slightly better but still arbitrary.

Our approach: Semantic Boundary Detection
──────────────────────────────────────────
  1. Detect headings (regex heuristic + spaCy NER)
  2. Split into paragraphs at double-newline boundaries
  3. Further split long paragraphs at sentence boundaries (spaCy)
  4. Merge very short sentences with neighbours to meet MIN_CHUNK_TOKENS
  5. Respect a MAX_CHUNK_TOKENS ceiling

This preserves:
  - Heading → body relationships (both in same chunk or heading duplicated)
  - Complete sentences (no mid-sentence splits)
  - Semantic coherence (related paragraphs stay together)
  - Page boundaries (tracked in metadata for citations)

Why this improves RAG quality:
  - Retrievers score chunks by cosine similarity to the query embedding.
    Semantically coherent chunks embed to a tighter, more representative
    vector — retrieval precision goes up.
  - Incomplete sentences create "orphan" embeddings that confuse the LLM:
    "fraud charges in..." → no subject, poor embedding quality.
  - Paragraph-level context means the LLM gets full reasoning chains,
    not arbitrary character windows.

Memory considerations:
  - spaCy loads the model once (module-level singleton) — ~50–150 MB.
  - Processing happens entirely in the Celery worker process, not the API.
  - Documents are processed one at a time per worker — no concurrent
    spaCy calls in the same process (no thread-safety issue).
  - For 50 MB documents, peak memory is ~10× raw text size due to spaCy's
    internal doc representation. Workers should have 2 GB RAM minimum.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Target chunk size in characters (not tokens — avoids tokenizer dependency)
# ~200–500 tokens for text-embedding-3-small (1 token ≈ 4 chars)
MIN_CHUNK_CHARS  = 200
MAX_CHUNK_CHARS  = 2000   # hard ceiling — split regardless at this size
OVERLAP_CHARS    = 100    # overlap ONLY when a paragraph exceeds MAX_CHUNK_CHARS

# spaCy model — "en_core_web_sm" (12 MB, fast) or "en_core_web_lg" (700 MB, better)
# For multilingual: "xx_ent_wiki_sm" or a dedicated language model
SPACY_MODEL = "en_core_web_sm"

# Heading detection regex: lines that look like headings
#   - ALL CAPS line ≥ 3 words
#   - Markdown headings: # Heading, ## Heading
#   - Numbered sections: "1.2.3 Overview", "SECTION 4:"
_HEADING_RE = re.compile(
    r"""
    ^(
        \#{1,6}\s+.+                             # Markdown heading
      | [A-Z][A-Z\s]{4,}\b                       # ALL CAPS (5+ chars)
      | (?:\d+\.)+\d*\s+[A-Z].{3,}              # Numbered: 1.2.3 Title
      | (?:Section|Chapter|Article|Appendix)\s+\S+  # Common doc headings
    )$
    """,
    re.VERBOSE | re.MULTILINE,
)

# Minimum heading line length (avoids matching short ALL-CAPS words like "NOTE:")
MIN_HEADING_LEN = 8


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChunkResult:
    """
    A single semantic chunk ready for embedding and vector storage.

    Fields map directly to the Pinecone / Weaviate metadata schema.
    """
    chunk_id:    str           # deterministic: sha256(tenant_id + doc_id + chunk_index)
    tenant_id:   UUID          # from JWT — enforced by the upstream pipeline
    document_id: str           # UUID of the parent document
    chunk_index: int           # 0-based ordering within the document
    text:        str           # the actual chunk content
    char_count:  int           # len(text)
    token_est:   int           # estimated token count (char_count // 4)
    page_number: int           # page where this chunk starts (1-based)
    source_key:  str           # S3 key of the source document
    heading:     str           # nearest preceding heading, if any
    metadata:    dict = field(default_factory=dict)   # arbitrary extra fields


# ---------------------------------------------------------------------------
# spaCy model singleton
# ---------------------------------------------------------------------------

_spacy_nlp = None


def _get_nlp():
    """
    Load the spaCy model once per process (Celery worker).
    Thread-safe: only Celery main thread calls this during task startup.
    """
    global _spacy_nlp
    if _spacy_nlp is None:
        import spacy
        try:
            _spacy_nlp = spacy.load(SPACY_MODEL, disable=["ner", "parser"])
            # We re-enable sentencizer (rule-based, fast) instead of parser
            if "sentencizer" not in _spacy_nlp.pipe_names:
                _spacy_nlp.add_pipe("sentencizer")
            logger.info("spaCy model '%s' loaded", SPACY_MODEL)
        except OSError:
            logger.warning(
                "spaCy model '%s' not found — run: python -m spacy download %s",
                SPACY_MODEL, SPACY_MODEL,
            )
            _spacy_nlp = None   # caller will use fallback splitter
    return _spacy_nlp


# ---------------------------------------------------------------------------
# Core chunker
# ---------------------------------------------------------------------------

class SemanticChunker:
    """
    Stateless semantic text chunker.

    Usage:
        chunker = SemanticChunker()
        chunks = chunker.chunk(
            text=extracted_text,
            tenant_id=tenant_id,
            document_id=str(document_id),
            source_key=s3_key,
            page_map={...},    # optional: {char_offset → page_number}
        )

    Design trade-offs:
      - spaCy sentence segmentation is the primary splitter (best quality)
      - Falls back to regex paragraph splitting if spaCy unavailable
      - Heading detection is heuristic (regex), not ML-based — fast but
        may miss non-standard headings in complex layouts
      - Overlap is added only when a single paragraph exceeds MAX_CHUNK_CHARS,
        not as a blanket strategy — avoids duplicating content unnecessarily
    """

    def chunk(
        self,
        text:        str,
        tenant_id:   UUID,
        document_id: str,
        source_key:  str,
        page_map:    dict[int, int] | None = None,   # char_offset → page_number
        extra_meta:  dict | None = None,
    ) -> list[ChunkResult]:
        """
        Segment `text` into semantic chunks.

        Args:
            text:        Full document text (all pages concatenated)
            tenant_id:   From JWT — enforced tenant isolation
            document_id: UUID of the parent Document record
            source_key:  S3 key for citation metadata
            page_map:    Optional mapping from char offset to page number.
                         If None, all chunks get page_number=1.
            extra_meta:  Additional metadata to include in every chunk.

        Returns:
            Ordered list of ChunkResult (chunk_index 0, 1, 2, …)
        """
        text = _normalize_text(text)
        if not text.strip():
            logger.warning("SemanticChunker: empty text for doc=%s", document_id)
            return []

        # Step 1: Detect headings and split into sections
        sections = self._split_into_sections(text)

        # Step 2: Split each section into paragraph-level pieces
        raw_chunks = self._sections_to_chunks(sections)

        # Step 3: Enforce size limits (merge short, split long)
        sized_chunks = self._enforce_size_limits(raw_chunks)

        # Step 4: Build ChunkResult objects
        results: list[ChunkResult] = []
        for idx, (chunk_text, heading) in enumerate(sized_chunks):
            char_offset = text.find(chunk_text[:40]) if chunk_text else 0
            page_num = _lookup_page(char_offset, page_map)

            chunk_id = _make_chunk_id(str(tenant_id), document_id, idx)

            results.append(ChunkResult(
                chunk_id=chunk_id,
                tenant_id=tenant_id,
                document_id=document_id,
                chunk_index=idx,
                text=chunk_text,
                char_count=len(chunk_text),
                token_est=max(1, len(chunk_text) // 4),
                page_number=page_num,
                source_key=source_key,
                heading=heading,
                metadata={
                    "tenant_id":   str(tenant_id),
                    "document_id": document_id,
                    "chunk_index": idx,
                    "page_number": page_num,
                    "source_key":  source_key,
                    "heading":     heading,
                    "char_count":  len(chunk_text),
                    "token_est":   max(1, len(chunk_text) // 4),
                    **(extra_meta or {}),
                },
            ))

        logger.info(
            "SemanticChunker | doc=%s chunks=%d avg_chars=%.0f",
            document_id, len(results),
            sum(c.char_count for c in results) / max(1, len(results)),
        )
        return results

    # ------------------------------------------------------------------
    # Section detection
    # ------------------------------------------------------------------

    def _split_into_sections(self, text: str) -> list[tuple[str, str]]:
        """
        Split text into (section_text, heading) pairs.
        Heading is the nearest preceding heading-like line.

        Returns list of (text_block, heading_str) tuples.
        """
        lines = text.split("\n")
        sections: list[tuple[str, str]] = []

        current_heading = ""
        current_lines:  list[str] = []

        for line in lines:
            stripped = line.strip()
            is_heading = bool(
                stripped
                and len(stripped) >= MIN_HEADING_LEN
                and _HEADING_RE.match(stripped)
            )

            if is_heading:
                # Save the current block
                if current_lines:
                    block = "\n".join(current_lines).strip()
                    if block:
                        sections.append((block, current_heading))
                # Start a new section under this heading
                current_heading = stripped
                current_lines = []
            else:
                current_lines.append(line)

        # Flush the last section
        if current_lines:
            block = "\n".join(current_lines).strip()
            if block:
                sections.append((block, current_heading))

        if not sections:
            sections = [(text, "")]

        return sections

    # ------------------------------------------------------------------
    # Paragraph → sentence splitting
    # ------------------------------------------------------------------

    def _sections_to_chunks(
        self, sections: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """
        Convert sections to paragraph-level pieces, using spaCy
        sentence segmentation within long paragraphs.
        """
        results: list[tuple[str, str]] = []
        nlp = _get_nlp()

        for section_text, heading in sections:
            # Split into paragraphs at blank lines
            paragraphs = re.split(r"\n\s*\n", section_text)

            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue

                if len(para) <= MAX_CHUNK_CHARS:
                    # Paragraph fits in one chunk — keep whole
                    results.append((para, heading))
                else:
                    # Long paragraph: split at sentence boundaries
                    sentences = self._split_sentences(para, nlp)
                    results.extend((s, heading) for s in sentences if s.strip())

        return results

    def _split_sentences(self, text: str, nlp) -> list[str]:
        """
        Split text into sentences using spaCy (if available)
        or regex (fallback). Returns sentence strings.
        """
        if nlp is not None:
            try:
                doc = nlp(text)
                return [sent.text.strip() for sent in doc.sents if sent.text.strip()]
            except Exception as exc:
                logger.warning("spaCy sentence split failed: %s — using regex", exc)

        # Regex fallback: split at ". " or "? " or "! "
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

    # ------------------------------------------------------------------
    # Size enforcement
    # ------------------------------------------------------------------

    def _enforce_size_limits(
        self, chunks: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """
        Merge very short chunks with neighbours; hard-split chunks that
        exceed MAX_CHUNK_CHARS.

        Overlap is added ONLY when hard-splitting a chunk — not globally.
        This avoids the RAG anti-pattern of embedding near-duplicate content.
        """
        # Pass 1: Merge short chunks (< MIN_CHUNK_CHARS) with next
        merged: list[tuple[str, str]] = []
        buffer_text  = ""
        buffer_head  = ""

        for text, heading in chunks:
            if not buffer_text:
                buffer_text = text
                buffer_head = heading
            elif len(buffer_text) < MIN_CHUNK_CHARS:
                # Merge: keep the non-empty heading
                buffer_text += "\n\n" + text
                buffer_head = buffer_head or heading
            else:
                merged.append((buffer_text, buffer_head))
                buffer_text = text
                buffer_head = heading

        if buffer_text:
            merged.append((buffer_text, buffer_head))

        # Pass 2: Hard-split oversized chunks with overlap
        final: list[tuple[str, str]] = []
        for text, heading in merged:
            if len(text) <= MAX_CHUNK_CHARS:
                final.append((text, heading))
            else:
                final.extend(self._hard_split(text, heading))

        return final

    def _hard_split(self, text: str, heading: str) -> list[tuple[str, str]]:
        """
        Split a text that exceeds MAX_CHUNK_CHARS into overlapping windows.
        Overlap of OVERLAP_CHARS preserves continuity at the split boundary.
        """
        parts: list[tuple[str, str]] = []
        start = 0
        while start < len(text):
            end = start + MAX_CHUNK_CHARS
            # Try to break at a sentence boundary near the end
            if end < len(text):
                # Look backwards for the last ". " before the cutoff
                boundary = text.rfind(". ", start, end)
                if boundary != -1 and boundary > start + MIN_CHUNK_CHARS:
                    end = boundary + 1   # include the period
                # else: hard split at MAX_CHUNK_CHARS

            chunk = text[start:end].strip()
            if chunk:
                parts.append((chunk, heading))

            # Next window starts OVERLAP_CHARS before the end
            start = max(start + 1, end - OVERLAP_CHARS)

        return parts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """
    Normalize Unicode, strip control characters, collapse excess whitespace.
    Preserves paragraph breaks (double newlines).
    """
    # Normalize Unicode (NFC form — consistent character composition)
    text = unicodedata.normalize("NFC", text)
    # Replace non-breaking spaces, zero-width chars, etc.
    text = re.sub(r"[\u00a0\u200b\u200c\u200d\ufeff]", " ", text)
    # Collapse 3+ newlines to double newline (preserve paragraph breaks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _lookup_page(char_offset: int, page_map: dict[int, int] | None) -> int:
    """
    Look up the page number for a given character offset.
    page_map keys are the starting char offset of each page.
    Returns 1 if page_map is None or offset not found.
    """
    if not page_map:
        return 1
    page = 1
    for offset_start, page_num in sorted(page_map.items()):
        if char_offset >= offset_start:
            page = page_num
        else:
            break
    return page


def _make_chunk_id(tenant_id: str, document_id: str, chunk_index: int) -> str:
    """
    Deterministic chunk ID: sha256(tenant_id:document_id:chunk_index).
    Determinism enables idempotent re-processing — upserting the same chunk
    twice doesn't create duplicates in the vector store.
    """
    import hashlib
    raw = f"{tenant_id}:{document_id}:{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def build_page_map(pages_text: list[tuple[int, str]]) -> dict[int, int]:
    """
    Build a char_offset → page_number map from a list of (page_num, text) tuples.
    Used by the orchestrator to pass page context to the chunker.

    Example:
        build_page_map([(1, "intro text"), (2, "body text")])
        → {0: 1, 10: 2}
    """
    page_map: dict[int, int] = {}
    offset = 0
    for page_num, text in pages_text:
        page_map[offset] = page_num
        offset += len(text) + 2   # +2 for the "\n\n" separator
    return page_map
