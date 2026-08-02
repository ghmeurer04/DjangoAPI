"""Configuração do projeto.

Tudo que varia por ambiente vem de variável de ambiente, com default de
desenvolvimento — é o que faz `docker compose up --build` funcionar num clone
limpo, sem passo prévio.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# --- Helpers de ambiente (sem dependência externa) -------------------------

def env(chave: str, padrao: str = "") -> str:
    return os.getenv(chave, padrao)


def env_bool(chave: str, padrao: bool = False) -> bool:
    valor = os.getenv(chave)
    if valor is None:
        return padrao
    return valor.strip().lower() in {"1", "true", "t", "yes", "y", "on", "sim"}


def env_int(chave: str, padrao: int) -> int:
    try:
        return int(os.getenv(chave, ""))
    except ValueError:
        return padrao


def env_float(chave: str, padrao: float) -> float:
    try:
        return float(os.getenv(chave, ""))
    except ValueError:
        return padrao


# --- Núcleo ----------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-inseguro-nao-usar-em-producao")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [host.strip() for host in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if host.strip()]

# `admin`, `auth`, `sessions` e `contenttypes` ficam de fora: a API é JSON e não
# tem autenticação. `staticfiles` fica por causa do Swagger UI.
INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "seguradoras",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# O Swagger UI é renderizado a partir de um template.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]


# --- Banco de dados (somente PostgreSQL) -----------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "seguradoras"),
        "USER": env("POSTGRES_USER", "seguradoras"),
        "PASSWORD": env("POSTGRES_PASSWORD", "seguradoras"),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env_int("DB_PORT", 5432),
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Cache da listagem (diferencial) ---------------------------------------
# LocMemCache é por processo, daí o compose subir 1 worker do gunicorn: com
# vários, uma importação não invalidaria o cache dos outros. Trocar por Redis
# é uma alteração isolada neste bloco.

CACHE_TTL = env_int("CACHE_TTL", 60)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "seguradoras",
        "TIMEOUT": CACHE_TTL,
    }
}


# --- DRF + documentação OpenAPI --------------------------------------------

REST_FRAMEWORK = {
    # A classe fica em `seguradoras.pagination` e não em `views`: o DRF resolve
    # esta setting no meio do import de `views.py`, o que fecharia um ciclo.
    "DEFAULT_PAGINATION_CLASS": "seguradoras.pagination.PaginacaoPadrao",
    "PAGE_SIZE": env_int("PAGE_SIZE", 20),
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    # Sem o Browsable API do DRF, `sessions` e `auth` deixam de ser
    # necessários; a documentação interativa fica em /api/docs/.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Vazio, senão o schema anunciaria cookieAuth e basicAuth — uma
    # autenticação que a API não tem.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    # Obrigatório ao remover `django.contrib.auth`: o padrão do DRF é
    # AnonymousUser, cujo import puxa `contenttypes`.
    "UNAUTHENTICATED_USER": None,
    # O payload de importação é um array, sem representação em formulário.
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "API de Catálogo de Seguradoras",
    "DESCRIPTION": (
        "Importação em lote com upsert por CNPJ e enriquecimento de dados "
        "via BrasilAPI executado fora do ciclo request/response."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}


# --- Integração com a BrasilAPI --------------------------------------------

BRASILAPI_BASE_URL = env("BRASILAPI_BASE_URL", "https://brasilapi.com.br/api/cnpj/v1")
BRASILAPI_TIMEOUT_CONNECT = env_float("BRASILAPI_TIMEOUT_CONNECT", 3.05)
BRASILAPI_TIMEOUT_READ = env_float("BRASILAPI_TIMEOUT_READ", 10.0)
BRASILAPI_MAX_RETRIES = env_int("BRASILAPI_MAX_RETRIES", 2)


# --- Enriquecimento em background ------------------------------------------

MODO_TESTE = sys.argv[1:2] == ["test"]

# Thread em background (True) ou execução inline no request (False).
ENRIQUECIMENTO_ASSINCRONO = env_bool("ENRIQUECIMENTO_ASSINCRONO", True)

# Se a importação dispara o enriquecimento. Desligado, ele só acontece via
# `manage.py enriquecer_seguradoras`.
ENRIQUECIMENTO_AO_IMPORTAR = env_bool("ENRIQUECIMENTO_AO_IMPORTAR", True)

if MODO_TESTE:
    # Nenhuma thread real e nenhuma requisição saindo por acidente. Os testes
    # de enriquecimento religam as flags com @override_settings e HTTP mockado.
    ENRIQUECIMENTO_ASSINCRONO = False
    ENRIQUECIMENTO_AO_IMPORTAR = False


# --- Internacionalização ---------------------------------------------------
# pt-br faz as mensagens de validação do DRF saírem em português.

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"


# --- Logging ---------------------------------------------------------------
# É aqui que aparecem as falhas da BrasilAPI, conforme o requisito de "logar o
# erro e manter o registro apenas com os dados básicos".

LOG_LEVEL = env("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "padrao": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "padrao",
        },
    },
    "loggers": {
        # Declarado com propagate=False porque o Django aplica o DEFAULT_LOGGING
        # antes deste bloco: sem isto, cada evento sairia duas vezes com
        # DEBUG=True, uma delas sem formatação.
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
}

if MODO_TESTE:
    # Vários testes provocam 400 e 404 de propósito. Só `django.request` é
    # silenciado; os logs da aplicação continuam visíveis e verificáveis.
    LOGGING["loggers"]["django.request"] = {
        "handlers": ["console"],
        "level": "ERROR",
        "propagate": False,
    }
