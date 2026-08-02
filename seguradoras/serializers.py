"""Serializers da API.

`SeguradoraImportItemSerializer` é o único portão de entrada: normaliza o CNPJ
e a UF. O serviço de importação confia que os dados já chegam nesse formato.
"""

from rest_framework import serializers

from seguradoras.models import SIGLAS_UF, Seguradora
from seguradoras.validators import normalizar_cnpj, validar_cnpj


class SeguradoraImportItemSerializer(serializers.Serializer):
    """Um item da lista recebida em POST /api/v1/seguradoras/importar/."""

    nome = serializers.CharField(max_length=255)
    cnpj = serializers.CharField(
        help_text="Com ou sem máscara. É armazenado apenas com os 14 dígitos.",
    )
    # CharField e não ChoiceField: o ChoiceField rejeitaria "pr" antes do
    # validate_uf conseguir passar para maiúsculas.
    uf = serializers.CharField(
        max_length=2,
        help_text="Sigla da unidade federativa. Aceita minúsculas.",
    )

    def validate_cnpj(self, valor: str) -> str:
        """Tira a máscara e confere os dígitos verificadores."""
        try:
            cnpj = normalizar_cnpj(valor)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

        if not validar_cnpj(cnpj):
            raise serializers.ValidationError(
                "CNPJ inválido: os dígitos verificadores não conferem."
            )
        return cnpj

    def validate_uf(self, valor: str) -> str:
        """Passa para maiúsculas e confere contra as 27 siglas."""
        uf = valor.strip().upper()
        if uf not in SIGLAS_UF:
            raise serializers.ValidationError(
                f"UF inválida: {valor!r}. Informe uma das 27 siglas brasileiras."
            )
        return uf


class ResumoImportacaoSerializer(serializers.Serializer):
    """Resposta do POST de importação, e o schema dela no OpenAPI."""

    total_recebidos = serializers.IntegerField(
        read_only=True, help_text="Quantidade de itens no payload."
    )
    criados = serializers.IntegerField(read_only=True)
    atualizados = serializers.IntegerField(read_only=True)
    duplicados_no_payload = serializers.IntegerField(
        read_only=True,
        help_text=(
            "CNPJs repetidos dentro do próprio payload. Só a última ocorrência "
            "de cada um é gravada, então `criados + atualizados` fica menor que "
            "`total_recebidos` quando este campo é maior que zero."
        ),
    )
    # Vem do retorno de `agendar_enriquecimento`, não do tamanho do lote: com o
    # enriquecimento automático desligado, nada é agendado e o número é 0.
    enriquecimento_agendado = serializers.IntegerField(
        read_only=True,
        help_text=(
            "Quantos CNPJs foram encaminhados para o enriquecimento em "
            "background. A consulta à BrasilAPI acontece fora desta resposta."
        ),
    )


class SeguradoraSerializer(serializers.ModelSerializer):
    """Saída da listagem. Tudo somente leitura: escrita só via importação."""

    cnpj_formatado = serializers.CharField(
        read_only=True,
        help_text="O mesmo CNPJ com a máscara aplicada, por conveniência.",
    )

    class Meta:
        model = Seguradora
        fields = (
            "id",
            "cnpj",
            "cnpj_formatado",
            "nome",
            "uf",
            "nome_fantasia",
            "situacao_cadastral",
            "status_enriquecimento",
            "criado_em",
            "atualizado_em",
        )
        read_only_fields = fields
