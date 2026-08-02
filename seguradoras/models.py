from django.db import models

from seguradoras.validators import formatar_cnpj

# Tupla e não conjunto: a ordem precisa ser estável, senão a migração e o
# schema OpenAPI mudariam a cada execução.
SIGLAS_UF = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
)

UFS = [(sigla, sigla) for sigla in SIGLAS_UF]


class StatusEnriquecimento(models.TextChoices):
    """Ciclo de vida do enriquecimento. É o que diz ao command o que reprocessar."""

    PENDENTE = "PENDENTE", "Pendente"
    CONCLUIDO = "CONCLUIDO", "Concluído"
    NAO_ENCONTRADO = "NAO_ENCONTRADO", "Não encontrado"
    ERRO = "ERRO", "Erro"

    @classmethod
    def reprocessaveis(cls) -> tuple[str, ...]:
        """Status que vale tentar de novo — o resto não muda de resposta."""
        return (cls.PENDENTE.value, cls.ERRO.value)


class Seguradora(models.Model):
    # Dados do payload de importação.
    cnpj = models.CharField(
        "CNPJ",
        max_length=14,
        unique=True,  # chave do upsert: é o UNIQUE que o ON CONFLICT usa
        help_text="Somente dígitos, sem máscara.",
    )
    nome = models.CharField(max_length=255)
    uf = models.CharField("UF", max_length=2, choices=UFS)

    # Preenchidos pelo enriquecimento; vazios até a BrasilAPI responder.
    nome_fantasia = models.CharField(max_length=255, blank=True, default="")
    situacao_cadastral = models.CharField(max_length=100, blank=True, default="")
    status_enriquecimento = models.CharField(
        max_length=20,
        choices=StatusEnriquecimento,
        default=StatusEnriquecimento.PENDENTE,
    )

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "seguradora"
        verbose_name_plural = "seguradoras"
        # `id` como desempate: sem ordenação determinística a paginação repete
        # ou pula registros quando há nomes iguais.
        ordering = ("nome", "id")
        indexes = [
            models.Index(fields=["uf"], name="seguradora_uf_idx"),  # filtro da listagem
            models.Index(fields=["status_enriquecimento"], name="seguradora_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.nome} ({self.cnpj_formatado})"

    @property
    def cnpj_formatado(self) -> str:
        """CNPJ com máscara, exposto na listagem."""
        return formatar_cnpj(self.cnpj)
