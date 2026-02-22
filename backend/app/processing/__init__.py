"""
Document Processing Package
════════════════════════════

Orchestrates the full post-upload ingestion pipeline:

  Text Extraction → Semantic Chunking → Embedding → Vector Upsert

Modules
───────
  ocr.py        Strategy pattern for text extraction (PyMuPDF → Unstructured → Textract)
  chunking.py   Semantic chunker (NLP-based paragraph/sentence segmentation)
  extractor.py  Orchestrator that selects the correct extraction strategy
  embeddings.py Batch embedding pipeline with retry logic and token accounting

Design principles
─────────────────
  • Every component is stateless and dependency-injected.
  • All heavy computation runs in the Celery worker, never in the API process.
  • Tenant isolation is enforced at every layer: extractor, vector store, audit log.
  • Observability is built-in: every step emits structured log lines.
"""

from app.processing.extractor import ExtractionResult, TextExtractorOrchestrator
from app.processing.chunking import ChunkResult, SemanticChunker
from app.processing.embeddings import EmbeddingPipeline, EmbeddingResult

__all__ = [
    "ExtractionResult",
    "TextExtractorOrchestrator",
    "ChunkResult",
    "SemanticChunker",
    "EmbeddingPipeline",
    "EmbeddingResult",
]
