"""Regras de negócio: as duas etapas do desafio, num arquivo só.

1. `importar_seguradoras` — importação em lote com upsert por CNPJ.
2. `enriquecer_pendentes` / `agendar_enriquecimento` — consulta à BrasilAPI
   fora do ciclo request/response.
"""

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field

from django.conf import settings
from django.db import close_old_connections, connection, transaction

from seguradoras import cache as cache_listagem
from seguradoras.brasilapi import BrasilAPIClient, BrasilAPIError, ErroPermanenteCNPJ
from seguradoras.models import Seguradora, StatusEnriquecimento

logger = logging.getLogger(__name__)

# Linhas por comando INSERT; lotes maiores viram vários comandos.
TAMANHO_LOTE = 500


@dataclass(frozen=True)
class ResumoImportacao:
    total_recebidos: int = 0
    criados: int = 0
    atualizados: int = 0
    # Sem este campo, `criados + atualizados` não fecharia com `total_recebidos`
    # quando há CNPJ repetido, e a resposta pareceria ter erro de contagem.
    duplicados_no_payload: int = 0
    cnpjs: list[str] = field(default_factory=list)


def importar_seguradoras(itens: list[dict]) -> ResumoImportacao:
    """Insere ou atualiza seguradoras em lote, usando o CNPJ como chave.

    Espera itens já validados pelo serializer. Custa 1 SELECT + 1 INSERT por
    lote de até `TAMANHO_LOTE`: o número de queries não cresce com o payload.
    """
    if not itens:
        return ResumoImportacao()

    # Deduplica mantendo a última ocorrência. Obrigatório: o Postgres recusa um
    # ON CONFLICT DO UPDATE que afete a mesma linha duas vezes no mesmo comando.
    por_cnpj = {item["cnpj"]: item for item in itens}
    cnpjs = list(por_cnpj)

    with transaction.atomic():
        # Só para relatar criados vs. atualizados; o upsert não depende disto.
        existentes = set(
            Seguradora.objects.filter(cnpj__in=cnpjs).values_list("cnpj", flat=True)
        )

        # INSERT ... ON CONFLICT (cnpj) DO UPDATE: resolver o conflito no banco
        # elimina a corrida entre importações simultâneas do mesmo CNPJ.
        # `status_enriquecimento` e `criado_em` ficam fora de `update_fields`
        # de propósito — o enriquecimento depende só do CNPJ, que não mudou.
        Seguradora.objects.bulk_create(
            [
                Seguradora(cnpj=cnpj, nome=item["nome"], uf=item["uf"])
                for cnpj, item in por_cnpj.items()
            ],
            update_conflicts=True,
            unique_fields=["cnpj"],
            update_fields=["nome", "uf", "atualizado_em"],
            batch_size=TAMANHO_LOTE,
        )

    return ResumoImportacao(
        total_recebidos=len(itens),
        criados=len(por_cnpj) - len(existentes),
        atualizados=len(existentes),
        duplicados_no_payload=len(itens) - len(por_cnpj),
        cnpjs=cnpjs,
    )


# --- Enriquecimento via BrasilAPI ------------------------------------------
# `enriquecer_pendentes` é o único ponto que faz o trabalho: o command e a
# thread chamam ela. O agendamento só decide quando e onde ela roda.


@dataclass(frozen=True)
class ResumoEnriquecimento:
    processados: int = 0
    concluidos: int = 0
    nao_encontrados: int = 0
    erros: int = 0


def enriquecer_seguradora(seguradora: Seguradora, client: BrasilAPIClient) -> str:
    """Enriquece um registro. Nenhuma falha da BrasilAPI escapa daqui.

    Erro da API externa vira log e status, com os dados básicos preservados —
    um CNPJ problemático não derruba o lote. Falha de banco no `save` não é
    tratada aqui: é a transação que quebrou, não a integração.
    """
    try:
        dados = client.consultar_cnpj(seguradora.cnpj)
    except ErroPermanenteCNPJ as exc:
        # 404 e 400 compartilham o status: para o reprocessamento significam a
        # mesma coisa, não adianta tentar de novo.
        logger.warning(
            "Enriquecimento definitivamente indisponível para o CNPJ %s (%s); "
            "registro mantido com os dados básicos.",
            seguradora.cnpj,
            exc,
        )
        seguradora.status_enriquecimento = StatusEnriquecimento.NAO_ENCONTRADO
    except BrasilAPIError as exc:
        logger.error(
            "Falha ao enriquecer o CNPJ %s (%s); registro mantido com os dados básicos.",
            seguradora.cnpj,
            exc,
        )
        seguradora.status_enriquecimento = StatusEnriquecimento.ERRO
    else:
        seguradora.nome_fantasia = dados.nome_fantasia
        seguradora.situacao_cadastral = dados.situacao_cadastral
        seguradora.status_enriquecimento = StatusEnriquecimento.CONCLUIDO

    # `atualizado_em` precisa estar na lista: com update_fields, o auto_now só
    # dispara para os campos citados.
    seguradora.save(
        update_fields=[
            "nome_fantasia",
            "situacao_cadastral",
            "status_enriquecimento",
            "atualizado_em",
        ]
    )
    return seguradora.status_enriquecimento


def enriquecer_pendentes(cnpjs=None, limite=None, forcar=False) -> ResumoEnriquecimento:
    """Enriquece os registros que ainda valem consulta.

    Sequencial e com um único cliente HTTP: a BrasilAPI é pública e tem limite
    de requisições, então paralelizar renderia 429 em vez de velocidade.
    """
    consulta = Seguradora.objects.all()

    if cnpjs is not None:
        consulta = consulta.filter(cnpj__in=list(cnpjs))
    if not forcar:
        consulta = consulta.filter(
            status_enriquecimento__in=StatusEnriquecimento.reprocessaveis()
        )

    consulta = consulta.order_by("id")
    # `is not None` e não `if limite:` — com `--limite 0` o falsy pularia o
    # corte e processaria a tabela inteira.
    if limite is not None:
        consulta = consulta[:limite]

    contagem = Counter()
    with BrasilAPIClient() as client:
        for seguradora in consulta:
            contagem[enriquecer_seguradora(seguradora, client)] += 1

    # Invalidar só nos sucessos deixaria ERRO e NAO_ENCONTRADO invisíveis na
    # listagem, que pode ser filtrada por status, até o TTL expirar.
    if contagem:
        cache_listagem.invalidar_listagem()

    return ResumoEnriquecimento(
        processados=sum(contagem.values()),
        concluidos=contagem[StatusEnriquecimento.CONCLUIDO],
        nao_encontrados=contagem[StatusEnriquecimento.NAO_ENCONTRADO],
        erros=contagem[StatusEnriquecimento.ERRO],
    )


def agendar_enriquecimento(cnpjs) -> int:
    """Encaminha o enriquecimento para fora do ciclo request/response.

    Devolve quantos CNPJs foram efetivamente encaminhados, para a resposta da
    importação não afirmar que agendou algo quando a flag está desligada.
    """
    if not cnpjs or not settings.ENRIQUECIMENTO_AO_IMPORTAR:
        return 0

    lista = list(cnpjs)

    if not settings.ENRIQUECIMENTO_ASSINCRONO:
        enriquecer_pendentes(cnpjs=lista)
        return len(lista)

    # `on_commit` e não `start()` direto: a thread abre a própria conexão e não
    # enxergaria registros de uma transação ainda não commitada.
    transaction.on_commit(lambda: _disparar_thread(lista))
    return len(lista)


def _disparar_thread(cnpjs: list[str]) -> None:
    """Sobe a thread de background. Isolado para os testes poderem interceptar."""
    threading.Thread(
        target=_processar_em_background,
        args=(cnpjs,),
        daemon=True,
        name="enriquecimento",
    ).start()


def _processar_em_background(cnpjs: list[str]) -> None:
    """Corpo da thread.

    Threads fora do ciclo de request não recebem o cleanup automático de
    conexões do Django: sem fechar na mão, cada lote deixa uma conexão do
    Postgres pendurada.
    """
    close_old_connections()
    try:
        enriquecer_pendentes(cnpjs=cnpjs)
    except Exception:
        logger.exception("Enriquecimento em background falhou para %d CNPJ(s).", len(cnpjs))
    finally:
        connection.close()
