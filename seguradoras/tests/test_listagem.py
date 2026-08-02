"""Testes de GET /api/v1/seguradoras/: paginação, filtros e cache."""

from django.core.cache import cache
from rest_framework.test import APITestCase

from seguradoras.models import Seguradora, StatusEnriquecimento
from seguradoras.tests.helpers import CNPJ_FICTICIO_A, gerar_cnpj

URL = "/api/v1/seguradoras/"
URL_IMPORTAR = "/api/v1/seguradoras/importar/"


class ListagemTestCase(APITestCase):
    def setUp(self):
        # O LocMemCache é do processo, não do teste: sem isto uma entrada de
        # um teste anterior responderia no lugar da consulta real.
        cache.clear()


class PaginacaoTests(ListagemTestCase):
    def setUp(self):
        super().setUp()
        Seguradora.objects.bulk_create(
            Seguradora(cnpj=gerar_cnpj(i), nome=f"Seguradora {i:03d}", uf="PR")
            for i in range(1, 26)
        )

    def test_pagina_com_tamanho_padrao(self):
        corpo = self.client.get(URL).json()

        self.assertEqual(corpo["count"], 25)
        self.assertEqual(len(corpo["results"]), 20)
        self.assertIsNotNone(corpo["next"])
        self.assertIsNone(corpo["previous"])

    def test_segunda_pagina(self):
        corpo = self.client.get(URL, {"page": 2}).json()

        self.assertEqual(len(corpo["results"]), 5)
        self.assertIsNone(corpo["next"])
        self.assertIsNotNone(corpo["previous"])

    def test_page_size_customizado(self):
        corpo = self.client.get(URL, {"page_size": 5}).json()
        self.assertEqual(len(corpo["results"]), 5)

    def test_page_size_respeita_o_teto(self):
        corpo = self.client.get(URL, {"page_size": 1000}).json()
        self.assertEqual(len(corpo["results"]), 25)  # limitado a max_page_size

    def test_pagina_inexistente_devolve_404(self):
        self.assertEqual(self.client.get(URL, {"page": 99}).status_code, 404)

    def test_ordenacao_e_estavel_entre_paginas(self):
        primeira = self.client.get(URL, {"page_size": 10, "page": 1}).json()["results"]
        segunda = self.client.get(URL, {"page_size": 10, "page": 2}).json()["results"]

        ids = [r["id"] for r in primeira] + [r["id"] for r in segunda]
        self.assertEqual(len(set(ids)), 20)  # nenhum registro repetido


class FiltrosTests(ListagemTestCase):
    def setUp(self):
        super().setUp()
        Seguradora.objects.create(cnpj=gerar_cnpj(1), nome="Porto Seguro", uf="SP")
        Seguradora.objects.create(cnpj=gerar_cnpj(2), nome="Porto Alegre Seguros", uf="RS")
        Seguradora.objects.create(cnpj=gerar_cnpj(3), nome="Bradesco Seguros", uf="SP")
        Seguradora.objects.create(
            cnpj=gerar_cnpj(4),
            nome="Allianz",
            uf="PR",
            status_enriquecimento=StatusEnriquecimento.CONCLUIDO,
        )

    def nomes(self, **parametros) -> list[str]:
        corpo = self.client.get(URL, parametros).json()
        return [resultado["nome"] for resultado in corpo["results"]]

    def test_filtra_por_uf(self):
        self.assertEqual(self.nomes(uf="SP"), ["Bradesco Seguros", "Porto Seguro"])

    def test_filtro_de_uf_ignora_caixa(self):
        self.assertEqual(self.nomes(uf="sp"), ["Bradesco Seguros", "Porto Seguro"])

    def test_filtra_por_trecho_do_nome(self):
        self.assertEqual(self.nomes(nome="Porto"), ["Porto Alegre Seguros", "Porto Seguro"])

    def test_filtro_de_nome_ignora_caixa(self):
        self.assertEqual(self.nomes(nome="porto"), ["Porto Alegre Seguros", "Porto Seguro"])

    def test_filtro_de_nome_casa_no_meio_da_string(self):
        self.assertEqual(self.nomes(nome="Seguros"), ["Bradesco Seguros", "Porto Alegre Seguros"])

    def test_filtros_combinados(self):
        self.assertEqual(self.nomes(nome="Seguro", uf="SP"), ["Bradesco Seguros", "Porto Seguro"])

    def test_filtra_por_status_do_enriquecimento(self):
        self.assertEqual(self.nomes(status_enriquecimento="CONCLUIDO"), ["Allianz"])

    def test_filtro_sem_resultado_devolve_lista_vazia(self):
        corpo = self.client.get(URL, {"uf": "AM"}).json()
        self.assertEqual(corpo["count"], 0)
        self.assertEqual(corpo["results"], [])

    def test_uf_invalida_nao_quebra(self):
        corpo = self.client.get(URL, {"uf": "XX"}).json()
        self.assertEqual(corpo["count"], 0)

    def test_saida_traz_cnpj_cru_e_formatado(self):
        resultado = self.client.get(URL, {"nome": "Allianz"}).json()["results"][0]

        self.assertEqual(resultado["cnpj"], gerar_cnpj(4))
        self.assertEqual(resultado["cnpj_formatado"], "00.000.004/0001-70")


class CacheListagemTests(ListagemTestCase):
    def setUp(self):
        super().setUp()
        Seguradora.objects.create(cnpj=gerar_cnpj(1), nome="Alfa", uf="PR")

    def test_segunda_chamada_identica_nao_consulta_o_banco(self):
        self.client.get(URL)

        with self.assertNumQueries(0):
            resposta = self.client.get(URL)

        self.assertEqual(resposta.json()["count"], 1)

    def test_querystrings_diferentes_nao_compartilham_entrada(self):
        self.client.get(URL, {"uf": "PR"})

        # Filtro diferente precisa ir ao banco em vez de reusar a entrada acima.
        # São 2 queries porque há resultado: o COUNT da paginação e o SELECT.
        with self.assertNumQueries(2):
            corpo = self.client.get(URL, {"nome": "Alfa"}).json()

        self.assertEqual(corpo["count"], 1)

    def test_ordem_dos_parametros_nao_gera_entrada_nova(self):
        self.client.get(f"{URL}?uf=PR&nome=Alfa")

        with self.assertNumQueries(0):
            self.client.get(f"{URL}?nome=Alfa&uf=PR")

    def test_valores_repetidos_do_mesmo_parametro_nao_colidem(self):
        """Regressão: a chave normalizava a ordem dos valores repetidos.

        O django-filter usa a última ocorrência, então `?uf=SP&uf=PR` filtra
        por PR e `?uf=PR&uf=SP` filtra por SP. Ordenar os valores fazia as duas
        compartilharem a entrada, e a segunda recebia o resultado da primeira.
        """
        Seguradora.objects.create(cnpj=gerar_cnpj(2), nome="Beta", uf="SP")

        primeira = self.client.get(f"{URL}?uf=SP&uf=PR").json()
        segunda = self.client.get(f"{URL}?uf=PR&uf=SP").json()

        self.assertEqual([r["nome"] for r in primeira["results"]], ["Alfa"])
        self.assertEqual([r["nome"] for r in segunda["results"]], ["Beta"])

    def test_importacao_invalida_o_cache(self):
        self.assertEqual(self.client.get(URL).json()["count"], 1)

        self.client.post(
            URL_IMPORTAR,
            [{"nome": "Beta", "cnpj": CNPJ_FICTICIO_A, "uf": "SP"}],
            format="json",
        )

        # A entrada anterior ficou obsoleta: a listagem enxerga o novo registro.
        self.assertEqual(self.client.get(URL).json()["count"], 2)

    def test_escrita_fora_da_api_nao_invalida(self):
        """Documenta uma limitação conhecida do cache.

        A invalidação é disparada pelos caminhos da aplicação (importação e
        enriquecimento). Uma escrita direta no banco não é percebida até a
        entrada expirar pelo TTL.
        """
        self.client.get(URL)
        Seguradora.objects.create(cnpj=gerar_cnpj(2), nome="Gama", uf="SP")

        self.assertEqual(self.client.get(URL).json()["count"], 1)
