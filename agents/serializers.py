from rest_framework import serializers

from agents.models import AnalysisRun, Repository, SandboxBranch


class RepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Repository
        fields = [
            "id",
            "full_name",
            "default_branch",
            "base_sandbox_image",
            "test_command",
            "is_active",
            "created_at",
        ]


class SandboxBranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = SandboxBranch
        fields = [
            "id",
            "branch_id",
            "hypothesis_label",
            "hypothesis_description",
            "patch_diff",
            "test_status",
            "execution_time_ms",
            "memory_used_mb",
            "created_at",
        ]


class AnalysisRunSerializer(serializers.ModelSerializer):
    branches = SandboxBranchSerializer(many=True, read_only=True)
    repository = RepositorySerializer(read_only=True)

    class Meta:
        model = AnalysisRun
        fields = [
            "id",
            "repository",
            "github_run_id",
            "pull_request_number",
            "triggering_commit_sha",
            "status",
            "error_summary",
            "winning_branch",
            "resulting_pr_url",
            "started_at",
            "completed_at",
            "created_at",
            "branches",
        ]


class AnalysisRunListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views (no nested branch logs)."""

    repository_name = serializers.CharField(source="repository.full_name", read_only=True)

    class Meta:
        model = AnalysisRun
        fields = [
            "id",
            "repository_name",
            "github_run_id",
            "status",
            "resulting_pr_url",
            "started_at",
            "completed_at",
            "created_at",
        ]
