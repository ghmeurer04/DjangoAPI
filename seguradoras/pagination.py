"""Paginação da listagem.

Fora de `views.py` por restrição do DRF: ele resolve DEFAULT_PAGINATION_CLASS
no meio do import de `views.py`, o que fecharia um ciclo.
"""

from rest_framework.pagination import PageNumberPagination


class PaginacaoPadrao(PageNumberPagination):
    """20 por página (env `PAGE_SIZE`), ajustável por `?page_size=` até 100."""

    page_size_query_param = "page_size"
    max_page_size = 100
