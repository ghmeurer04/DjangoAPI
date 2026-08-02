"""ViewSet com os dois endpoints do desafio."""

from dataclasses import asdict

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from seguradoras import cache as cache_listagem
from seguradoras.filters import SeguradoraFilter
from seguradoras.models import Seguradora
from seguradoras.serializers import (
    ResumoImportacaoSerializer,
    SeguradoraImportItemSerializer,
    SeguradoraSerializer,
)
from seguradoras.services import agendar_enriquecimento, importar_seguradoras


@extend_schema_view(
    list=extend_schema(
        summary="Lista seguradoras",
        description=(
            "Listagem paginada, com filtros por UF e por trecho do nome. "
            "As respostas ficam em cache por um curto período e são "
            "invalidadas automaticamente a cada importação ou enriquecimento."
        ),
        tags=["seguradoras"],
    ),
)
class SeguradoraViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Seguradora.objects.all()
    serializer_class = SeguradoraSerializer
    filterset_class = SeguradoraFilter

    def list(self, request, *args, **kwargs):
        """Listagem com cache.

        Sobrescrito em vez de usar @cache_page: o decorator não oferece gancho
        de invalidação, e a importação precisa poder invalidar.
        """
        chave = cache_listagem.chave_listagem(request)
        dados = cache.get(chave)

        if dados is None:
            dados = super().list(request, *args, **kwargs).data
            cache.set(chave, dados, settings.CACHE_TTL)

        return Response(dados)

    @extend_schema(
        summary="Importa seguradoras em lote",
        description=(
            "Recebe uma lista de seguradoras e grava os dados básicos usando o "
            "CNPJ como chave: se o CNPJ já existir, o registro é atualizado em "
            "vez de duplicado.\n\n"
            "A validação é tudo-ou-nada: um item inválido rejeita o lote "
            "inteiro, e os erros vêm indexados pela posição no payload."
        ),
        request=SeguradoraImportItemSerializer(many=True),
        responses={201: ResumoImportacaoSerializer},
        tags=["seguradoras"],
        examples=[
            # Um item por exemplo: com `many=True` o drf-spectacular já envolve
            # o valor num array, e passar a lista pronta geraria [[{...}]].
            # CNPJs reais para o "Try it out" enriquecer de verdade.
            OpenApiExample(
                "CNPJ mascarado",
                value={"nome": "BB Seguridade", "cnpj": "00.000.000/0001-91", "uf": "DF"},
                request_only=True,
            ),
            OpenApiExample(
                "CNPJ sem máscara e UF em minúsculas",
                value={"nome": "Petrobras", "cnpj": "33000167000101", "uf": "rj"},
                request_only=True,
            ),
        ],
    )
    @action(detail=False, methods=["post"], url_path="importar")
    def importar(self, request):
        """Grava os dados básicos e responde já; o enriquecimento fica para depois."""
        serializer = SeguradoraImportItemSerializer(
            data=request.data, many=True, allow_empty=False
        )
        serializer.is_valid(raise_exception=True)

        resumo = importar_seguradoras(serializer.validated_data)
        cache_listagem.invalidar_listagem()

        agendados = agendar_enriquecimento(resumo.cnpjs)

        return Response(
            ResumoImportacaoSerializer(
                {**asdict(resumo), "enriquecimento_agendado": agendados}
            ).data,
            status=status.HTTP_201_CREATED,
        )
