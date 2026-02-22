"""
Text Extraction Orchestrator
════════════════════════════

Orchestrates the OCR strategy cascade and builds the page_map used
by the semantic chunker.

Strategy selection flow:
  1.  Try PyMuPDF (fast, in-process, native PDF text layer)
  2a. If avg_chars_per_page < threshold  →  document is scanned
  2b. Select OCR backend from settings:
        UNSTRUCTURED  → UnstructuredExtractor (default, on-premise)
        TEXTRACT      → TextractExtractor (AWS managed, high accuracy)
  3.  Return ExtractionResult containing:
        - full concatenated text
        - per-page texts (for page_map construction)
        - extraction method used
        - whether OCR was invoked
        - per-page confidence scores

This module is the only place that knows about the strategy cascade.
Workers and other callers only see ExtractionResult.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from app.processing.ocr import (
    ExtractionStrategyResult,
    PyMuPDFExtractor,
    TextractExtractor,
    UnstructuredExtractor,
)
from app.processing.chunking import build_page_map

logger = logging.getLogger(__name__)

# Environment-based OCR backend selection
# Set OCR_BACKEND=textract in production if Textract is preferred
_OCR_BACKEND = os.getenv("OCR_BACKEND", "unstructured").lower()

# Textract async threshold (pages): use StartDocumentTextDetection for >3 pages
_TEXTRACT_ASYNC_PAGE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """
    Full extraction output returned to the worker pipeline.

    full_text         : all pages concatenated with "\n\n" separator
    pages             : list of (page_number, page_text) sorted by page
    page_map          : char_offset → page_number (for chunker)
    strategy_used     : "pymupdf" | "unstructured" | "textract"
    used_ocr          : True if image-based OCR was invoked
    total_chars       : total character count
    elapsed_ms        : total extraction wall time (ms)
    page_count        : number of pages in the document
    avg_confidence    : average OCR confidence (0–1; -1 if N/A)
    """
    full_text:      str
    pages:          list[tuple[int, str]]    # [(page_num, text), ...]
    page_map:       dict[int, int]           # char_offset → page_number
    strategy_used:  str
    used_ocr:       bool
    total_chars:    int
    elapsed_ms:     float
    page_count:     int
    avg_confidence: float = -1.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class TextExtractorOrchestrator:
    """
    Stateless orchestrator — select and execute the right extraction strategy.

    Constructor args:
        s3_bucket  : bucket where the document lives (for Textract async jobs)
        s3_key     : S3 key of the document (for Textract async jobs)

    Usage:
        orchestrator = TextExtractorOrchestrator(s3_bucket, s3_key)
        result = await orchestrator.extract(pdf_bytes)
    """

    def __init__(self, s3_bucket: str = "", s3_key: str = "") -> None:
        self._s3_bucket = s3_bucket
        self._s3_key    = s3_key
        self._pymupdf   = PyMuPDFExtractor()

    async def extract(self, pdf_bytes: bytes) -> ExtractionResult:
        """
        Execute the strategy cascade and return a unified ExtractionResult.

        ┌─────────────────────────────────────────────────────────────────┐
        │  1. PyMuPDF   ──► avg_chars ≥ threshold?                        │
        │                         YES → done ✓                            │
        │                         NO  → document is scanned               │
        │                               │                                 │
        │  2. OCR_BACKEND == "textract" → TextractExtractor               │
        │     OCR_BACKEND == "unstructured" → UnstructuredExtractor       │
        │                               │                                 │
        │  3. If OCR also fails → return PyMuPDF partial result           │
        │     (empty text is better than crashing the worker)             │
        └─────────────────────────────────────────────────────────────────┘
        """
        t0 = time.monotonic()

        # ── Step 1: Try PyMuPDF ──────────────────────────────────────────
        pymupdf_result = await self._pymupdf.extract(pdf_bytes)

        logger.info(
            "Extraction | strategy=pymupdf pages=%d total_chars=%d "
            "avg_chars_per_page=%.0f is_scanned=%s",
            len(pymupdf_result.pages),
            pymupdf_result.total_chars,
            pymupdf_result.avg_chars_per_page,
            pymupdf_result.is_likely_scanned(),
        )

        if not pymupdf_result.is_likely_scanned():
            # Native text layer — no OCR needed
            return self._build_result(pymupdf_result, time.monotonic() - t0)

        # ── Step 2: OCR cascade ──────────────────────────────────────────
        logger.info(
            "Document appears scanned (avg %.0f chars/page < threshold). "
            "Falling back to OCR backend: %s",
            pymupdf_result.avg_chars_per_page,
            _OCR_BACKEND,
        )

        ocr_result = await self._run_ocr(pdf_bytes, pymupdf_result)

        if ocr_result and ocr_result.total_chars > 0:
            return self._build_result(ocr_result, time.monotonic() - t0)

        # ── Step 3: OCR also produced nothing — return PyMuPDF partial ───
        logger.error(
            "All extraction strategies failed for s3://%s/%s — returning partial text",
            self._s3_bucket, self._s3_key,
        )
        return self._build_result(pymupdf_result, time.monotonic() - t0)

    async def _run_ocr(
        self,
        pdf_bytes:     bytes,
        pymupdf_result: ExtractionStrategyResult,
    ) -> ExtractionStrategyResult | None:
        """Select and run the configured OCR backend."""
        if _OCR_BACKEND == "textract":
            extractor = TextractExtractor()
            page_count = len(pymupdf_result.pages)

            if page_count > _TEXTRACT_ASYNC_PAGE_THRESHOLD and self._s3_key:
                # Use async Textract job (document already on S3)
                logger.info(
                    "Textract async job: %d pages via s3://%s/%s",
                    page_count, self._s3_bucket, self._s3_key,
                )
                try:
                    return await extractor.extract_async_job(self._s3_bucket, self._s3_key)
                except Exception as exc:
                    logger.error("Textract async job failed: %s", exc, exc_info=True)
                    return None
            else:
                # Sync Textract (≤3 pages or no S3 key)
                return await extractor.extract(pdf_bytes)
        else:
            # Default: Unstructured.io (local or API)
            use_api = os.getenv("UNSTRUCTURED_USE_API", "false").lower() == "true"
            api_key = os.getenv("UNSTRUCTURED_API_KEY", "")
            extractor = UnstructuredExtractor(use_api=use_api, api_key=api_key)
            return await extractor.extract(pdf_bytes)

    def _build_result(
        self,
        strategy_result: ExtractionStrategyResult,
        elapsed_sec:     float,
    ) -> ExtractionResult:
        """Convert a strategy result into the unified ExtractionResult."""
        pages_tuples = [
            (p.page_number, p.text)
            for p in strategy_result.pages
        ]
        page_map = build_page_map(pages_tuples)
        full_text = "\n\n".join(p.text for p in strategy_result.pages if p.text.strip())

        # Compute average confidence (skip pages with no confidence)
        conf_values = [
            p.confidence for p in strategy_result.pages if p.confidence >= 0
        ]
        avg_conf = sum(conf_values) / len(conf_values) if conf_values else -1.0

        return ExtractionResult(
            full_text=full_text,
            pages=pages_tuples,
            page_map=page_map,
            strategy_used=strategy_result.strategy_name,
            used_ocr=strategy_result.used_ocr,
            total_chars=strategy_result.total_chars,
            elapsed_ms=elapsed_sec * 1000,
            page_count=len(strategy_result.pages),
            avg_confidence=round(avg_conf, 3),
        )
