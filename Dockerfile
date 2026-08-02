# Estágio 1 — dependências num virtualenv isolado.
# O que só serve para instalar (pip, cache) fica confinado aqui.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --requirement requirements.txt


# Estágio 2 — só o interpretador, o virtualenv pronto e o código.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app
COPY --chown=appuser:appuser . .
USER appuser

EXPOSE 8000

# 1 worker com múltiplas threads, e não vários workers: o cache é LocMemCache,
# que vive no processo. Com dois workers, uma importação num deles não
# invalidaria o cache do outro, e a listagem devolveria dados velhos.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "8", \
     "--access-logfile", "-"]
