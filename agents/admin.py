from django.contrib import admin

from agents.models import AnalysisRun, Repository, SandboxBranch


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ("full_name", "default_branch", "is_active", "created_at")
    search_fields = ("full_name",)
    list_filter = ("is_active",)


class SandboxBranchInline(admin.TabularInline):
    model = SandboxBranch
    extra = 0
    readonly_fields = (
        "branch_id",
        "hypothesis_label",
        "test_status",
        "execution_time_ms",
        "memory_used_mb",
    )
    can_delete = False


@admin.register(AnalysisRun)
class AnalysisRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "repository",
        "status",
        "github_run_id",
        "resulting_pr_url",
        "created_at",
    )
    list_filter = ("status", "repository")
    search_fields = ("github_run_id", "triggering_commit_sha")
    inlines = [SandboxBranchInline]
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(SandboxBranch)
class SandboxBranchAdmin(admin.ModelAdmin):
    list_display = (
        "hypothesis_label",
        "run",
        "test_status",
        "execution_time_ms",
        "memory_used_mb",
    )
    list_filter = ("test_status",)
