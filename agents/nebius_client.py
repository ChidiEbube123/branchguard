"""
agents/nebius_client.py

Thin client wrappers around the two Nebius Token Factory surfaces BranchGuard
depends on:

    1. NemotronReasoningClient  -> OpenAI-compatible chat completions endpoint,
                                    used to run NVIDIA Nemotron 3 (Ultra/Super)
                                    and get back structured bug-fix hypotheses.

    2. NebiusSandboxClient       -> REST client for Nebius Token Factory
                                    Sandboxes: boots a base VM, forks it into
                                    N git-like parallel branches, applies a
                                    patch inside each, and runs the test suite.

Both classes read credentials/config from Django settings, which in turn
read from `os.environ` (see branchguard/settings.py). No key ever needs to
be hardcoded here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings
from openai import OpenAI

logger = logging.getLogger("agents")


# ==========================================================================
# Data classes
# ==========================================================================


@dataclass
class BugFixHypothesis:
    """One structured hypothesis returned by Nemotron 3."""

    label: str
    description: str
    patch_diff: str
    target_files: list[str] = field(default_factory=list)


@dataclass
class SandboxExecutionResult:
    """Result of running the test suite for a single sandbox branch."""

    branch_id: str
    passed: bool
    stdout: str
    stderr: str
    execution_time_ms: int
    memory_used_mb: float


# ==========================================================================
# 1. NVIDIA Nemotron 3 reasoning client (via Nebius Token Factory)
# ==========================================================================


class NemotronReasoningClient:
    """
    Wraps the Nebius Token Factory OpenAI-compatible endpoint to query
    NVIDIA Nemotron 3 (Ultra / Super) for parallel bug-fix hypotheses.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or settings.NEBIUS_API_KEY
        self.base_url = base_url or settings.NEBIUS_MODEL_BASE_URL
        self.model = model or settings.NEMOTRON_MODEL_NAME

        # === NEBIUS TOKEN FACTORY CALL (Model API) =========================
        # OpenAI-compatible client pointed at Nebius Token Factory. Any model
        # served on Token Factory (here: NVIDIA Nemotron 3) can be swapped in
        # via NEMOTRON_MODEL_NAME without touching call sites.
        # =====================================================================
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def generate_fix_hypotheses(
        self, error_log: str, repo_context: str, num_hypotheses: int = 3
    ) -> list[BugFixHypothesis]:
        """
        Ask Nemotron 3 for `num_hypotheses` independent, structured bug-fix
        candidates given a failing CI log and lightweight repo context
        (file tree / relevant source snippets).

        Returns a list of BugFixHypothesis, parsed from the model's
        JSON-only response.
        """
        system_prompt = (
            "You are an expert autonomous software engineering agent. "
            "You will be given a CI failure log and repository context. "
            f"Return EXACTLY {num_hypotheses} independent, materially different "
            "bug-fix hypotheses as a JSON array. Each element must have the keys: "
            "'label' (short name like 'Hypothesis A: Dependency fix'), "
            "'description' (root-cause reasoning), "
            "'patch_diff' (a valid unified diff), and "
            "'target_files' (list of file paths touched). "
            "Respond with ONLY the JSON array — no prose, no markdown fences."
        )
        user_prompt = (
            f"=== CI ERROR LOG ===\n{error_log}\n\n"
            f"=== REPOSITORY CONTEXT ===\n{repo_context}\n\n"
            f"Generate {num_hypotheses} distinct fix hypotheses now."
        )

        # === NEBIUS TOKEN FACTORY CALL (Nemotron 3 chat completion) ========
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=4000,
        )
        # =====================================================================

        raw_text = response.choices[0].message.content.strip()
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            parsed: list[dict[str, Any]] = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.error("Nemotron returned non-JSON payload: %s", raw_text[:500])
            raise

        hypotheses = [
            BugFixHypothesis(
                label=item.get("label", f"Hypothesis {i + 1}"),
                description=item.get("description", ""),
                patch_diff=item.get("patch_diff", ""),
                target_files=item.get("target_files", []),
            )
            for i, item in enumerate(parsed[:num_hypotheses])
        ]
        logger.info("Nemotron generated %d fix hypotheses", len(hypotheses))
        return hypotheses

    def summarize_root_cause(self, error_log: str) -> str:
        """Short human-readable root-cause summary, shown in the dashboard."""
        # === NEBIUS TOKEN FACTORY CALL (Nemotron 3 chat completion) ========
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "Summarize the root cause of this CI failure in 2-3 sentences.",
                },
                {"role": "user", "content": error_log},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        # =====================================================================
        return response.choices[0].message.content.strip()


# ==========================================================================
# 2. Nebius Token Factory Sandboxes client
# ==========================================================================


class NebiusSandboxClient:
    """
    REST wrapper around the Nebius Token Factory Sandboxes API. Handles
    booting a base VM, git-like branching/forking of sandbox state for
    parallel hypothesis execution, patch application, and test running.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.NEBIUS_API_KEY
        self.base_url = (base_url or settings.NEBIUS_SANDBOX_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    # -- internal helper ----------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        # === NEBIUS TOKEN FACTORY CALL (Sandboxes REST API) ================
        resp = self._session.request(method, url, timeout=120, **kwargs)
        # =====================================================================
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # -- lifecycle ------------------------------------------------------------
    def init_base_sandbox(self, image: str, repo_full_name: str, commit_sha: str) -> str:
        """
        Boot a fresh Nebius Token Factory Sandbox from `image`, checked out
        at `commit_sha` of `repo_full_name`. Returns the base sandbox id.
        """
        # === NEBIUS TOKEN FACTORY CALL: create base sandbox environment ====
        payload = {
            "image": image,
            "source": {"type": "github", "repo": repo_full_name, "ref": commit_sha},
        }
        data = self._request("POST", "/", json=payload)
        # =====================================================================
        sandbox_id = data["id"]
        logger.info("Initialized base sandbox %s for %s@%s", sandbox_id, repo_full_name, commit_sha)
        return sandbox_id

    def fork_branch(self, base_sandbox_id: str, branch_label: str) -> str:
        """
        Git-like fork of the base sandbox's filesystem/process state into an
        isolated branch, so N hypotheses can be tested concurrently without
        interfering with each other.
        """
        # === NEBIUS TOKEN FACTORY CALL: fork/branch sandbox state ==========
        data = self._request(
            "POST",
            f"/{base_sandbox_id}/branches",
            json={"label": branch_label},
        )
        # =====================================================================
        branch_id = data["branch_id"]
        logger.info("Forked sandbox branch %s from base %s", branch_id, base_sandbox_id)
        return branch_id

    def apply_patch(self, branch_id: str, patch_diff: str) -> None:
        """Apply a unified diff inside the given sandbox branch."""
        # === NEBIUS TOKEN FACTORY CALL: apply patch inside branch ==========
        self._request(
            "POST",
            f"/branches/{branch_id}/patch",
            json={"diff": patch_diff},
        )
        # =====================================================================

    def run_tests(self, branch_id: str, test_command: str) -> SandboxExecutionResult:
        """Execute the project's test suite inside the sandbox branch."""
        # === NEBIUS TOKEN FACTORY CALL: exec command + capture metrics =====
        data = self._request(
            "POST",
            f"/branches/{branch_id}/exec",
            json={"command": test_command, "collect_metrics": True},
        )
        # =====================================================================
        return SandboxExecutionResult(
            branch_id=branch_id,
            passed=data.get("exit_code", 1) == 0,
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            execution_time_ms=data.get("execution_time_ms", 0),
            memory_used_mb=data.get("memory_used_mb", 0.0),
        )

    def teardown_branch(self, branch_id: str) -> None:
        """Roll back / destroy a losing hypothesis branch to free resources."""
        # === NEBIUS TOKEN FACTORY CALL: destroy sandbox branch =============
        try:
            self._request("DELETE", f"/branches/{branch_id}")
        except requests.HTTPError:
            logger.warning("Failed to tear down sandbox branch %s (non-fatal)", branch_id)
        # =====================================================================

    def teardown_base(self, base_sandbox_id: str) -> None:
        """Tear down the base sandbox once a run is finished."""
        # === NEBIUS TOKEN FACTORY CALL: destroy base sandbox ================
        try:
            self._request("DELETE", f"/{base_sandbox_id}")
        except requests.HTTPError:
            logger.warning("Failed to tear down base sandbox %s (non-fatal)", base_sandbox_id)
        # =====================================================================
