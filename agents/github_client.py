"""
agents/github_client.py

Minimal GitHub REST wrapper used at the end of the pipeline to push the
winning sandbox branch's patch as a new branch + open a pull request with
a full benchmark diff across all tested hypotheses.
"""

from __future__ import annotations

import logging

import requests

from agents.models import AnalysisRun, Repository, SandboxBranch

logger = logging.getLogger("agents")

GITHUB_API_BASE = "https://api.github.com"


def open_pull_request(
    repository: Repository,
    run: AnalysisRun,
    winning_branch: SandboxBranch,
    all_branches: list[SandboxBranch],
) -> str:
    """
    Create a branch on GitHub containing the winning patch, then open a PR
    against the repository's default branch with a benchmark table
    comparing every hypothesis BranchGuard tried.

    Returns the URL of the created pull request.
    """
    headers = {
        "Authorization": f"Bearer {repository.github_installation_token}",
        "Accept": "application/vnd.github+json",
    }
    branch_name = f"branchguard-ai/fix-{run.id}"

    session = requests.Session()
    session.headers.update(headers)

    # 1) Resolve base branch SHA
    ref_resp = session.get(
        f"{GITHUB_API_BASE}/repos/{repository.full_name}/git/ref/heads/{repository.default_branch}"
    )
    ref_resp.raise_for_status()
    base_sha = ref_resp.json()["object"]["sha"]

    # 2) Create new branch pointing at base
    session.post(
        f"{GITHUB_API_BASE}/repos/{repository.full_name}/git/refs",
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    ).raise_for_status()

    # 3) Commit the winning patch to the new branch.
    #    (In production: apply the unified diff via the Git Trees/Blobs API
    #    or a temporary local checkout + push. Left as a single commit call
    #    site here for clarity/brevity.)
    _commit_patch(session, repository, branch_name, winning_branch.patch_diff)

    # 4) Open the PR with a benchmark table across all hypotheses
    pr_body = _build_pr_body(run, winning_branch, all_branches)
    pr_resp = session.post(
        f"{GITHUB_API_BASE}/repos/{repository.full_name}/pulls",
        json={
            "title": f"[BranchGuard-AI] Auto-fix for CI failure (run {run.github_run_id})",
            "head": branch_name,
            "base": repository.default_branch,
            "body": pr_body,
        },
    )
    pr_resp.raise_for_status()
    pr_url = pr_resp.json()["html_url"]
    logger.info("Opened PR %s for run %s", pr_url, run.id)
    return pr_url


def _commit_patch(session: requests.Session, repository: Repository, branch: str, diff: str) -> None:
    """Placeholder single-commit application of the unified diff."""
    # A real implementation parses `diff` and writes/updates blobs via the
    # Git Data API. Kept intentionally minimal for the hackathon prototype.
    logger.debug("Applying patch to %s@%s (%d bytes)", repository.full_name, branch, len(diff))


def _build_pr_body(run: AnalysisRun, winner: SandboxBranch, all_branches: list[SandboxBranch]) -> str:
    lines = [
        "### 🤖 BranchGuard-AI Autonomous Fix",
        "",
        f"**Root cause (Nemotron 3 analysis):** {run.error_summary}",
        "",
        f"**Winning hypothesis:** {winner.hypothesis_label}",
        winner.hypothesis_description,
        "",
        "### Benchmark across all parallel sandbox branches",
        "",
        "| Hypothesis | Status | Time (ms) | Memory (MB) |",
        "|---|---|---|---|",
    ]
    for b in all_branches:
        marker = " ✅ (selected)" if b.id == winner.id else ""
        lines.append(
            f"| {b.hypothesis_label}{marker} | {b.test_status} | "
            f"{b.execution_time_ms or '-'} | {b.memory_used_mb or '-'} |"
        )
    lines += [
        "",
        "_Generated automatically by BranchGuard-AI — parallel agentic "
        "tree exploration powered by NVIDIA Nemotron 3 on Nebius Token "
        "Factory Sandboxes._",
    ]
    return "\n".join(lines)
