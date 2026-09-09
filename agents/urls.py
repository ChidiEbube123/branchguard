from django.urls import include, path
from rest_framework.routers import DefaultRouter

from agents.views import AnalysisRunViewSet, GitHubWebhookView

router = DefaultRouter()
router.register(r"runs", AnalysisRunViewSet, basename="analysisrun")

urlpatterns = [
    path("webhooks/github/", GitHubWebhookView.as_view(), name="github-webhook"),
    path("", include(router.urls)),
]
