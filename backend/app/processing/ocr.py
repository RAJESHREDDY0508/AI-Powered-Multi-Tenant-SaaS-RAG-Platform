"""
OCR Strategy Pattern  —  Text Extraction from PDFs
═══════════════════════════════════════════════════

Design: Strategy + Factory
──────────────────────────
The system tries strategies in order of speed and cost:

  Strategy 1: PyMuPDF (fitz)
    - Native PDF text layer extraction (microseconds per page)
    - Zero API calls — runs entirely in-process
    - Fails gracefully on scanned PDFs (returns empty string per page)

  Strategy 2: Unstructured.io
    - Open-source document intelligence library
    - Handles scanned PDFs, mixed content, complex layouts
    - Runs locally (Docker) or via cloud API
    - Best for enterprise deployments wanting on-premise control

  Strategy 3: AWS Textract
    - Managed cloud OCR with tables, forms, handwriting support
    - Higher accuracy than Unstructured for structured documents
    - Adds ~1–5s latency per page (API call)
    - Best for high-compliance environments already on AWS

Selection logic (in TextExtractorOrchestrator.extract()):
  1. Attempt PyMuPDF
  2. If total extracted text < MIN_TEXT_CHARS_PER_PAGE × page_count
     → document is likely scanned → fall back to Unstructured or Textract

Enterprise trade-off:
  Unstructured = zero per-call cost, runs in-cluster, GDPR-friendly
  Textract     = higher accuracy, pay-per-page, adds AWS dependency

Both are exposed via the same PageText dataclass interface —
callers never need to know which backend was used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import IO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# If average extracted chars per page is below this threshold,
# the document is classified as scanned / image-based.
MIN_CHARS_PER_PAGE_THRESHOLD = 50

# AWS Textract: max pages per synchronous API call
TEXTRACT_SYNC_MAX_PAGES = 3

# OCR timeout (seconds) — prevents worker stalls on pathological documents
OCR_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------

@dataclass
class PageText:
    """
    Text extracted from a single page.

    page_number  : 1-based page index
    text         : raw extracted text (may be empty for image-only pages)
    confidence   : extraction confidence score (0.0–1.0); -1.0 = not applicable
    extraction_method : "pymupdf" | "unstructured" | "textract"
    """
    page_number:       int
    text:              str
    confidence:        float = -1.0
    extraction_method: str  = "unknown"


@dataclass
class ExtractionStrategyResult:
    """
    Full result from a single strategy run.

    pages         : list of PageText (one per page)
    total_chars   : sum of len(p.text) for all pages
    strategy_name : which strategy produced this result
    elapsed_ms    : wall-clock time for the strategy (ms)
    used_ocr      : True if image-based OCR was invoked
    """
    pages:         list[PageText]
    total_chars:   int
    strategy_name: str
    elapsed_ms:    float
    used_ocr:      bool = False

    @property
    def full_text(self) -> str:
        """Concatenate all pages with page separators."""
        return "\n\n".join(p.text for p in self.pages if p.text.strip())

    @property
    def avg_chars_per_page(self) -> float:
        if not self.pages:
            return 0.0
        return self.total_chars / len(self.pages)

    def is_likely_scanned(self) -> bool:
        """Return True if the document appears to be image-based."""
        return self.avg_chars_per_page < MIN_CHARS_PER_PAGE_THRESHOLD


# ---------------------------------------------------------------------------
# Abstract strategy
# ---------------------------------------------------------------------------

class BaseTextExtractor(ABC):
    """
    Abstract base for text extraction strategies.

    All implementations:
      - Accept raw PDF bytes (never a file path — keeps workers stateless)
      - Return ExtractionStrategyResult
      - Handle their own errors internally (log + return partial results)
      - Are safe for concurrent use (no shared mutable state)
    """

    @property
    @abstractmethod
    def strategy_name(self) -> str:
        """Unique name for logging and metrics."""

    @abstractmethod
    async def extract(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        """
        Extract text from a PDF document provided as raw bytes.

        Must NOT raise exceptions — return partial/empty result on failure
        so the caller can fall through to the next strategy.
        """


# ---------------------------------------------------------------------------
# Strategy 1: PyMuPDF (fitz)
# ---------------------------------------------------------------------------

class PyMuPDFExtractor(BaseTextExtractor):
    """
    Fastest strategy — uses PyMuPDF to read the native PDF text layer.

    Advantages:
      - Pure Python, zero API calls, sub-millisecond per page
      - Preserves text order (reading order), headings, footnotes
      - Extracts page count, bounding boxes (available via blocks)

    Limitations:
      - Cannot OCR image-only pages (returns empty string for those)
      - Struggles with multi-column layouts (text order may be wrong)
      - Encrypted PDFs return empty (password-protection)

    Thread-safety: fitz.open() returns an independent document object
    per call — safe for concurrent use.
    """

    @property
    def strategy_name(self) -> str:
        return "pymupdf"

    async def extract(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()

        try:
            result = await loop.run_in_executor(None, self._extract_sync, pdf_bytes)
        except Exception as exc:
            logger.warning("PyMuPDF extraction failed: %s", exc)
            result = ExtractionStrategyResult(
                pages=[], total_chars=0,
                strategy_name=self.strategy_name,
                elapsed_ms=0.0, used_ocr=False,
            )

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "PyMuPDF | pages=%d total_chars=%d avg_chars_per_page=%.0f elapsed_ms=%.0f",
            len(result.pages), result.total_chars,
            result.avg_chars_per_page, result.elapsed_ms,
        )
        return result

    def _extract_sync(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        """Blocking extraction — runs in thread executor."""
        import fitz  # PyMuPDF; imported here to avoid module-level import cost

        pages: list[PageText] = []

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page_num, page in enumerate(doc, start=1):
                # get_text("text") returns plain text preserving reading order.
                # "blocks" mode returns [(x0,y0,x1,y1,text,block_no,block_type)]
                # — use "text" for simplicity, "blocks" for heading detection.
                raw = page.get_text("text") or ""
                pages.append(PageText(
                    page_number=page_num,
                    text=raw.strip(),
                    confidence=-1.0,
                    extraction_method=self.strategy_name,
                ))

        total = sum(len(p.text) for p in pages)
        return ExtractionStrategyResult(
            pages=pages, total_chars=total,
            strategy_name=self.strategy_name,
            elapsed_ms=0.0, used_ocr=False,
        )


# ---------------------------------------------------------------------------
# Strategy 2: Unstructured.io
# ---------------------------------------------------------------------------

class UnstructuredExtractor(BaseTextExtractor):
    """
    OCR fallback using the open-source Unstructured library.

    Unstructured uses a cascade of ML models to detect document layout,
    extract text from images, tables, and mixed-content pages.

    Deployment options:
      A) Local (pip install unstructured[pdf]) — runs in-process using
         pdfminer + pytesseract + detectron2 for layout analysis.
         Requires poppler + tesseract system packages in the container.

      B) Unstructured API (hosted) — POST to https://api.unstructured.io
         or self-hosted via their Docker image.
         Requires UNSTRUCTURED_API_KEY env var.

    We default to local mode; set UNSTRUCTURED_USE_API=true for cloud.

    Enterprise trade-off:
      Local mode = GDPR-friendly, zero egress, but high memory (2–4 GB).
      API mode   = low memory, faster, but data leaves the cluster.
    """

    def __init__(self, use_api: bool = False, api_key: str = "") -> None:
        self._use_api = use_api
        self._api_key = api_key

    @property
    def strategy_name(self) -> str:
        return "unstructured"

    async def extract(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        loop = asyncio.get_event_loop()
        t0   = time.monotonic()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._extract_sync, pdf_bytes),
                timeout=OCR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Unstructured OCR timed out after %ds", OCR_TIMEOUT_SECONDS)
            result = ExtractionStrategyResult(
                pages=[], total_chars=0,
                strategy_name=self.strategy_name,
                elapsed_ms=0.0, used_ocr=True,
            )
        except Exception as exc:
            logger.error("Unstructured extraction failed: %s", exc, exc_info=True)
            result = ExtractionStrategyResult(
                pages=[], total_chars=0,
                strategy_name=self.strategy_name,
                elapsed_ms=0.0, used_ocr=True,
            )

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Unstructured | pages=%d total_chars=%d elapsed_ms=%.0f",
            len(result.pages), result.total_chars, result.elapsed_ms,
        )
        return result

    def _extract_sync(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        """Blocking OCR extraction — runs in thread executor."""
        import io
        from unstructured.partition.pdf import partition_pdf

        # unstructured expects a file-like object
        pdf_file = io.BytesIO(pdf_bytes)

        # strategy="hi_res" uses layout detection ML models.
        # strategy="fast"   uses pdfminer (fast, no ML, similar to PyMuPDF).
        # strategy="ocr_only" forces pytesseract on every page.
        elements = partition_pdf(
            file=pdf_file,
            strategy="hi_res",               # full ML pipeline for scanned docs
            include_page_breaks=True,        # inject PageBreak elements
            infer_table_structure=True,      # extract table HTML
            extract_images_in_pdf=False,     # skip inline image extraction
        )

        # Group elements by page number
        pages_dict: dict[int, list[str]] = {}
        for elem in elements:
            page_num = elem.metadata.page_number if elem.metadata else 1
            page_num = page_num or 1
            pages_dict.setdefault(page_num, [])

            # Tables are returned as HTML; convert to pipe-delimited text
            text = str(elem) if elem.category != "Table" else elem.metadata.text_as_html or str(elem)
            if text.strip():
                pages_dict[page_num].append(text.strip())

        pages = [
            PageText(
                page_number=pn,
                text="\n".join(texts),
                confidence=0.85,              # Unstructured doesn't expose per-element confidence
                extraction_method=self.strategy_name,
            )
            for pn, texts in sorted(pages_dict.items())
        ]
        total = sum(len(p.text) for p in pages)

        return ExtractionStrategyResult(
            pages=pages, total_chars=total,
            strategy_name=self.strategy_name,
            elapsed_ms=0.0, used_ocr=True,
        )


# ---------------------------------------------------------------------------
# Strategy 3: AWS Textract
# ---------------------------------------------------------------------------

class TextractExtractor(BaseTextExtractor):
    """
    AWS Textract — managed, high-accuracy OCR.

    Advantages over Unstructured:
      - Superior accuracy on structured forms, tables, handwriting
      - Native AWS integration (IAM, KMS, CloudTrail)
      - Asynchronous job API for large documents (>3 pages)
      - Returns bounding boxes + confidence scores per word

    Cost model:
      Textract charges per page processed (~$0.0015/page for DetectText).
      For a 10,000-document/day platform at avg. 15 pages = ~$225/day.
      Only invoke Textract when PyMuPDF returns insufficient text.

    Implementation:
      Sync API (detect_document_text) for ≤3 pages.
      Async API (start_document_text_detection) for >3 pages — polls
      until JobStatus=SUCCEEDED, with exponential backoff.

    IAM permissions required on the Celery worker task role:
      textract:DetectDocumentText
      textract:StartDocumentTextDetection
      textract:GetDocumentTextDetection
      s3:GetObject   (if passing S3Document instead of raw bytes)
    """

    def __init__(self, region: str = "us-east-1") -> None:
        self._region = region

    @property
    def strategy_name(self) -> str:
        return "textract"

    async def extract(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        loop = asyncio.get_event_loop()
        t0   = time.monotonic()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._extract_sync, pdf_bytes),
                timeout=OCR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Textract OCR timed out after %ds", OCR_TIMEOUT_SECONDS)
            result = ExtractionStrategyResult(
                pages=[], total_chars=0,
                strategy_name=self.strategy_name,
                elapsed_ms=0.0, used_ocr=True,
            )
        except Exception as exc:
            logger.error("Textract extraction failed: %s", exc, exc_info=True)
            result = ExtractionStrategyResult(
                pages=[], total_chars=0,
                strategy_name=self.strategy_name,
                elapsed_ms=0.0, used_ocr=True,
            )

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Textract | pages=%d total_chars=%d elapsed_ms=%.0f",
            len(result.pages), result.total_chars, result.elapsed_ms,
        )
        return result

    def _extract_sync(self, pdf_bytes: bytes) -> ExtractionStrategyResult:
        """
        Calls Textract DetectDocumentText (synchronous API, ≤3 page PDFs).
        For larger documents, the orchestrator should upload to S3 first and
        call StartDocumentTextDetection with an S3Object reference.
        """
        import boto3

        client = boto3.client("textract", region_name=self._region)

        response = client.detect_document_text(
            Document={"Bytes": pdf_bytes}
        )

        # Group LINE blocks by Page
        pages_dict: dict[int, list[str]] = {}
        word_confidences: dict[int, list[float]] = {}

        for block in response.get("Blocks", []):
            if block["BlockType"] not in ("LINE", "WORD"):
                continue

            page_num = block.get("Page", 1)
            confidence = block.get("Confidence", 0.0) / 100.0   # normalize to 0–1

            if block["BlockType"] == "LINE":
                pages_dict.setdefault(page_num, []).append(block.get("Text", ""))

            word_confidences.setdefault(page_num, []).append(confidence)

        pages = []
        for pn in sorted(pages_dict):
            text = "\n".join(pages_dict[pn])
            avg_conf = (
                sum(word_confidences[pn]) / len(word_confidences[pn])
                if word_confidences.get(pn) else 0.0
            )
            pages.append(PageText(
                page_number=pn,
                text=text,
                confidence=round(avg_conf, 3),
                extraction_method=self.strategy_name,
            ))

        total = sum(len(p.text) for p in pages)
        return ExtractionStrategyResult(
            pages=pages, total_chars=total,
            strategy_name=self.strategy_name,
            elapsed_ms=0.0, used_ocr=True,
        )

    async def extract_async_job(
        self,
        s3_bucket: str,
        s3_key: str,
    ) -> ExtractionStrategyResult:
        """
        Async Textract job for documents >3 pages.
        Uploads to S3 first (already done by the ingestion pipeline),
        then polls for completion with exponential back-off.

        Max poll wait: ~5 minutes (matches Celery soft_time_limit).
        """
        import boto3, time as _time

        client = boto3.client("textract", region_name=self._region)

        # Start async job
        job = client.start_document_text_detection(
            DocumentLocation={
                "S3Object": {"Bucket": s3_bucket, "Name": s3_key}
            }
        )
        job_id = job["JobId"]
        logger.info("Textract async job started: %s for s3://%s/%s", job_id, s3_bucket, s3_key)

        # Poll with exponential back-off (2s → 4s → 8s → max 30s)
        delay = 2
        max_delay = 30
        deadline = _time.monotonic() + OCR_TIMEOUT_SECONDS

        all_blocks: list[dict] = []
        next_token: str | None = None

        while _time.monotonic() < deadline:
            kwargs = {"JobId": job_id}
            if next_token:
                kwargs["NextToken"] = next_token

            result = client.get_document_text_detection(**kwargs)
            status = result["JobStatus"]

            if status == "SUCCEEDED":
                all_blocks.extend(result.get("Blocks", []))
                next_token = result.get("NextToken")
                if not next_token:
                    break   # all pages retrieved
            elif status == "FAILED":
                raise RuntimeError(f"Textract job {job_id} failed: {result.get('StatusMessage')}")
            else:
                # IN_PROGRESS — wait and retry
                _time.sleep(min(delay, max_delay))
                delay *= 2
        else:
            raise TimeoutError(f"Textract job {job_id} timed out after {OCR_TIMEOUT_SECONDS}s")

        # Parse blocks (same as sync path)
        return self._parse_blocks(all_blocks)

    def _parse_blocks(self, blocks: list[dict]) -> ExtractionStrategyResult:
        pages_dict: dict[int, list[str]] = {}
        for block in blocks:
            if block["BlockType"] == "LINE":
                pn = block.get("Page", 1)
                pages_dict.setdefault(pn, []).append(block.get("Text", ""))

        pages = [
            PageText(
                page_number=pn,
                text="\n".join(texts),
                confidence=0.9,
                extraction_method=self.strategy_name,
            )
            for pn, texts in sorted(pages_dict.items())
        ]
        total = sum(len(p.text) for p in pages)
        return ExtractionStrategyResult(
            pages=pages, total_chars=total,
            strategy_name=self.strategy_name,
            elapsed_ms=0.0, used_ocr=True,
        )
