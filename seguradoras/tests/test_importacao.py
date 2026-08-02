"""Validação de CNPJ, modelo, serializers, regra de upsert e o endpoint de
importação."""

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from seguradoras.models import Seguradora, StatusEnriquecimento
from seguradoras.serializers import (
    ResumoImportacaoSerializer,
    SeguradoraImportItemSerializer,
)
from seguradoras.services import importar_seguradoras
from seguradoras.tests.helpers import (
    CNPJ_BANCO_DO_BRASIL,
    CNPJ_BRADESCO,
    CNPJ_FICTICIO_A,
    CNPJ_FICTICIO_B,
    CNPJ_FICTICIO_C,
    CNPJ_ITAU,
    CNPJ_PETROBRAS,
    gerar_cnpj,
    item,
)
from seguradoras.validators import formatar_cnpj, normalizar_cnpj, validar_cnpj


class ValidadorCNPJTests(SimpleTestCase):
    """Não toca no banco: a validação é lógica pura, por design."""

    def test_aceita_cnpjs_reais(self):
        for cnpj in (CNPJ_BANCO_DO_BRASIL, CNPJ_PETROBRAS, CNPJ_BRADESCO, CNPJ_ITAU):
            with self.subTest(cnpj=cnpj):
                self.assertTrue(validar_cnpj(cnpj))

    def test_aceita_cnpjs_ficticios_bem_formados(self):
        for cnpj in (CNPJ_FICTICIO_A, CNPJ_FICTICIO_B, CNPJ_FICTICIO_C):
            with self.subTest(cnpj=cnpj):
                self.assertTrue(validar_cnpj(cnpj))

    def test_rejeita_digito_verificador_errado(self):
        # Mesma base, último dígito trocado.
        for cnpj in ("00000000000192", "33000167000100", "11222333000182"):
            with self.subTest(cnpj=cnpj):
                self.assertFalse(validar_cnpj(cnpj))

    def test_rejeita_sequencia_repetida(self):
        # Passam no módulo 11, mas não são CNPJs reais.
        for digito in "0123456789":
            cnpj = digito * 14
            with self.subTest(cnpj=cnpj):
                self.assertFalse(validar_cnpj(cnpj))

    def test_rejeita_tamanho_invalido(self):
        for cnpj in ("", "123", "0000000000019", "000000000001911"):
            with self.subTest(cnpj=cnpj):
                self.assertFalse(validar_cnpj(cnpj))

    def test_rejeita_entrada_nao_normalizada(self):
        # validar_cnpj espera a entrada já normalizada.
        self.assertFalse(validar_cnpj("00.000.000/0001-91"))

    def test_normaliza_mascara_e_espacos(self):
        for entrada in (
            "00.000.000/0001-91",
            "  00000000000191  ",
            "00 000 000 0001 91",
            "00-000-000-0001-91",
        ):
            with self.subTest(entrada=entrada):
                self.assertEqual(normalizar_cnpj(entrada), CNPJ_BANCO_DO_BRASIL)

    def test_normalizacao_rejeita_caractere_estranho(self):
        # Letras não são "ignoradas como máscara": invalidam a entrada.
        for entrada in ("00.000.000/0001-9X", "abc", "0000000000019$"):
            with self.subTest(entrada=entrada):
                with self.assertRaises(ValueError):
                    normalizar_cnpj(entrada)

    def test_normalizacao_rejeita_vazio_e_nulo(self):
        for entrada in ("", "   ", None):
            with self.subTest(entrada=entrada):
                with self.assertRaises(ValueError):
                    normalizar_cnpj(entrada)

    def test_normalizacao_rejeita_quantidade_errada_de_digitos(self):
        with self.assertRaisesMessage(ValueError, "14 dígitos"):
            normalizar_cnpj("123")

    def test_formatacao_aplica_mascara(self):
        self.assertEqual(formatar_cnpj(CNPJ_BANCO_DO_BRASIL), "00.000.000/0001-91")
        self.assertEqual(formatar_cnpj(CNPJ_PETROBRAS), "33.000.167/0001-01")

    def test_formatacao_nao_quebra_com_valor_inesperado(self):
        # Nunca deve levantar durante a serialização de um registro.
        for entrada in ("", "123", None):
            with self.subTest(entrada=entrada):
                self.assertEqual(formatar_cnpj(entrada), entrada)


class SeguradoraModelTests(TestCase):
    def test_status_inicial_e_pendente(self):
        seguradora = Seguradora.objects.create(
            cnpj=CNPJ_FICTICIO_A, nome="Seguradora A", uf="PR"
        )
        self.assertEqual(seguradora.status_enriquecimento, StatusEnriquecimento.PENDENTE)
        self.assertEqual(seguradora.nome_fantasia, "")
        self.assertEqual(seguradora.situacao_cadastral, "")

    def test_cnpj_e_unico(self):
        Seguradora.objects.create(cnpj=CNPJ_FICTICIO_A, nome="Seguradora A", uf="PR")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Seguradora.objects.create(cnpj=CNPJ_FICTICIO_A, nome="Outra", uf="SP")

    def test_ordenacao_padrao_por_nome(self):
        Seguradora.objects.create(cnpj=CNPJ_FICTICIO_C, nome="Zurich", uf="SP")
        Seguradora.objects.create(cnpj=CNPJ_FICTICIO_A, nome="Allianz", uf="PR")
        Seguradora.objects.create(cnpj=CNPJ_FICTICIO_B, nome="Mapfre", uf="RJ")

        nomes = list(Seguradora.objects.values_list("nome", flat=True))
        self.assertEqual(nomes, ["Allianz", "Mapfre", "Zurich"])

    def test_str_mostra_nome_e_cnpj_formatado(self):
        seguradora = Seguradora(cnpj=CNPJ_BANCO_DO_BRASIL, nome="BB Seguros", uf="DF")
        self.assertEqual(str(seguradora), "BB Seguros (00.000.000/0001-91)")

    def test_reprocessaveis_cobre_pendente_e_erro(self):
        self.assertEqual(
            set(StatusEnriquecimento.reprocessaveis()),
            {StatusEnriquecimento.PENDENTE, StatusEnriquecimento.ERRO},
        )


class SeguradoraImportItemSerializerTests(SimpleTestCase):
    """O serializer é o único portão de entrada: normaliza e rejeita."""

    def payload(self, **sobrescritas) -> dict:
        base = {"nome": "Seguradora Exemplo", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"}
        base.update(sobrescritas)
        return base

    def test_normaliza_cnpj_com_mascara(self):
        serializer = SeguradoraImportItemSerializer(
            data=self.payload(cnpj="11.222.333/0001-81")
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["cnpj"], CNPJ_FICTICIO_A)

    def test_normaliza_uf_para_maiusculas(self):
        serializer = SeguradoraImportItemSerializer(data=self.payload(uf="pr"))
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["uf"], "PR")

    def test_rejeita_cnpj_com_digito_verificador_errado(self):
        serializer = SeguradoraImportItemSerializer(data=self.payload(cnpj="11222333000182"))
        self.assertFalse(serializer.is_valid())
        self.assertIn("cnpj", serializer.errors)

    def test_rejeita_cnpj_malformado(self):
        for cnpj in ("", "123", "abc", "11.222.333/0001-8X"):
            with self.subTest(cnpj=cnpj):
                serializer = SeguradoraImportItemSerializer(data=self.payload(cnpj=cnpj))
                self.assertFalse(serializer.is_valid())
                self.assertIn("cnpj", serializer.errors)

    def test_rejeita_uf_inexistente(self):
        for uf in ("XX", "ZZ", ""):
            with self.subTest(uf=uf):
                serializer = SeguradoraImportItemSerializer(data=self.payload(uf=uf))
                self.assertFalse(serializer.is_valid())
                self.assertIn("uf", serializer.errors)

    def test_rejeita_uf_por_extenso(self):
        # "Paraná" excede max_length=2 e é barrado antes do validate_uf.
        serializer = SeguradoraImportItemSerializer(data=self.payload(uf="Paraná"))
        self.assertFalse(serializer.is_valid())
        self.assertIn("uf", serializer.errors)

    def test_rejeita_nome_vazio(self):
        serializer = SeguradoraImportItemSerializer(data=self.payload(nome=""))
        self.assertFalse(serializer.is_valid())
        self.assertIn("nome", serializer.errors)

    def test_erros_de_lista_vem_indexados_por_posicao(self):
        dados = [
            self.payload(),
            self.payload(cnpj="00000000000000"),
            self.payload(uf="XX"),
        ]
        serializer = SeguradoraImportItemSerializer(data=dados, many=True)

        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors[0], {})
        self.assertIn("cnpj", serializer.errors[1])
        self.assertIn("uf", serializer.errors[2])


class ImportacaoServicoTests(TestCase):
    def test_cria_registros_novos(self):
        resumo = importar_seguradoras([item(1), item(2), item(3)])

        self.assertEqual(Seguradora.objects.count(), 3)
        self.assertEqual(resumo.total_recebidos, 3)
        self.assertEqual(resumo.criados, 3)
        self.assertEqual(resumo.atualizados, 0)
        self.assertEqual(resumo.duplicados_no_payload, 0)
        self.assertEqual(set(resumo.cnpjs), {gerar_cnpj(1), gerar_cnpj(2), gerar_cnpj(3)})

    def test_reimportar_atualiza_em_vez_de_duplicar(self):
        importar_seguradoras([item(1, nome="Nome Antigo", uf="PR")])
        resumo = importar_seguradoras([item(1, nome="Nome Novo", uf="SP")])

        self.assertEqual(Seguradora.objects.count(), 1)
        self.assertEqual(resumo.criados, 0)
        self.assertEqual(resumo.atualizados, 1)

        seguradora = Seguradora.objects.get(cnpj=gerar_cnpj(1))
        self.assertEqual(seguradora.nome, "Nome Novo")
        self.assertEqual(seguradora.uf, "SP")

    def test_lote_misto_conta_criados_e_atualizados(self):
        importar_seguradoras([item(1), item(2)])
        resumo = importar_seguradoras([item(2), item(3), item(4)])

        self.assertEqual(Seguradora.objects.count(), 4)
        self.assertEqual(resumo.criados, 2)
        self.assertEqual(resumo.atualizados, 1)

    def test_update_preserva_o_status_do_enriquecimento(self):
        # O enriquecimento depende só do CNPJ, que não muda num update:
        # reprocessar seria consulta desperdiçada à API externa.
        importar_seguradoras([item(1)])
        Seguradora.objects.filter(cnpj=gerar_cnpj(1)).update(
            status_enriquecimento=StatusEnriquecimento.CONCLUIDO,
            nome_fantasia="Fantasia Preservada",
            situacao_cadastral="ATIVA",
        )

        importar_seguradoras([item(1, nome="Nome Novo")])

        seguradora = Seguradora.objects.get(cnpj=gerar_cnpj(1))
        self.assertEqual(seguradora.nome, "Nome Novo")
        self.assertEqual(seguradora.status_enriquecimento, StatusEnriquecimento.CONCLUIDO)
        self.assertEqual(seguradora.nome_fantasia, "Fantasia Preservada")
        self.assertEqual(seguradora.situacao_cadastral, "ATIVA")

    def test_update_preserva_criado_em_e_avanca_atualizado_em(self):
        importar_seguradoras([item(1)])
        antes = Seguradora.objects.get(cnpj=gerar_cnpj(1))

        importar_seguradoras([item(1, nome="Nome Novo")])
        depois = Seguradora.objects.get(cnpj=gerar_cnpj(1))

        self.assertEqual(depois.criado_em, antes.criado_em)
        self.assertGreater(depois.atualizado_em, antes.atualizado_em)

    def test_cnpj_repetido_no_payload_vence_a_ultima_ocorrencia(self):
        # Sem a deduplicação, o Postgres recusaria o comando inteiro com
        # "ON CONFLICT DO UPDATE command cannot affect row a second time".
        resumo = importar_seguradoras(
            [item(1, nome="Primeira"), item(1, nome="Segunda")]
        )

        self.assertEqual(Seguradora.objects.count(), 1)
        self.assertEqual(Seguradora.objects.get().nome, "Segunda")
        self.assertEqual(resumo.total_recebidos, 2)
        self.assertEqual(resumo.criados, 1)
        self.assertEqual(resumo.duplicados_no_payload, 1)

    def test_lista_vazia_nao_faz_nada(self):
        with CaptureQueriesContext(connection) as queries:
            resumo = importar_seguradoras([])

        self.assertEqual(len(queries), 0)
        self.assertEqual(resumo.total_recebidos, 0)
        self.assertEqual(resumo.cnpjs, [])

    def test_custo_em_queries_nao_cresce_com_o_tamanho_do_lote(self):
        """O critério de avaliação fala em 'evitar consultas excessivas'.

        Comparar dois lotes de tamanhos muito diferentes é mais robusto que
        fixar um número absoluto, que variaria conforme o Django emita ou não
        SAVEPOINTs em volta da transação.
        """
        with CaptureQueriesContext(connection) as lote_pequeno:
            importar_seguradoras([item(i) for i in range(1, 4)])

        Seguradora.objects.all().delete()

        with CaptureQueriesContext(connection) as lote_grande:
            importar_seguradoras([item(i) for i in range(1, 101)])

        self.assertEqual(len(lote_pequeno), len(lote_grande))

    def test_lote_usa_um_unico_insert(self):
        with CaptureQueriesContext(connection) as queries:
            importar_seguradoras([item(i) for i in range(1, 101)])

        sqls = [consulta["sql"] for consulta in queries.captured_queries]
        inserts = [sql for sql in sqls if "INSERT INTO" in sql.upper()]
        selects = [sql for sql in sqls if sql.strip().upper().startswith("SELECT")]

        self.assertEqual(len(inserts), 1, sqls)
        self.assertEqual(len(selects), 1, sqls)
        self.assertIn("ON CONFLICT", inserts[0].upper())


class ImportacaoAPITests(APITestCase):
    url = "/api/v1/seguradoras/importar/"

    def setUp(self):
        # O LocMemCache sobrevive entre testes do mesmo processo.
        cache.clear()

    def test_importa_e_responde_201(self):
        payload = [
            {"nome": "Seguradora Alfa", "cnpj": "11.222.333/0001-81", "uf": "PR"},
            {"nome": "Seguradora Beta", "cnpj": CNPJ_FICTICIO_B, "uf": "sp"},
        ]

        resposta = self.client.post(self.url, payload, format="json")

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(
            resposta.json(),
            {
                "total_recebidos": 2,
                "criados": 2,
                "atualizados": 0,
                "duplicados_no_payload": 0,
                # Zero porque a suíte desliga o enriquecimento automático: o
                # campo reflete o que foi de fato agendado, não o tamanho do
                # lote. Com a flag ligada, ver `AgendamentoTests`.
                "enriquecimento_agendado": 0,
            },
        )
        self.assertEqual(Seguradora.objects.count(), 2)

    def test_grava_cnpj_sem_mascara_e_uf_em_maiusculas(self):
        payload = [{"nome": "Alfa", "cnpj": "11.222.333/0001-81", "uf": "pr"}]

        self.client.post(self.url, payload, format="json")

        seguradora = Seguradora.objects.get()
        self.assertEqual(seguradora.cnpj, CNPJ_FICTICIO_A)
        self.assertEqual(seguradora.uf, "PR")

    def test_reimportacao_atualiza_pelo_endpoint(self):
        self.client.post(
            self.url, [{"nome": "Antigo", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"}], format="json"
        )
        resposta = self.client.post(
            self.url, [{"nome": "Novo", "cnpj": CNPJ_FICTICIO_A, "uf": "SP"}], format="json"
        )

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.json()["atualizados"], 1)
        self.assertEqual(resposta.json()["criados"], 0)
        self.assertEqual(Seguradora.objects.count(), 1)

    def test_item_invalido_rejeita_o_lote_inteiro(self):
        payload = [
            {"nome": "Válida", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"},
            {"nome": "Inválida", "cnpj": "11222333000182", "uf": "PR"},
        ]

        resposta = self.client.post(self.url, payload, format="json")

        self.assertEqual(resposta.status_code, 400)
        # Nada é gravado: a validação é tudo-ou-nada.
        self.assertEqual(Seguradora.objects.count(), 0)

    def test_erros_vem_indexados_pela_posicao_no_payload(self):
        payload = [
            {"nome": "Válida", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"},
            {"nome": "UF ruim", "cnpj": CNPJ_FICTICIO_B, "uf": "XX"},
        ]

        resposta = self.client.post(self.url, payload, format="json")
        erros = resposta.json()

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(erros[0], {})
        self.assertIn("uf", erros[1])

    def test_lista_vazia_e_rejeitada(self):
        resposta = self.client.post(self.url, [], format="json")
        self.assertEqual(resposta.status_code, 400)

    def test_payload_que_nao_e_lista_e_rejeitado(self):
        resposta = self.client.post(
            self.url, {"nome": "Alfa", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"}, format="json"
        )
        self.assertEqual(resposta.status_code, 400)

    def test_campos_faltando_sao_rejeitados(self):
        resposta = self.client.post(self.url, [{"nome": "Sem CNPJ"}], format="json")

        self.assertEqual(resposta.status_code, 400)
        self.assertIn("cnpj", resposta.json()[0])
        self.assertIn("uf", resposta.json()[0])

    def test_resposta_bate_com_o_schema_documentado(self):
        # Protege contra a resposta e o ResumoImportacaoSerializer divergirem.
        resposta = self.client.post(
            self.url, [{"nome": "Alfa", "cnpj": CNPJ_FICTICIO_A, "uf": "PR"}], format="json"
        )

        self.assertEqual(
            set(resposta.json()),
            set(ResumoImportacaoSerializer().fields),
        )
