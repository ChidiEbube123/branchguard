"""
agents/models.py

Core data model for BranchGuard-AI.

Repository        -> a tracked GitHub repo BranchGuard watches for CI failures.
AnalysisRun        -> one end-to-end "healing" run triggered by a failing build.
SandboxBranch      -> one of the N parallel hypothesis branches spun up inside
                       a Nebius Token Factory Sandbox for a given AnalysisRun.
"""

import uuid

from django.db import models


class Repository(models.Model):
    """A GitHub repository BranchGuard-AI is authorized to monitor and patch."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(
        max_length=255, unique=True, help_text="e.g. 'octo-org/octo-repo'"
    )
    default_branch = models.CharField(max_length=100, default="main")
    github_installation_token = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Short-lived GitHub App / PAT token used for push + PR creation.",
    )
    base_sandbox_image = models.CharField(
        max_length=255,
        default="python:3.11-slim",
        help_text="Base image Nebius Sandboxes should boot for this repo.",
    )
    test_command = models.CharField(
        max_length=255,
        default="pytest -q",
        help_text="Command run inside the sandbox to validate a candidate patch.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.full_name


class AnalysisRun(models.Model):
    """One self-healing pipeline execution triggered by a failing CI build."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="runs"
    )

    # Trigger provenance
    github_run_id = models.CharField(
        max_length=100,
        help_text="The failing GitHub Actions workflow_run.id that triggered this.",
    )
    pull_request_number = models.PositiveIntegerField(null=True, blank=True)
    triggering_commit_sha = models.CharField(max_length=64, blank=True, default="")

    # State
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    primary_log = models.TextField(
        blank=True, default="", help_text="Raw failing CI log payload."
    )
    error_summary = models.TextField(
        blank=True, default="", help_text="Nemotron-generated root-cause summary."
    )

    # Outcome
    winning_branch = models.ForeignKey(
        "SandboxBranch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    resulting_pr_url = models.URLField(blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["repository", "status"]),
        ]

    def __str__(self) -> str:
        return f"AnalysisRun({self.id}) [{self.status}] {self.repository.full_name}"


class SandboxBranch(models.Model):
    """
    One parallel bug-fix hypothesis executed inside an isolated Nebius Token
    Factory Sandbox branch (forked from the base sandbox state for the run).
    """

    class TestStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        ERROR = "ERROR", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="branches"
    )

    # Nebius Sandbox identity
    branch_id = models.CharField(
        max_length=255,
        help_text="Identifier returned by Nebius Token Factory Sandboxes API "
        "for this forked sandbox branch.",
    )
    hypothesis_label = models.CharField(
        max_length=50,
        help_text="e.g. 'Hypothesis A: Dependency fix'",
    )
    hypothesis_description = models.TextField(
        blank=True, default="", help_text="Nemotron's reasoning for this fix."
    )
    patch_diff = models.TextField(
        blank=True, default="", help_text="Unified diff proposed by Nemotron."
    )

    # Execution results
    test_status = models.CharField(
        max_length=20, choices=TestStatus.choices, default=TestStatus.PENDING
    )
    stdout_log = models.TextField(blank=True, default="")
    stderr_log = models.TextField(blank=True, default="")
    execution_time_ms = models.PositiveIntegerField(null=True, blank=True)
    memory_used_mb = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.hypothesis_label} ({self.test_status}) — run {self.run_id}"
