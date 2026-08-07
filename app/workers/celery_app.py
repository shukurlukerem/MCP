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
    # Tasks are published from inside a web request, so an unreachable broker must
    # surface as an error the API can report — not as a socket that blocks until
    # the OS gives up minutes later, by which point the caller has seen a 502.
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.5,
        "interval_max": 1,
    },
)
