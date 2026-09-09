"""
agents/tasks.py

The core Celery pipeline: turns a failing CI build into a verified,
auto-opened pull request.

    process_ci_failure_task(run_id)
        Step 1: Load the failing AnalysisRun + its raw CI log.
        Step 2: Ask Nemotron 3 (via NemotronReasoningClient) for N parallel
                bug-fix hypotheses.
        Step 3: Boot a base Nebius Sandbox and fork it into N branches.
        Step 4: Apply each patch + run the test suite in parallel.
        Step 5: Pick the best passing branch (lowest resource usage), open
                a GitHub PR with the winning diff + full benchmark table,
                and tear down all sandbox branches.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from agents.github_client import open_pull_request
from agents.models import AnalysisRun, SandboxBranch
from agents.nebius_client import NebiusSandboxClient, NemotronReasoningClient

logger = logging.getLogger("agents")

NUM_PARALLEL_HYPOTHESES = 3


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_ci_failure_task(self, run_id: str):
    """
    End-to-end self-healing pipeline for a single failing CI build.
    Idempotent-ish: safe to retry, since sandbox branches are namespaced
    per attempt and losing branches are always torn down.
    """
    try:
        run = AnalysisRun.objects.select_related("repository").get(id=run_id)
    except AnalysisRun.DoesNotExist:
        logger.error("AnalysisRun %s not found; aborting task.", run_id)
        return

    run.status = AnalysisRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    reasoning_client = NemotronReasoningClient()
    sandbox_client = NebiusSandboxClient()

    base_sandbox_id = None
    try:
        # ------------------------------------------------------------------
        # Step 1: Root-cause summary (cheap, fast, shown in dashboard early)
        # ------------------------------------------------------------------
        run.error_summary = reasoning_client.summarize_root_cause(run.primary_log)
        run.save(update_fields=["error_summary"])

        # ------------------------------------------------------------------
        # Step 2: Generate N parallel bug-fix hypotheses via Nemotron 3
        # ------------------------------------------------------------------
        repo_context = _build_repo_context(run.repository)
        hypotheses = reasoning_client.generate_fix_hypotheses(
            error_log=run.primary_log,
            repo_context=repo_context,
            num_hypotheses=NUM_PARALLEL_HYPOTHESES,
        )

        if not hypotheses:
            raise RuntimeError("Nemotron returned zero fix hypotheses.")

        # ------------------------------------------------------------------
        # Step 3: Boot base sandbox, fork N branches (Nebius Sandboxes API)
        # ------------------------------------------------------------------
        base_sandbox_id = sandbox_client.init_base_sandbox(
            image=run.repository.base_sandbox_image,
            repo_full_name=run.repository.full_name,
            commit_sha=run.triggering_commit_sha or run.repository.default_branch,
        )

        branch_records: list[SandboxBranch] = []
        for hypothesis in hypotheses:
            sandbox_branch_id = sandbox_client.fork_branch(
                base_sandbox_id, branch_label=hypothesis.label
            )
            branch_records.append(
                SandboxBranch.objects.create(
                    run=run,
                    branch_id=sandbox_branch_id,
                    hypothesis_label=hypothesis.label,
                    hypothesis_description=hypothesis.description,
                    patch_diff=hypothesis.patch_diff,
                    test_status=SandboxBranch.TestStatus.PENDING,
                )
            )

        # ------------------------------------------------------------------
        # Step 4: Apply patches + run tests in parallel across all branches
        # ------------------------------------------------------------------
        _run_branches_in_parallel(sandbox_client, run.repository.test_command, branch_records)

        # ------------------------------------------------------------------
        # Step 5: Select winner, open PR, tear down losing branches
        # ------------------------------------------------------------------
        winner = _select_winning_branch(branch_records)

        if winner is None:
            run.status = AnalysisRun.Status.FAILED
            logger.warning("Run %s: no branch passed its test suite.", run.id)
        else:
            pr_url = open_pull_request(
                repository=run.repository,
                run=run,
                winning_branch=winner,
                all_branches=branch_records,
            )
            run.winning_branch = winner
            run.resulting_pr_url = pr_url
            run.status = AnalysisRun.Status.COMPLETED

        # Tear down every sandbox branch (winner included — the PR now owns
        # the diff, the sandbox has done its job).
        for branch in branch_records:
            sandbox_client.teardown_branch(branch.branch_id)

    except Exception as exc:  # noqa: BLE001 — top-level pipeline guard
        logger.exception("process_ci_failure_task failed for run %s: %s", run_id, exc)
        run.status = AnalysisRun.Status.FAILED
        run.save(update_fields=["status"])
        raise self.retry(exc=exc)
    else:
        run.completed_at = timezone.now()
        run.save()
    finally:
        if base_sandbox_id:
            sandbox_client.teardown_base(base_sandbox_id)


# ==========================================================================
# Helpers
# ==========================================================================


def _build_repo_context(repository) -> str:
    """
    Lightweight repo context passed to Nemotron alongside the error log.
    In production this would pull relevant file snippets via the GitHub API
    (e.g. files referenced in the traceback); kept minimal here for clarity.
    """
    return (
        f"Repository: {repository.full_name}\n"
        f"Default branch: {repository.default_branch}\n"
        f"Test command: {repository.test_command}\n"
    )


def _run_branches_in_parallel(
    sandbox_client: NebiusSandboxClient,
    test_command: str,
    branch_records: list[SandboxBranch],
) -> None:
    """Apply each branch's patch and run its test suite concurrently."""

    def _execute(branch: SandboxBranch) -> None:
        branch.test_status = SandboxBranch.TestStatus.RUNNING
        branch.save(update_fields=["test_status"])
        try:
            sandbox_client.apply_patch(branch.branch_id, branch.patch_diff)
            result = sandbox_client.run_tests(branch.branch_id, test_command)
            branch.test_status = (
                SandboxBranch.TestStatus.PASSED
                if result.passed
                else SandboxBranch.TestStatus.FAILED
            )
            branch.stdout_log = result.stdout
            branch.stderr_log = result.stderr
            branch.execution_time_ms = result.execution_time_ms
            branch.memory_used_mb = result.memory_used_mb
        except Exception as exc:  # noqa: BLE001
            logger.exception("Branch %s errored: %s", branch.branch_id, exc)
            branch.test_status = SandboxBranch.TestStatus.ERROR
            branch.stderr_log = str(exc)
        finally:
            branch.save()

    # Nebius Sandboxes are independent VM forks, so we fan out the
    # apply-patch + run-tests cycle across a thread pool for true wall-clock
    # parallelism (each call blocks on network I/O, not local CPU).
    with ThreadPoolExecutor(max_workers=len(branch_records) or 1) as pool:
        futures = [pool.submit(_execute, branch) for branch in branch_records]
        for future in as_completed(futures):
            future.result()  # re-raise any unexpected exception


def _select_winning_branch(branch_records: list[SandboxBranch]) -> SandboxBranch | None:
    """
    Winner = passed the test suite AND has the lowest resource footprint
    (memory first, then execution time, as a tie-breaker).
    """
    passing = [b for b in branch_records if b.test_status == SandboxBranch.TestStatus.PASSED]
    if not passing:
        return None
    return min(
        passing,
        key=lambda b: (b.memory_used_mb or float("inf"), b.execution_time_ms or float("inf")),
    )
