from __future__ import annotations

import os
from celery import Celery

# On Vercel subscriber services CELERY_BROKER_URL is provided automatically;
# vercel:// uses Vercel Queues. A different broker can still be supplied locally.
app = Celery(
    "signalyth-worker",
    broker=os.getenv("CELERY_BROKER_URL", "vercel://"),
)
app.conf.update(
    accept_content=["json"],
    result_backend=None,
    result_serializer="json",
    task_ignore_result=True,
    task_serializer="json",
)
