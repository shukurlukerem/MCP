from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "mcp_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,           # re-queue on worker crash, never silently drop
    worker_prefetch_multiplier=1,  # fair dispatch; LLM tasks run 30–120s each
    result_expires=3600,
)
