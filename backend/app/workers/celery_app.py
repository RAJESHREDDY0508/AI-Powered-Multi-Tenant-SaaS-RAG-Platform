"""
Celery Application Factory

Configures the Celery app for async document processing.
Broker: RabbitMQ (amqp://) in production; Redis (redis://) as fallback for local dev.
Result backend: Redis (optional — tasks are fire-and-forget for status tracking via DB).

Queue topology:
  documents.ingest   — high priority, document ingestion pipeline
  documents.retry    — lower priority, re-queued failed documents
  system.health      — internal health-check tasks

SOC2 note: All task arguments are logged by Celery. Avoid passing raw file bytes
in task payloads — always pass S3 keys and load from storage within the worker.
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.signals import after_setup_logger, task_failure, task_postrun, task_prerun
from kombu import Exchange, Queue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Broker / backend URLs from environment
# ---------------------------------------------------------------------------

BROKER_URL: str = os.getenv(
    "CELERY_BROKER_URL",
    "amqp://guest:guest@localhost:5672//",  # RabbitMQ default
)
RESULT_BACKEND: str = os.getenv(
    "CELERY_RESULT_BACKEND",
    "redis://localhost:6379/1",
)

# ---------------------------------------------------------------------------
# Queue and exchange definitions
# ---------------------------------------------------------------------------

INGEST_EXCHANGE = Exchange("documents", type="direct", durable=True)

TASK_QUEUES = (
    Queue(
        "documents.ingest",
        exchange=INGEST_EXCHANGE,
        routing_key="documents.ingest",
        queue_arguments={"x-max-priority": 10},
        durable=True,
    ),
    Queue(
        "documents.retry",
        exchange=INGEST_EXCHANGE,
        routing_key="documents.retry",
        durable=True,
    ),
    Queue(
        "system.health",
        Exchange("system", type="direct"),
        routing_key="system.health",
        durable=True,
    ),
)

TASK_ROUTES = {
    "app.workers.tasks.process_document":         {"queue": "documents.ingest"},
    "app.workers.tasks.retry_failed_documents":   {"queue": "documents.retry"},
    "app.workers.tasks.health_check":             {"queue": "system.health"},
}

# ---------------------------------------------------------------------------
# Celery app factory
# ---------------------------------------------------------------------------

def create_celery_app() -> Celery:
    app = Celery("rag_platform")

    app.conf.update(
        # --- Broker / Backend ---
        broker_url=BROKER_URL,
        result_backend=RESULT_BACKEND,

        # --- Serialization (security: reject non-JSON messages) ---
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        event_serializer="json",

        # --- Queues ---
        task_queues=TASK_QUEUES,
        task_routes=TASK_ROUTES,
        task_default_queue="documents.ingest",
        task_default_exchange="documents",
        task_default_routing_key="documents.ingest",

        # --- Reliability ---
        task_acks_late=True,         # ack only after task completes (prevents message loss on crash)
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # one task at a time per worker (prevents overload)

        # --- Retries ---
        task_max_retries=3,
        task_default_retry_delay=60,    # seconds

        # --- Timeouts ---
        task_soft_time_limit=300,   # 5 min — sends SIGTERM, task can handle gracefully
        task_time_limit=360,        # 6 min — sends SIGKILL as final backstop

        # --- Result TTL ---
        result_expires=3600,   # 1 hour; we track state in PostgreSQL, not Celery results

        # --- Timezone ---
        timezone="UTC",
        enable_utc=True,

        # --- Beat schedule (retry scanner) ---
        beat_schedule={
            "retry-pending-documents-every-60s": {
                "task":     "app.workers.tasks.retry_failed_documents",
                "schedule": 60,  # every 60 seconds
                "options":  {"queue": "documents.retry"},
            },
        },

        # --- Worker ---
        worker_max_tasks_per_child=200,   # recycle workers to prevent memory bloat
        worker_disable_rate_limits=False,
    )

    # Auto-discover tasks module
    app.autodiscover_tasks(["app.workers"])

    return app


celery_app = create_celery_app()


# ---------------------------------------------------------------------------
# Celery signals — structured logging for SOC2 audit trail
# ---------------------------------------------------------------------------

@task_prerun.connect
def on_task_prerun(task_id, task, args, kwargs, **_):
    logger.info(
        "Task start | task_id=%s task=%s doc=%s tenant=%s",
        task_id, task.name,
        kwargs.get("document_id", "?"),
        kwargs.get("tenant_id", "?"),
    )


@task_postrun.connect
def on_task_postrun(task_id, task, args, kwargs, retval, state, **_):
    logger.info(
        "Task end | task_id=%s task=%s state=%s doc=%s",
        task_id, task.name, state, kwargs.get("document_id", "?"),
    )


@task_failure.connect
def on_task_failure(task_id, exception, args, kwargs, traceback, einfo, **_):
    logger.error(
        "Task failed | task_id=%s doc=%s error=%s",
        task_id, kwargs.get("document_id", "?"), exception,
        exc_info=True,
    )
