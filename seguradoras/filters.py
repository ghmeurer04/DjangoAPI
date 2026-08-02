"""Filtros da listagem: UF e trecho do nome, como o enunciado pede."""

import django_filters

from seguradoras.models import Seguradora


class SeguradoraFilter(django_filters.FilterSet):
    nome = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Trecho do nome, sem diferenciar maiúsculas de minúsculas.",
    )
    # Normaliza o parâmetro em vez de usar `iexact`: este geraria UPPER(uf) na
    # consulta, e um btree não serve predicado com função — `seguradora_uf_idx`
    # ficaria inútil. O serializer já grava tudo em maiúsculas.
    uf = django_filters.CharFilter(
        method="filtrar_por_uf",
        label="Sigla da unidade federativa.",
    )

    class Meta:
        model = Seguradora
        # `status_enriquecimento` é extra: é o filtro que torna o resultado do
        # enriquecimento em background verificável pela própria API.
        fields = ("nome", "uf", "status_enriquecimento")

    def filtrar_por_uf(self, queryset, nome_campo, valor):
        return queryset.filter(uf=valor.strip().upper())
