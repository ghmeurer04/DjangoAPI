"""Cache da listagem.

Módulo próprio porque tem clientes em duas camadas: a listagem (HTTP) lê e
escreve, o enriquecimento (serviço) invalida. Deixar isto em `views.py` faria
`services.py` importar a camada HTTP, fechando um ciclo.
"""

import hashlib
from urllib.parse import urlencode

from django.core.cache import cache

PREFIXO = "seguradoras:listagem"


def invalidar_listagem() -> None:
    """Torna obsoleta toda a listagem em cache.

    `clear()` porque este cache é exclusivo da aplicação e só guarda listagem.
    """
    cache.clear()


def chave_listagem(request) -> str:
    """Chave estável para uma requisição de listagem.

    Ordena as chaves, para `?uf=PR&nome=x` e `?nome=x&uf=PR` reusarem a mesma
    entrada, mas preserva a ordem dos valores de um mesmo parâmetro — o
    django-filter usa a última ocorrência, então `?uf=SP&uf=PR` e `?uf=PR&uf=SP`
    filtram diferente. O host entra porque as URLs de paginação são absolutas.
    """
    parametros = [
        (chave, valor)
        for chave, valores in sorted(request.query_params.lists())
        for valor in valores
    ]
    assinatura = f"{request.get_host()}?{urlencode(parametros)}"
    digest = hashlib.sha256(assinatura.encode()).hexdigest()[:16]
    return f"{PREFIXO}:{digest}"
