"""
Celery application entrypoint for BranchGuard-AI.

Run a worker with:
    celery -A branchguard worker -l info -Q branchguard.default --concurrency=4
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "branchguard.settings")

app = Celery("branchguard")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
