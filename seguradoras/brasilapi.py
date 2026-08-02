"""Cliente da BrasilAPI.

Único ponto do projeto que fala HTTP com o mundo externo, e não conhece o ORM:
recebe um CNPJ, devolve `DadosCNPJ` ou levanta exceção tipada. Nenhuma exceção
da `requests` escapa daqui — quem decide o que logar é `services.py`.
"""

from dataclasses import dataclass

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from seguradoras.validators import validar_cnpj

# Limites das colunas de `Seguradora`: truncar aqui evita que uma resposta fora
# do padrão derrube o enriquecimento na hora de salvar.
TAMANHO_MAX_NOME_FANTASIA = 255
TAMANHO_MAX_SITUACAO_CADASTRAL = 100

STATUS_QUE_VALEM_RETENTAR = (429, 500, 502, 503, 504)

# A BrasilAPI devolve 429 para o User-Agent padrão do `requests` já na primeira
# requisição, e responde normalmente com qualquer agente próprio.
USER_AGENT = "catalogo-seguradoras/1.0 (+integracao-brasilapi)"


class BrasilAPIError(Exception):
    """Falha transitória: rede, timeout, 429 ou 5xx. Vale retentar."""


class ErroPermanenteCNPJ(BrasilAPIError):
    """Não muda numa nova tentativa — o command não deve reprocessar."""


class CNPJNaoEncontradoError(ErroPermanenteCNPJ):
    """404 — o CNPJ não existe na base consultada."""


class CNPJRecusadoError(ErroPermanenteCNPJ):
    """400 — a BrasilAPI recusou o CNPJ como malformado."""


@dataclass(frozen=True)
class DadosCNPJ:
    nome_fantasia: str = ""
    situacao_cadastral: str = ""


def _texto(valor, tamanho_max: int) -> str:
    """Normaliza um campo da resposta: nunca None, sempre dentro do limite."""
    if valor is None:
        return ""
    return str(valor).strip()[:tamanho_max]


class BrasilAPIClient:
    """Consulta dados cadastrais de CNPJ.

    A sessão é reaproveitada entre chamadas (keep-alive), então vale manter uma
    instância por lote. Serve como context manager.
    """

    def __init__(self, base_url=None, timeout=None, max_retries=None):
        self.base_url = (base_url or settings.BRASILAPI_BASE_URL).rstrip("/")

        # Timeout sempre explícito e separado em (conexão, leitura): sem ele uma
        # API pendurada travaria a thread indefinidamente.
        self.timeout = (
            timeout
            if timeout is not None
            else (settings.BRASILAPI_TIMEOUT_CONNECT, settings.BRASILAPI_TIMEOUT_READ)
        )

        tentativas = settings.BRASILAPI_MAX_RETRIES if max_retries is None else max_retries
        politica = Retry(
            total=tentativas,
            backoff_factor=0.5,
            status_forcelist=STATUS_QUE_VALEM_RETENTAR,
            allowed_methods=frozenset(["GET"]),
            # Sem isto o urllib3 dormiria o Retry-After sem teto: um
            # "Retry-After: 3600" travaria a thread por uma hora.
            respect_retry_after_header=False,
            # Devolve a resposta em vez de levantar, para o erro citar o status.
            raise_on_status=False,
        )

        adaptador = HTTPAdapter(max_retries=politica)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.mount("https://", adaptador)
        self.session.mount("http://", adaptador)

    def consultar_cnpj(self, cnpj: str) -> DadosCNPJ:
        """Consulta um CNPJ e extrai os dois campos do enriquecimento."""
        # O CNPJ vem do banco e é interpolado na URL; o modelo não tem
        # validador. Conferir aqui protege o path e poupa uma chamada inútil.
        if not validar_cnpj(cnpj):
            raise CNPJRecusadoError(f"CNPJ {cnpj!r} não é válido; consulta não realizada")

        url = f"{self.base_url}/{cnpj}"

        try:
            resposta = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BrasilAPIError(f"falha de rede ao consultar {cnpj}: {exc}") from exc

        # 404 e 400 são permanentes; o resto é transitório. Distinção conferida
        # contra a API real: DV inválido devolve 400, não 404.
        if resposta.status_code == requests.codes.not_found:
            raise CNPJNaoEncontradoError(f"CNPJ {cnpj} não encontrado na BrasilAPI")

        if resposta.status_code == requests.codes.bad_request:
            raise CNPJRecusadoError(f"BrasilAPI considerou o CNPJ {cnpj} inválido")

        if resposta.status_code != requests.codes.ok:
            raise BrasilAPIError(
                f"BrasilAPI respondeu {resposta.status_code} para o CNPJ {cnpj}"
            )

        try:
            corpo = resposta.json()
        except ValueError as exc:
            raise BrasilAPIError(f"resposta da BrasilAPI não é JSON para {cnpj}") from exc

        if not isinstance(corpo, dict):
            raise BrasilAPIError(f"resposta da BrasilAPI em formato inesperado para {cnpj}")

        return DadosCNPJ(
            nome_fantasia=_texto(corpo.get("nome_fantasia"), TAMANHO_MAX_NOME_FANTASIA),
            # A API expõe a situação em dois campos: um textual ("ATIVA") e um
            # numérico. O textual é o preferido.
            situacao_cadastral=_texto(
                corpo.get("descricao_situacao_cadastral") or corpo.get("situacao_cadastral"),
                TAMANHO_MAX_SITUACAO_CADASTRAL,
            ),
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "BrasilAPIClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()
