"""
agents/views.py

GitHubWebhookView   -> ingests failing workflow_run events from GitHub
                       Actions and kicks off the async self-healing pipeline.
AnalysisRunViewSet  -> DRF read API for the dashboard: list active runs,
                       inspect branch comparisons + benchmark logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.models import AnalysisRun, Repository, SandboxBranch
from agents.serializers import (
    AnalysisRunListSerializer,
    AnalysisRunSerializer,
    SandboxBranchSerializer,
)
from agents.tasks import process_ci_failure_task

logger = logging.getLogger("agents")


def _verify_github_signature(request) -> bool:
    """Validate the X-Hub-Signature-256 header against GITHUB_WEBHOOK_SECRET."""
    secret = settings.GITHUB_WEBHOOK_SECRET
    if not secret:
        # No secret configured (e.g. local dev) — skip verification.
        return True

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


class GitHubWebhookView(APIView):
    """
    Receives GitHub `workflow_run` (and `check_run`) webhook events.
    On a `completed` event with `conclusion == "failure"`, creates an
    AnalysisRun and enqueues `process_ci_failure_task` on Celery.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, *args, **kwargs):
        if not _verify_github_signature(request):
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)

        event_type = request.headers.get("X-GitHub-Event", "")
        payload = request.data

        if event_type != "workflow_run":
            # Ack and ignore anything we don't care about (push, star, etc).
            return Response({"detail": f"Ignored event: {event_type}"}, status=status.HTTP_200_OK)

        workflow_run = payload.get("workflow_run", {})
        if workflow_run.get("status") != "completed" or workflow_run.get("conclusion") != "failure":
            return Response({"detail": "Not a failing completed run; ignored."}, status=status.HTTP_200_OK)

        repo_payload = payload.get("repository", {})
        full_name = repo_payload.get("full_name")
        if not full_name:
            return Response({"detail": "Missing repository.full_name"}, status=status.HTTP_400_BAD_REQUEST)

        repository, _ = Repository.objects.get_or_create(
            full_name=full_name,
            defaults={"default_branch": repo_payload.get("default_branch", "main")},
        )

        if not repository.is_active:
            return Response({"detail": "Repository monitoring disabled."}, status=status.HTTP_200_OK)

        run = AnalysisRun.objects.create(
            repository=repository,
            github_run_id=str(workflow_run.get("id", "")),
            triggering_commit_sha=workflow_run.get("head_sha", ""),
            primary_log=_extract_log_excerpt(payload),
            status=AnalysisRun.Status.PENDING,
        )

        # === Kick off the async self-healing pipeline (Celery -> Nebius) ===
        process_ci_failure_task.delay(str(run.id))
        # =====================================================================

        logger.info("Enqueued self-healing pipeline for run %s (%s)", run.id, full_name)
        return Response(
            {"detail": "Analysis run created.", "run_id": str(run.id)},
            status=status.HTTP_202_ACCEPTED,
        )


def _extract_log_excerpt(payload: dict) -> str:
    """
    GitHub webhook payloads don't include raw logs directly — a full
    implementation fetches them via the Actions "download logs" REST
    endpoint using the workflow_run id. Placeholder extraction here keeps
    the webhook handler synchronous and fast; log fetching can be moved
    into the Celery task itself if preferred.
    """
    workflow_run = payload.get("workflow_run", {})
    return json.dumps(
        {
            "name": workflow_run.get("name"),
            "id": workflow_run.get("id"),
            "html_url": workflow_run.get("html_url"),
            "head_sha": workflow_run.get("head_sha"),
            "note": "Fetch full logs via GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs",
        },
        indent=2,
    )


class AnalysisRunViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only dashboard API.

    GET /api/runs/            -> list of runs (lightweight)
    GET /api/runs/{id}/       -> full run detail incl. all sandbox branches
    GET /api/runs/{id}/branches/ -> just the branch benchmark comparison
    """

    queryset = AnalysisRun.objects.select_related("repository").prefetch_related("branches")

    def get_serializer_class(self):
        if self.action == "list":
            return AnalysisRunListSerializer
        return AnalysisRunSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def branches(self, request, pk=None):
        """Just the sandbox branch benchmark comparison for one run."""
        run = self.get_object()
        serializer = SandboxBranchSerializer(run.branches.all(), many=True)
        return Response(serializer.data)
