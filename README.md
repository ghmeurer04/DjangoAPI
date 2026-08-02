# API de Catálogo de Seguradoras

Django 5.1 · Django REST Framework · PostgreSQL 16 · Docker

---

## Subindo o ambiente

```bash
docker compose up --build
```

Todas as variáveis têm default embutido no `docker-compose.yml`. O comando sobe o PostgreSQL, espera
o banco ficar saudável, aplica as migrações e inicia o Gunicorn.

| Endereço | O que é |
|---|---|
| http://localhost:8000/api/docs/ | Documentação interativa (Swagger UI) |
| http://localhost:8000/api/v1/seguradoras/ | Listagem |
| http://localhost:8000/api/schema/ | Documento OpenAPI 3 |

A raiz (`http://localhost:8000/`) redireciona para a documentação.

Para derrubar tudo, incluindo o volume do banco:

```bash
docker compose down -v
```

---

## Rodando os testes

```bash
docker compose exec web python manage.py test
```

Se a stack não estiver de pé:

```bash
docker compose run --rm web python manage.py test
```

São **106 testes** e a suíte **não acessa a internet**: toda comunicação com a
BrasilAPI é interceptada pela biblioteca `responses`, e os testes de
enriquecimento apontam para um domínio que nem resolve — se algum escapasse do
mock, falharia com erro de DNS. Também nenhuma thread real é criada: as
settings detectam o modo de teste e desligam o disparo automático, o que
mantém o resultado determinístico.

---

## Endpoints

### `POST /api/v1/seguradoras/importar/`

Recebe uma lista de seguradoras, grava os dados básicos e responde
imediatamente. O CNPJ pode vir com ou sem máscara, e a UF em qualquer caixa.

```bash
curl -X POST http://localhost:8000/api/v1/seguradoras/importar/ \
  -H "Content-Type: application/json" \
  -d '[
    {"nome": "BB Seguridade", "cnpj": "00.000.000/0001-91", "uf": "df"},
    {"nome": "Petrobras",     "cnpj": "33000167000101",     "uf": "RJ"}
  ]'
```

```json
{
  "total_recebidos": 2,
  "criados": 2,
  "atualizados": 0,
  "duplicados_no_payload": 0,
  "enriquecimento_agendado": 2
}
```

Os CNPJs acima são reais e públicos: alguns segundos após a importação eles
aparecem na listagem já com `nome_fantasia` e `situacao_cadastral` preenchidos pela BrasilAPI.

**Regra de upsert:** o CNPJ é a chave. Se já existir, o registro é atualizado em vez de duplicado.

**Validação tudo-ou-nada:** um item inválido rejeita o lote inteiro, com `400` e os erros indexados pela posição no payload:

```json
[
  {},
  {"cnpj": ["CNPJ inválido: os dígitos verificadores não conferem."]}
]
```

### `GET /api/v1/seguradoras/`

Listagem paginada, com filtros por UF e por trecho do nome.

```bash
curl "http://localhost:8000/api/v1/seguradoras/?uf=RJ"
curl "http://localhost:8000/api/v1/seguradoras/?nome=segur"                          # filtro por nome parcial
curl "http://localhost:8000/api/v1/seguradoras/?uf=SP&page=2&page_size=50"           # page size padrão 20, maximo 100
curl "http://localhost:8000/api/v1/seguradoras/?status_enriquecimento=CONCLUIDO"
```

---

## Estratégia para desacoplar a chamada à API externa

O enriquecimento nunca acontece dentro do request. O `POST` grava os dados
básicos, responde `201` em poucos milissegundos e **encaminha o trabalho**.

Há dois gatilhos, e ambos chamam exatamente a mesma função:

```
POST /importar/  ──▶ transaction.on_commit ──▶ Thread(daemon) ──┐
                                                                 ├──▶ enriquecer_pendentes()
manage.py enriquecer_seguradoras ────────────────────────────────┘
```

**Por que `transaction.on_commit`.** A thread abre a própria conexão com o
banco. Se ela subisse antes do commit, faria um `SELECT` que não enxergaria os
registros recém-inseridos e não encontraria nada para enriquecer. Registrar o
disparo no `on_commit` garante que ela só comece depois dos dados existirem.

**Por que o command existe.** A thread não sobrevive a
um restart do container, e se a BrasilAPI estiver fora do ar no momento da
importação o registro fica sem enriquecer. O command varre o banco e
reprocessa o que ficou para trás — é a rede de segurança, e pode ir para um
cron:

```bash
docker compose exec web python manage.py enriquecer_seguradoras
docker compose exec web python manage.py enriquecer_seguradoras --limite 100
docker compose exec web python manage.py enriquecer_seguradoras --forcar
```

**O ciclo de vida do status.** Cada registro carrega um
`status_enriquecimento`, que é o que permite ao command saber o que
reprocessar:

| Status | Significado | Reprocessado? |
|---|---|---|
| `PENDENTE` | Importado, ainda não consultado | sim |
| `CONCLUIDO` | Enriquecido com sucesso | não |
| `NAO_ENCONTRADO` | A API respondeu 404 ou 400 | não |
| `ERRO` | Rede, timeout, 429 ou 5xx | sim |

A distinção entre `NAO_ENCONTRADO` e `ERRO` é o que evita gastar uma
requisição por execução em registros que nunca vão resolver.

**Tratamento de falhas.** Nenhuma falha da BrasilAPI escapa do enriquecimento:
vira log e status, com o registro preservado com os dados básicos. Um CNPJ
problemático não derruba o lote nem o sistema. Os erros aparecem em:

```bash
docker compose logs web
```

O cliente HTTP (`requests`) usa timeout explícito e separado em conexão e
leitura, e uma política de retry com backoff para `429` e `5xx`. O cabeçalho
`Retry-After` é deliberadamente ignorado: o `urllib3` dormiria o que ele
mandasse, sem teto, e um `Retry-After: 3600` prenderia a thread por uma hora.
Nenhuma exceção da biblioteca escapa de `brasilapi.py`.

O cliente também **se identifica com um `User-Agent` próprio**, já que a
BrasilAPI devolve `429` para o agente padrão do `requests` já na primeira
requisição, e responde normalmente com qualquer agente identificado.

Pelo mesmo motivo o enriquecimento processa **em sequência**, reaproveitando
uma única conexão HTTP: a BrasilAPI é pública e tem limite de requisições, e
paralelizar renderia `429` em vez de velocidade.

---

## Onde está cada requisito

| Requisito do teste | Arquivo |
|---|---|
| `POST` de importação | `seguradoras/views.py` → `SeguradoraViewSet.importar` |
| Regra de upsert por CNPJ | `seguradoras/services.py` → `importar_seguradoras` |
| `GET` com paginação e filtros | `seguradoras/views.py`, `filters.py`, `pagination.py` |
| Enriquecimento fora do request | `seguradoras/services.py` → `agendar_enriquecimento` |
| Consumo da API externa | `seguradoras/brasilapi.py` |
| Validação de CNPJ | `seguradoras/validators.py` |
| Management command | `seguradoras/management/commands/enriquecer_seguradoras.py` |
| Cache da listagem | `seguradoras/cache.py` |
| Docker e multi-stage | `Dockerfile`, `docker-compose.yml` |
| Testes com mock | `seguradoras/tests/` |

### Estrutura

```
config/
  settings.py          Configuração por variáveis de ambiente
  urls.py              Todas as rotas do projeto
seguradoras/
  models.py            Seguradora + StatusEnriquecimento
  validators.py        Validação de CNPJ (módulo 11), sem ORM nem DRF
  serializers.py       Entrada da importação e saída da listagem
  filters.py           Filtros por UF e nome
  pagination.py        Tamanho de página e teto de ?page_size=
  views.py             ViewSet com os dois endpoints
  cache.py             Chave e invalidação do cache da listagem
  services.py          Upsert em lote + enriquecimento + agendamento
  brasilapi.py         Cliente HTTP da API externa
  management/commands/enriquecer_seguradoras.py
  tests/               Um arquivo por área, mais helpers compartilhados
```

O fluxo de uma importação atravessa a pilha nesta ordem:

```
views.importar
  → serializers.SeguradoraImportItemSerializer   valida e normaliza
  → services.importar_seguradoras                upsert em 2 queries
  → cache.invalidar_listagem
  → services.agendar_enriquecimento              devolve a resposta aqui
        └── thread ─→ services.enriquecer_pendentes
                        └── brasilapi.BrasilAPIClient.consultar_cnpj
```

---

## Detalhes de implementação

**Upsert eficiente.** A importação custa **2 queries**, independentemente do
tamanho do lote: um `SELECT` dos CNPJs já existentes (apenas para relatar
criados vs. atualizados) e um `INSERT ... ON CONFLICT (cnpj) DO UPDATE`.
Resolver o conflito dentro do banco, em vez de decidir na aplicação com um
`SELECT` seguido de `INSERT`, também elimina a condição de corrida entre duas
importações simultâneas do mesmo CNPJ. Há um teste que compara o custo de um
lote de 3 com o de 100 e exige que sejam iguais.

Atualizar um registro **não** reseta o enriquecimento: ele depende só do CNPJ,
que por definição não mudou num update. Reconsultar seria desperdício.

**Validação de CNPJ.** Implementada à mão em `validators.py` (módulo 11, com
rejeição de sequências repetidas), sem dependência externa. O algoritmo está
ancorado em CNPJs reais nos testes — Banco do Brasil, Petrobras, Bradesco e
Itaú —, de modo que uma alteração acidental nos pesos quebre a suíte.

**Cache da listagem.** A chave é derivada da querystring normalizada, e a
invalidação acontece a cada importação e a cada execução do enriquecimento —
inclusive quando ela só produz erros, já que a listagem pode ser filtrada por
`status_enriquecimento`. Como o cache é exclusivo desta aplicação e só guarda
listagem, invalidar é simplesmente limpá-lo.

**Multi-stage build.** O `Dockerfile` monta as dependências num virtualenv no
estágio `builder` e copia só o resultado para a imagem final: **328 MB → 307
MB**. O ganho é modesto porque `psycopg[binary]` distribui wheels e não há
toolchain de compilação a descartar — o que sobra é o cache do `pip`. A imagem
final também roda como usuário sem privilégios.

---

## Variáveis de ambiente

Todas opcionais: os defaults deixam o `docker compose up --build` funcionar
sem configuração.

| Variável | Padrão | Para que serve |
|---|---|---|
| `PORTA_APP` | `8000` | Porta publicada no host |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `seguradoras` | Credenciais do banco |
| `DJANGO_SECRET_KEY` | chave de desenvolvimento | Trocar obrigatoriamente em produção, junto com `POSTGRES_PASSWORD` |
| `DJANGO_DEBUG` | `0` | Modo debug |
| `DJANGO_ALLOWED_HOSTS` | `*` | Hosts aceitos, separados por vírgula |
| `LOG_LEVEL` | `INFO` | Nível dos logs |
| `PAGE_SIZE` | `20` | Tamanho padrão da página |
| `CACHE_TTL` | `60` | Segundos de vida das entradas de cache |
| `BRASILAPI_BASE_URL` | endpoint público | Útil para apontar a um mock |
| `BRASILAPI_TIMEOUT_CONNECT` / `BRASILAPI_TIMEOUT_READ` | `3.05` / `10` | Timeouts em segundos |
| `BRASILAPI_MAX_RETRIES` | `2` | Tentativas extras em `429` e `5xx` |
| `ENRIQUECIMENTO_ASSINCRONO` | `1` | `0` executa o enriquecimento dentro do request |
| `ENRIQUECIMENTO_AO_IMPORTAR` | `1` | `0` deixa o enriquecimento só a cargo do command |

O banco **não** publica porta no host: o `web` o alcança por `db:5432` na rede
interna do compose. Isso evita conflito com um PostgreSQL já instalado na
máquina — um cenário comum, e silencioso o bastante para custar tempo de
diagnóstico.

---

## Limitações conhecidas

Registradas de propósito, porque são decisões e não descuidos.

**A thread não sobrevive a um restart.** Se o container reiniciar durante o
enriquecimento, os registros ficam em `PENDENTE`. É exatamente o que o
management command resolve. Num sistema com volume de produção, a resposta
correta seria Celery ou RQ, com fila persistente, retentativa com backoff e
workers observáveis — a thread é adequada ao escopo deste teste, não a um
serviço real sob carga.

**O cache é local ao processo.** Com `LocMemCache`, cada worker do Gunicorn
teria a própria cópia, e uma importação atendida por um worker não invalidaria
o cache do outro. Por isso o `docker-compose.yml` sobe **1 worker com 8
threads** em vez de vários workers. Trocar o backend por Redis é uma alteração
isolada no bloco `CACHES` das settings, e é o que destravaria escalar em
processos.

**A imagem inclui as dependências de teste.** `responses` está no
`requirements.txt` único, de propósito — é o que permite rodar a suíte dentro
do container, como as instruções acima orientam. Num deploy real, valeria
separar `requirements-dev.txt`.

**O Swagger UI carrega de CDN.** Sem acesso à internet, a página de
documentação não renderiza — o documento em `/api/schema/` continua
disponível. Servir os assets localmente exigiria `drf-spectacular[sidecar]`,
WhiteNoise e `collectstatic`.

**A importação é tudo-ou-nada.** Um item inválido rejeita o lote inteiro. A
alternativa — aceitar os válidos e reportar os rejeitados — é defensável, mas
torna a resposta ambígua sobre o que foi gravado.
