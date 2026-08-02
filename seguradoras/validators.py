"""Validação de CNPJ pelo módulo 11 da Receita Federal.

Funções puras, sem ORM nem DRF: testáveis isoladamente e reaproveitadas pela
importação e pelo cliente da BrasilAPI.
"""

import re

TAMANHO_CNPJ = 14
TAMANHO_BASE = 12

PESOS_PRIMEIRO_DIGITO = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
PESOS_SEGUNDO_DIGITO = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)

# Só os separadores da máscara são removidos; qualquer outro caractere invalida
# a entrada, senão "0000000000019a1" passaria como válido.
_SEPARADORES_MASCARA = re.compile(r"[.\-/\s]")

# [0-9] e não \d, que casaria dígitos de outros alfabetos Unicode.
_SOMENTE_DIGITOS = re.compile(r"[0-9]+")


def normalizar_cnpj(valor: str) -> str:
    """Tira a máscara e devolve os 14 dígitos. Levanta ValueError se não for um.

    Não confere os dígitos verificadores — isso é `validar_cnpj`.
    """
    if valor is None:
        raise ValueError("CNPJ não informado.")

    digitos = _SEPARADORES_MASCARA.sub("", str(valor).strip())

    if not digitos:
        raise ValueError("CNPJ não informado.")

    if not _SOMENTE_DIGITOS.fullmatch(digitos):
        raise ValueError("CNPJ deve conter apenas dígitos e os separadores . - /")

    if len(digitos) != TAMANHO_CNPJ:
        raise ValueError(f"CNPJ deve ter {TAMANHO_CNPJ} dígitos, recebeu {len(digitos)}.")

    return digitos


def _digito_verificador(digitos: str, pesos: tuple[int, ...]) -> str:
    """Um dígito verificador pelo módulo 11."""
    soma = sum(int(digito) * peso for digito, peso in zip(digitos, pesos))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def calcular_digitos_verificadores(base: str) -> str:
    """Os 2 dígitos verificadores de uma base de 12 dígitos."""
    if not _SOMENTE_DIGITOS.fullmatch(base or "") or len(base) != TAMANHO_BASE:
        raise ValueError(f"A base do CNPJ deve ter {TAMANHO_BASE} dígitos.")

    primeiro = _digito_verificador(base, PESOS_PRIMEIRO_DIGITO)
    segundo = _digito_verificador(base + primeiro, PESOS_SEGUNDO_DIGITO)
    return primeiro + segundo


def validar_cnpj(cnpj: str) -> bool:
    """Diz se os 14 dígitos formam um CNPJ válido. Espera entrada normalizada.

    Devolve False em vez de levantar, para servir como predicado.
    """
    if not cnpj or not _SOMENTE_DIGITOS.fullmatch(cnpj) or len(cnpj) != TAMANHO_CNPJ:
        return False

    # Sequências repetidas passam no módulo 11 mas não são CNPJs reais.
    if cnpj == cnpj[0] * TAMANHO_CNPJ:
        return False

    return cnpj[TAMANHO_BASE:] == calcular_digitos_verificadores(cnpj[:TAMANHO_BASE])


def formatar_cnpj(cnpj: str) -> str:
    """00000000000191 -> 00.000.000/0001-91, para a saída da API.

    Devolve a entrada intacta se o tamanho for inesperado, para nunca quebrar a
    serialização de um registro.
    """
    if not cnpj or len(cnpj) != TAMANHO_CNPJ:
        return cnpj
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
