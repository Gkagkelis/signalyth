from __future__ import annotations

from app.worker import tasks
from app.worker.celery import app

__all__ = ["app", "tasks"]
