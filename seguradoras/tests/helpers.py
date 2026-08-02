"""Dados e utilitários compartilhados pelos testes.

Ficam num módulo próprio para que os arquivos de teste não precisem importar
uns dos outros.
"""

from seguradoras.validators import calcular_digitos_verificadores

# CNPJs reais e públicos, âncora do algoritmo: se os pesos ou o módulo 11 forem
# alterados por engano, estes são os primeiros a quebrar.
CNPJ_BANCO_DO_BRASIL = "00000000000191"
CNPJ_PETROBRAS = "33000167000101"
CNPJ_BRADESCO = "60746948000112"
CNPJ_ITAU = "60701190000104"

# CNPJs sintéticos com dígitos verificadores válidos.
CNPJ_FICTICIO_A = "11222333000181"
CNPJ_FICTICIO_B = "45678901000175"
CNPJ_FICTICIO_C = "98765432000198"


def gerar_cnpj(indice: int) -> str:
    """CNPJ sintético válido e estável por índice, para montar lotes."""
    base = f"{indice:08d}0001"
    return base + calcular_digitos_verificadores(base)


def item(indice: int, nome: str | None = None, uf: str = "PR") -> dict:
    """Um item de payload já validado, no formato que o serviço espera."""
    return {
        "cnpj": gerar_cnpj(indice),
        "nome": nome or f"Seguradora {indice}",
        "uf": uf,
    }
