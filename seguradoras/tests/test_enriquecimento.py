"""Testes do enriquecimento via BrasilAPI.

Toda comunicação HTTP é interceptada pelo `responses`: a suíte inteira roda
sem internet, como o enunciado exige.
"""

from io import StringIO
from unittest import mock

import requests
import responses
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APITestCase

from seguradoras.brasilapi import (
    USER_AGENT,
    BrasilAPIClient,
    BrasilAPIError,
    CNPJNaoEncontradoError,
    CNPJRecusadoError,
)
from seguradoras.models import Seguradora, StatusEnriquecimento
from seguradoras.services import enriquecer_pendentes
from seguradoras.tests.helpers import gerar_cnpj

BASE_URL = "https://brasilapi.exemplo/api/cnpj/v1"
CNPJ = gerar_cnpj(1)
URL_CNPJ = f"{BASE_URL}/{CNPJ}"

RESPOSTA_COMPLETA = {
    "cnpj": CNPJ,
    "razao_social": "SEGURADORA EXEMPLO S.A.",
    "nome_fantasia": "Exemplo Seguros",
    "descricao_situacao_cadastral": "ATIVA",
    "situacao_cadastral": 2,
}


@override_settings(BRASILAPI_BASE_URL=BASE_URL, BRASILAPI_MAX_RETRIES=2)
class BrasilAPIClientTests(SimpleTestCase):
    """O cliente não toca no banco: é HTTP puro, testável isoladamente."""

    @responses.activate
    def test_mapeia_os_campos_usados_no_enriquecimento(self):
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        dados = BrasilAPIClient().consultar_cnpj(CNPJ)

        self.assertEqual(dados.nome_fantasia, "Exemplo Seguros")
        self.assertEqual(dados.situacao_cadastral, "ATIVA")

    @responses.activate
    def test_usa_situacao_numerica_quando_falta_a_descricao(self):
        responses.add(
            responses.GET,
            URL_CNPJ,
            json={"nome_fantasia": "Exemplo", "situacao_cadastral": 2},
            status=200,
        )

        self.assertEqual(BrasilAPIClient().consultar_cnpj(CNPJ).situacao_cadastral, "2")

    @responses.activate
    def test_campos_ausentes_viram_string_vazia(self):
        responses.add(responses.GET, URL_CNPJ, json={"cnpj": CNPJ}, status=200)

        dados = BrasilAPIClient().consultar_cnpj(CNPJ)

        self.assertEqual(dados.nome_fantasia, "")
        self.assertEqual(dados.situacao_cadastral, "")

    @responses.activate
    def test_trunca_valores_maiores_que_a_coluna(self):
        responses.add(
            responses.GET,
            URL_CNPJ,
            json={"nome_fantasia": "x" * 400, "descricao_situacao_cadastral": "y" * 300},
            status=200,
        )

        dados = BrasilAPIClient().consultar_cnpj(CNPJ)

        self.assertEqual(len(dados.nome_fantasia), 255)
        self.assertEqual(len(dados.situacao_cadastral), 100)

    @responses.activate
    def test_404_vira_cnpj_nao_encontrado(self):
        responses.add(responses.GET, URL_CNPJ, json={"message": "não encontrado"}, status=404)

        with self.assertRaises(CNPJNaoEncontradoError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_400_vira_cnpj_recusado_e_nao_retenta(self):
        # Comportamento confirmado contra a BrasilAPI real: DV inválido
        # devolve 400 {"type":"bad_request"}, não 404.
        responses.add(responses.GET, URL_CNPJ, json={"type": "bad_request"}, status=400)

        with self.assertRaises(CNPJRecusadoError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_cnpj_invalido_nao_chega_a_sair_da_aplicacao(self):
        # O CNPJ é interpolado na URL; conferir antes evita alterar o path da
        # requisição e poupa uma chamada fadada a 400.
        with self.assertRaises(CNPJRecusadoError):
            BrasilAPIClient().consultar_cnpj("../../v1/foo")

        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_erro_do_servidor_vira_brasilapi_error(self):
        for _ in range(5):
            responses.add(responses.GET, URL_CNPJ, status=500)

        with self.assertRaises(BrasilAPIError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_retenta_e_se_recupera_de_um_5xx(self):
        responses.add(responses.GET, URL_CNPJ, status=503)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        dados = BrasilAPIClient().consultar_cnpj(CNPJ)

        self.assertEqual(dados.nome_fantasia, "Exemplo Seguros")
        self.assertEqual(len(responses.calls), 2)  # provou que houve retry

    @responses.activate
    def test_timeout_vira_brasilapi_error(self):
        responses.add(responses.GET, URL_CNPJ, body=requests.Timeout("estourou"))

        with self.assertRaises(BrasilAPIError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_falha_de_conexao_vira_brasilapi_error(self):
        responses.add(responses.GET, URL_CNPJ, body=requests.ConnectionError("sem rota"))

        with self.assertRaises(BrasilAPIError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_corpo_que_nao_e_json_vira_brasilapi_error(self):
        responses.add(responses.GET, URL_CNPJ, body="<html>manutenção</html>", status=200)

        with self.assertRaises(BrasilAPIError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_json_que_nao_e_objeto_vira_brasilapi_error(self):
        responses.add(responses.GET, URL_CNPJ, json=["inesperado"], status=200)

        with self.assertRaises(BrasilAPIError):
            BrasilAPIClient().consultar_cnpj(CNPJ)

    @responses.activate
    def test_requisicao_identifica_o_cliente(self):
        # Não é só etiqueta: a BrasilAPI devolve 429 para o User-Agent padrão
        # do `requests`, e responde normalmente com um agente próprio.
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        BrasilAPIClient().consultar_cnpj(CNPJ)

        enviado = responses.calls[0].request.headers["User-Agent"]
        self.assertEqual(enviado, USER_AGENT)
        self.assertNotIn("python-requests", enviado)

    @responses.activate
    def test_toda_requisicao_leva_timeout(self):
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        cliente = BrasilAPIClient()
        with mock.patch.object(
            cliente.session, "get", wraps=cliente.session.get
        ) as chamada:
            cliente.consultar_cnpj(CNPJ)

        self.assertIn("timeout", chamada.call_args.kwargs)
        self.assertIsNotNone(chamada.call_args.kwargs["timeout"])


@override_settings(BRASILAPI_BASE_URL=BASE_URL, BRASILAPI_MAX_RETRIES=0)
class EnriquecimentoServicoTests(TestCase):
    def setUp(self):
        cache.clear()

    def criar(self, indice=1, **campos):
        return Seguradora.objects.create(
            cnpj=gerar_cnpj(indice),
            nome=campos.pop("nome", "Seguradora Exemplo"),
            uf=campos.pop("uf", "PR"),
            **campos,
        )

    @responses.activate
    def test_sucesso_preenche_campos_e_marca_concluido(self):
        self.criar()
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        resumo = enriquecer_pendentes()

        seguradora = Seguradora.objects.get()
        self.assertEqual(seguradora.nome_fantasia, "Exemplo Seguros")
        self.assertEqual(seguradora.situacao_cadastral, "ATIVA")
        self.assertEqual(seguradora.status_enriquecimento, StatusEnriquecimento.CONCLUIDO)
        self.assertEqual(resumo.concluidos, 1)

    @responses.activate
    def test_400_tambem_marca_nao_encontrado(self):
        # Não é retentável: o command não deve gastar uma requisição por
        # execução num CNPJ que a API rejeita.
        self.criar()
        responses.add(responses.GET, URL_CNPJ, status=400)

        resumo = enriquecer_pendentes()

        self.assertEqual(
            Seguradora.objects.get().status_enriquecimento,
            StatusEnriquecimento.NAO_ENCONTRADO,
        )
        self.assertEqual(resumo.nao_encontrados, 1)

    @responses.activate
    def test_404_preserva_os_dados_basicos(self):
        self.criar(nome="Nome Original", uf="SP")
        responses.add(responses.GET, URL_CNPJ, status=404)

        resumo = enriquecer_pendentes()

        seguradora = Seguradora.objects.get()
        self.assertEqual(seguradora.status_enriquecimento, StatusEnriquecimento.NAO_ENCONTRADO)
        self.assertEqual(seguradora.nome, "Nome Original")
        self.assertEqual(seguradora.uf, "SP")
        self.assertEqual(seguradora.cnpj, CNPJ)
        self.assertEqual(resumo.nao_encontrados, 1)

    @responses.activate
    def test_falha_da_api_preserva_os_dados_basicos(self):
        self.criar(nome="Nome Original")
        responses.add(responses.GET, URL_CNPJ, status=500)

        resumo = enriquecer_pendentes()

        seguradora = Seguradora.objects.get()
        self.assertEqual(seguradora.status_enriquecimento, StatusEnriquecimento.ERRO)
        self.assertEqual(seguradora.nome, "Nome Original")
        self.assertEqual(resumo.erros, 1)

    @responses.activate
    def test_erro_e_registrado_no_log(self):
        # O enunciado pede explicitamente "logar o erro".
        self.criar()
        responses.add(responses.GET, URL_CNPJ, status=500)

        with self.assertLogs("seguradoras.services", level="ERROR") as registro:
            enriquecer_pendentes()

        self.assertIn(CNPJ, registro.output[0])

    @responses.activate
    def test_cnpj_nao_encontrado_e_registrado_no_log(self):
        self.criar()
        responses.add(responses.GET, URL_CNPJ, status=404)

        with self.assertLogs("seguradoras.services", level="WARNING") as registro:
            enriquecer_pendentes()

        self.assertIn(CNPJ, registro.output[0])

    @responses.activate
    def test_uma_falha_nao_interrompe_o_lote(self):
        self.criar(1)
        self.criar(2)
        self.criar(3)
        responses.add(responses.GET, f"{BASE_URL}/{gerar_cnpj(1)}", status=500)
        responses.add(responses.GET, f"{BASE_URL}/{gerar_cnpj(2)}", json=RESPOSTA_COMPLETA)
        responses.add(responses.GET, f"{BASE_URL}/{gerar_cnpj(3)}", status=404)

        resumo = enriquecer_pendentes()

        self.assertEqual(resumo.processados, 3)
        self.assertEqual(resumo.concluidos, 1)
        self.assertEqual(resumo.erros, 1)
        self.assertEqual(resumo.nao_encontrados, 1)

    @responses.activate
    def test_ignora_registros_ja_concluidos(self):
        self.criar(status_enriquecimento=StatusEnriquecimento.CONCLUIDO)

        resumo = enriquecer_pendentes()

        self.assertEqual(resumo.processados, 0)
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_reprocessa_registros_com_erro(self):
        self.criar(status_enriquecimento=StatusEnriquecimento.ERRO)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        self.assertEqual(enriquecer_pendentes().concluidos, 1)

    @responses.activate
    def test_forcar_reprocessa_ate_os_concluidos(self):
        self.criar(status_enriquecimento=StatusEnriquecimento.CONCLUIDO)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        self.assertEqual(enriquecer_pendentes(forcar=True).concluidos, 1)

    @responses.activate
    def test_filtra_pelos_cnpjs_informados(self):
        self.criar(1)
        self.criar(2)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        resumo = enriquecer_pendentes(cnpjs=[gerar_cnpj(1)])

        self.assertEqual(resumo.processados, 1)
        self.assertEqual(
            Seguradora.objects.get(cnpj=gerar_cnpj(2)).status_enriquecimento,
            StatusEnriquecimento.PENDENTE,
        )

    @responses.activate
    def test_respeita_o_limite(self):
        for indice in range(1, 4):
            self.criar(indice)
            responses.add(
                responses.GET, f"{BASE_URL}/{gerar_cnpj(indice)}", json=RESPOSTA_COMPLETA
            )

        self.assertEqual(enriquecer_pendentes(limite=2).processados, 2)

    @responses.activate
    def test_rodar_duas_vezes_e_idempotente(self):
        self.criar()
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        enriquecer_pendentes()
        segunda = enriquecer_pendentes()

        self.assertEqual(segunda.processados, 0)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_falha_tambem_invalida_o_cache_da_listagem(self):
        """Regressão: o cache só era invalidado quando algo chegava a CONCLUIDO.

        Um lote que falha inteiro muda o status no banco, e a listagem pode ser
        filtrada por ele — servir dado velho até o TTL expirar esconderia o
        resultado do processamento em background.
        """
        self.criar()
        responses.add(responses.GET, URL_CNPJ, status=500)

        antes = self.client.get("/api/v1/seguradoras/").json()["results"][0]
        self.assertEqual(antes["status_enriquecimento"], StatusEnriquecimento.PENDENTE)

        enriquecer_pendentes()

        depois = self.client.get("/api/v1/seguradoras/").json()["results"][0]
        self.assertEqual(depois["status_enriquecimento"], StatusEnriquecimento.ERRO)

    @responses.activate
    def test_nao_altera_os_dados_basicos_no_sucesso(self):
        self.criar(nome="Razão Social Informada", uf="RS")
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        enriquecer_pendentes()

        seguradora = Seguradora.objects.get()
        self.assertEqual(seguradora.nome, "Razão Social Informada")
        self.assertEqual(seguradora.uf, "RS")


@override_settings(BRASILAPI_BASE_URL=BASE_URL, BRASILAPI_MAX_RETRIES=0)
class AgendamentoTests(APITestCase):
    """Como o enriquecimento é disparado a partir do endpoint de importação."""

    url = "/api/v1/seguradoras/importar/"

    def payload(self):
        return [{"nome": "Seguradora Exemplo", "cnpj": CNPJ, "uf": "PR"}]

    @responses.activate
    @override_settings(ENRIQUECIMENTO_AO_IMPORTAR=True, ENRIQUECIMENTO_ASSINCRONO=False)
    def test_importacao_dispara_o_enriquecimento(self):
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        resposta = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.json()["enriquecimento_agendado"], 1)
        self.assertEqual(
            Seguradora.objects.get().status_enriquecimento,
            StatusEnriquecimento.CONCLUIDO,
        )

    @override_settings(ENRIQUECIMENTO_AO_IMPORTAR=True, ENRIQUECIMENTO_ASSINCRONO=True)
    def test_modo_assincrono_agenda_para_depois_do_commit(self):
        with mock.patch("seguradoras.services._disparar_thread") as disparo:
            with self.captureOnCommitCallbacks(execute=True):
                resposta = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(resposta.status_code, 201)
        disparo.assert_called_once_with([CNPJ])

    @override_settings(ENRIQUECIMENTO_AO_IMPORTAR=True, ENRIQUECIMENTO_ASSINCRONO=True)
    def test_nada_e_disparado_se_a_transacao_reverter(self):
        with mock.patch("seguradoras.services._disparar_thread") as disparo:
            with self.captureOnCommitCallbacks(execute=False):
                self.client.post(self.url, self.payload(), format="json")

        disparo.assert_not_called()

    @override_settings(ENRIQUECIMENTO_AO_IMPORTAR=False)
    def test_flag_desligada_nao_dispara_nada(self):
        with mock.patch("seguradoras.services.enriquecer_pendentes") as servico:
            resposta = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(resposta.status_code, 201)
        servico.assert_not_called()

    @responses.activate
    @override_settings(ENRIQUECIMENTO_AO_IMPORTAR=True, ENRIQUECIMENTO_ASSINCRONO=False)
    def test_falha_no_enriquecimento_nao_derruba_a_importacao(self):
        responses.add(responses.GET, URL_CNPJ, status=500)

        resposta = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(Seguradora.objects.count(), 1)


@override_settings(BRASILAPI_BASE_URL=BASE_URL, BRASILAPI_MAX_RETRIES=0)
class CommandTests(TestCase):
    def criar(self, indice, status=StatusEnriquecimento.PENDENTE):
        return Seguradora.objects.create(
            cnpj=gerar_cnpj(indice),
            nome=f"Seguradora {indice}",
            uf="PR",
            status_enriquecimento=status,
        )

    def executar(self, **opcoes) -> str:
        """Roda o comando capturando a saída, para não poluir a suíte."""
        saida = StringIO()
        call_command("enriquecer_seguradoras", stdout=saida, stderr=StringIO(), **opcoes)
        return saida.getvalue()

    @responses.activate
    def test_processa_pendentes(self):
        self.criar(1)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        saida = self.executar()

        self.assertEqual(
            Seguradora.objects.get().status_enriquecimento,
            StatusEnriquecimento.CONCLUIDO,
        )
        self.assertIn("Concluídas:      1", saida)

    @responses.activate
    def test_ignora_concluidos_e_nao_encontrados(self):
        self.criar(1, StatusEnriquecimento.CONCLUIDO)
        self.criar(2, StatusEnriquecimento.NAO_ENCONTRADO)

        self.executar()

        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_forcar_reprocessa_tudo(self):
        self.criar(1, StatusEnriquecimento.CONCLUIDO)
        responses.add(responses.GET, URL_CNPJ, json=RESPOSTA_COMPLETA, status=200)

        self.executar(forcar=True)

        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_limite(self):
        for indice in (1, 2, 3):
            self.criar(indice)
            responses.add(
                responses.GET, f"{BASE_URL}/{gerar_cnpj(indice)}", json=RESPOSTA_COMPLETA
            )

        self.executar(limite=2)

        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_relata_erros_separadamente(self):
        self.criar(1)
        responses.add(responses.GET, URL_CNPJ, status=500)

        saida = self.executar()

        self.assertIn("Com erro:", saida)
        self.assertIn("tentadas de novo", saida)

    def test_avisa_quando_nao_ha_nada_a_fazer(self):
        self.assertIn("Nenhuma seguradora", self.executar())
