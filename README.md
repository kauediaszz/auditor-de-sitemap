# Sitemap Auditor

Ferramenta de linha de comando (Python) que lê o índice de sitemaps de um site,
escolhe uma amostra das URLs mais recentes e audita cada uma: **status HTTP**,
**SEO** básico, **dados estruturados (JSON-LD / schema.org)** e **Core Web Vitals**.
Gera relatórios em CSV, Markdown e Excel e um **dashboard local** com botão para
rodar novas auditorias só com URLs ainda não vistas.

> **English summary:** a configurable sitemap auditor. Point it at your sitemap
> index, describe your sitemap layout in `config/config.yaml`, and it checks
> HTTP status, SEO tags, JSON-LD against rule files you edit (no code changes),
> and Core Web Vitals (CrUX/PageSpeed). Outputs CSV/Markdown/Excel plus a local
> dashboard. It is meant to be used as a **template**: copy, configure, adapt.
> The docs below are in Portuguese.

Este repositório é um **modelo (template)**: nenhum dado de um site real vem
incluído. Você descreve o seu site em arquivos de configuração e o resto funciona.

---

## Como funciona

```
sitemap-index.xml ──► leitor de sitemaps ──► amostra de URLs (mais recentes)
                                                   │
        ┌──────────────────┬───────────────────────┼───────────────────┐
        ▼                  ▼                       ▼                   ▼
   status HTTP          SEO (HTML)         schema (JSON-LD)     Core Web Vitals
 (retry, redirects)  title, canonical,   regras em arquivos    CrUX ou PageSpeed
                     H1, noindex, OG        JSON externos      (amostra menor)
        └──────────────────┴───────────────────────┴───────────────────┘
                                   │
                                   ▼
        saida/AAAA-MM-DD_HHMMSS/  auditoria.csv · schema_detalhe.csv ·
        resumo.md · auditoria.xlsx · cwv_reprovados.xlsx · dashboard/
```

1. **Leitura dos sitemaps.** O auditor baixa o índice mestre (`sitemapindex`),
   reconhece cada sitemap filho por um **papel** (matérias, galerias, vídeos,
   rotas principais) e coleta as URLs com `lastmod`. Cada URL fica sempre
   ligada ao sitemap de origem.
2. **Amostragem.** Matérias, galerias e vídeos são amostrados: as *N* mais
   recentes por `lastmod`. As **rotas principais** (papel `homes`) são sempre
   auditadas por completo.
3. **Verificações.** Status HTTP e SEO rodam sobre a amostra inteira; o schema
   é validado contra as regras do *tipo de página*; o Core Web Vitals roda numa
   amostra menor (homes + 1 matéria por categoria), porque só páginas com
   tráfego têm dados de campo.
4. **Saída em duas fases.** Primeiro os relatórios rápidos (CSV, Markdown,
   Excel); depois o Core Web Vitals (lento) e os arquivos finais.
5. **Histórico.** As URLs auditadas são registradas em `saida/urls_auditadas.json`
   para que uma nova rodada (`--only-new` ou o botão do dashboard) pegue só as
   próximas, sem repetir.

### O que é verificado

| Verificação | O que olha |
|---|---|
| Inventário | URL repetida no sitemap, URL com fragmento (`#`), host sem `www` e *slugs* legados (os dois últimos são opcionais) |
| Status HTTP | status final, cadeia de redirects, tempo; “fora do ar” = 4xx/5xx, timeout ou erro de rede (com *retry* para não acusar falhas passageiras) |
| SEO | `title` e `meta description` (tamanho é só aviso), `canonical`, `noindex`, um único `H1`, tags Open Graph |
| Schema | blocos `application/ld+json`, `@graph` achatado e validado por regras JSON: campos obrigatórios, contagem de tipos, datas ISO, `@id` consistente, dimensões de imagem |
| Core Web Vitals | LCP, INP, CLS (e TBT no PageSpeed), faixas do Google: bom / precisa melhorar / ruim |

Limitações: o auditor **não executa JavaScript** (lê o HTML que o servidor
entrega) e os limites de tamanho de `title`/`description` são estimativas, não
regras do Google.

---

## Começando

Requer Python 3.11+.

```bash
git clone <este-repositorio>
cd auditor-de-sitemap

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

pytest -q                          # confere se tudo funciona (não acessa a internet)
```

### Arquivos que você precisa criar/editar

Este repositório traz só exemplos. Para auditar o **seu** site:

| Arquivo | O que fazer | Vai para o Git? |
|---|---|---|
| `config/config.local.yaml` | Copie `config/config.yaml` e edite: URL do índice, papéis dos sitemaps, tipos de página, quantidades | **Não** (está no `.gitignore`) |
| `.env` | Copie `.env.example` e preencha `PSI_API_KEY` (opcional; só para Core Web Vitals) | **Não** (está no `.gitignore`) |
| `config/schema_rules/<tipo>.json` | Ajuste/crie as regras de schema do seu site, um arquivo por tipo de página | Sim, se não tiver dados sensíveis |

```bash
cp config/config.yaml config/config.local.yaml
cp .env.example .env
# edite os dois arquivos
python -m auditor.main --config config/config.local.yaml
```

A pasta `saida/` é criada sozinha a cada execução.

---

## Usando como template para o seu site

Tudo que é específico de um site fica em configuração, não no código.

### 1. Descreva seus sitemaps (`config.yaml`)

```yaml
sources:
  sitemap_index: https://www.seusite.com/sitemap-index.xml
  sitemaps:                       # papel -> sufixo do caminho do sitemap filho
    articles: /sitemap-articles.xml
    videos:   [/sitemap-videos.xml, /videos/sitemap.xml]   # aceita lista
    homes:    /sitemap-pages.xml
    # galleries: não tem? apague a linha: papéis ausentes não são lidos
```

O auditor percorre o índice mestre e associa cada sitemap filho ao papel cujo
sufixo bate com o final do caminho. Só os papéis listados são exigidos.

| Papel | Uso |
|---|---|
| `articles` | conteúdo editorial / páginas de detalhe (amostrado) |
| `galleries` | galerias de imagens (amostrado) |
| `videos` | páginas de vídeo (amostrado) |
| `homes` | rotas principais: home, categorias, páginas fixas (**sempre todas**) |

### 2. Classifique as páginas (`page_types`)

```yaml
page_types:            # a primeira regra que casar vale
  - prefix: /videos/
    type: video
  - prefix: /tables/
    type: table
```

Sem regra, o tipo vem do papel (`articles` → `article`, `videos` → `video`,
`galleries` → `gallery`), a raiz do site é `home` e o resto é `other`.

### 3. Escreva as regras de schema (`config/schema_rules/`)

Cada tipo de página tem um arquivo `<tipo>.json` com as regras de JSON-LD.
Os exemplos incluídos (`article`, `home`, `video`, `other`) são só ponto de
partida: **ajuste ao que o seu site realmente emite**.

```json
{
  "page_type": "article",
  "types": {
    "NewsArticle": {
      "min_count": 1,
      "max_count": 1,
      "required": { "headline": "str", "datePublished": "str", "author": "object_or_list" },
      "iso_dates": ["datePublished", "dateModified"],
      "date_not_before": [["dateModified", "datePublished"]]
    },
    "BreadcrumbList": { "min_count": 1, "required": { "itemListElement": "list" } }
  }
}
```

Para um tipo de página sem arquivo de regras, o auditor apenas registra os tipos
de schema detectados, sem validar.

### 4. Alertas de inventário opcionais

```yaml
inventory:
  require_www: true
  legacy_slugs: [site-antigo, /old-section/]
```

### 5. Quantidades e limites

```yaml
sampling: { articles: 80, galleries: 20, videos: 20 }
limits:   { max_urls_per_run: 500 }   # teto da soma, por execução
```

---

## Uso

```bash
# amostra padrão do config
python -m auditor.main --config config/config.local.yaml

# quantidades por tipo (0 pula o tipo)
python -m auditor.main --articles 150 --galleries 10 --videos 0

# só URLs que ainda não foram auditadas em execuções anteriores
python -m auditor.main --articles 100 --only-new

# todas as URLs do sitemap (ignora amostragem e teto; pode demorar muito)
python -m auditor.main --all

# Core Web Vitals no desktop, e abre o dashboard ao final
python -m auditor.main --desktop --serve
```

| Opção | Efeito |
|---|---|
| `--config` | caminho do YAML de configuração |
| `--articles/--galleries/--videos N` | quantidade por tipo (sobrescreve `sampling`) |
| `--only-new` | ignora URLs já registradas em `saida/urls_auditadas.json` |
| `--all` | audita tudo, sem limites |
| `--desktop` | Core Web Vitals com estratégia desktop (padrão: mobile) |
| `--serve`, `--port`, `--no-browser` | sobe o dashboard local ao final |
| `--output` | caminho do Excel principal (os demais arquivos vão junto) |
| `-v` | logs detalhados |

### Core Web Vitals

Configurado em `pagespeed` no `config.yaml`:

- `method: crux` (padrão): API do Chrome UX Report. Rápida, dados reais de
  usuários, sem TBT. Exige habilitar a *Chrome UX Report API* no projeto
  Google Cloud da chave. URLs sem dados de campo (pouco tráfego) ficam como
  `sem_dado`, sem derrubar a execução.
- `method: psi`: PageSpeed Insights. Lenta, inclui TBT e laboratório.

A chave vem da variável `PSI_API_KEY` (ambiente ou `.env`) e **nunca** é gravada
em relatórios ou logs. Sem chave, o Core Web Vitals é pulado com um aviso.

### Dashboard

```bash
python -m auditor.dashboard --config config/config.local.yaml
```

Abre um painel local (HTML/JS puro, sem CDN) com cards, gráficos e tabela
filtrável. O painel **Nova rodada** deixa você informar quantas matérias,
galerias e vídeos auditar e roda uma nova auditoria **só com URLs novas**. O
servidor aceita apenas conexões locais (`127.0.0.1`) e roda uma auditoria por
vez. Abrindo o `index.html` direto do disco, o botão fica desabilitado.

### Arquivos gerados (`saida/AAAA-MM-DD_HHMMSS/`)

- `auditoria.csv`: uma linha por URL (status, SEO, schema, Core Web Vitals).
- `schema_detalhe.csv`: uma linha por problema de schema.
- `resumo.md`: contagens, piores casos e alertas de inventário.
- `auditoria.xlsx`: planilha principal; `cwv_reprovados.xlsx`: só URLs reprovadas.
- `dashboard/`: painel estático (`index.html` + `dados.json`).

---

## Estrutura do projeto

```
auditor/
  main.py              fluxo principal e CLI
  config.py            leitura de .env e chaves
  history.py           histórico de URLs já auditadas
  sitemap/reader.py    leitura do índice, papéis, amostragem, classificação
  checks/              http_status, seo, schema (+ schema_rules), crux, core_web_vitals, inventory
  reports/             csv, markdown, excel, cwv
  dashboard/           gerador do painel, servidor local e template HTML
config/
  config.yaml          configuração de exemplo (copie e edite)
  schema_rules/        regras de JSON-LD de exemplo, um arquivo por tipo de página
tests/                 testes automatizados (respostas HTTP simuladas)
```

## Testes

```bash
pytest -q
```

Os testes usam respostas HTTP simuladas (`httpx.MockTransport`) e **não acessam
a internet**.

## Boas práticas e uso responsável

- Audite apenas sites **seus** ou para os quais você tenha **autorização**.
- Respeite o servidor: ajuste `http.concurrency` e `limits.max_urls_per_run`.
  Cada URL gera cerca de 3 requisições (status, SEO e schema).
- O auditor se identifica com o `User-Agent` `SitemapAuditor/0.1`.
- Nunca commite `.env`, `config/config.local.yaml` ou `saida/`: já estão no
  `.gitignore`, mas confira antes de publicar um *fork*.
