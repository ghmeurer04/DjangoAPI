"""Rotas do projeto.

Concentradas aqui, sem `urls.py` por app: o projeto expõe um recurso só.
"""

from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from seguradoras.views import SeguradoraViewSet

router = DefaultRouter()
router.register("seguradoras", SeguradoraViewSet, basename="seguradora")

urlpatterns = [
    # GET  /api/v1/seguradoras/
    # POST /api/v1/seguradoras/importar/
    path("api/v1/", include(router.urls)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("", RedirectView.as_view(pattern_name="swagger-ui", permanent=False)),
]
