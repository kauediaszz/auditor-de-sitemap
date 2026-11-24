# Sitemap Auditor

Iae! Fico feliz que você queira conhecer melhor o projeto. Vou te explicar como ele funciona, o que você consegue fazer com ele e como colocar tudo para rodar.

Basicamente, o **Sitemap Auditor** é uma ferramenta desenvolvida em Python que automatiza auditorias de páginas de um site. Em vez de precisar acessar várias URLs manualmente para conferir se está tudo certo, você configura a ferramenta e deixa ela fazer esse trabalho.

Ela verifica status HTTP, SEO, dados estruturados (JSON-LD / Schema.org) e Core Web Vitals. No final, gera relatórios com os resultados e disponibiliza um dashboard local para facilitar a visualização.

Um detalhe importante: esse projeto é um template. Isso significa que ele não vem configurado para um site específico. Você precisa informar qual site deseja auditar e ajustar algumas configurações antes de começar.

---

## 1. Como o projeto funciona?

Vamos imaginar que você queira auditar um portal de notícias.

O primeiro passo é informar o endereço do sitemap principal do site, aquele arquivo que reúne os demais sitemaps.

A partir dele, o auditor identifica os diferentes grupos de páginas, como:

- Matérias;
- Galerias;
- Vídeos;
- Páginas principais, como home e categorias.

Depois disso, ele seleciona uma amostra das URLs mais recentes e começa a executar as verificações.

Cada página passa por algumas etapas:

**Status HTTP:** verifica se a página está respondendo corretamente, se apresenta erros 404 ou 500 e se existem redirecionamentos.

**SEO:** verifica informações importantes do HTML, como title, meta description, canonical, H1, noindex e Open Graph.

**Dados estruturados:** procura os blocos JSON-LD da página e verifica se eles seguem as regras definidas para aquele tipo de conteúdo.

**Core Web Vitals:** consulta métricas de desempenho, como LCP, INP e CLS, utilizando dados do Google CrUX ou PageSpeed Insights.

Ao terminar, tudo é organizado em relatórios para você analisar os resultados.

## 2. Como configurar o projeto?

Antes de executar a ferramenta, precisamos preparar o ambiente.

Você vai precisar ter o Python 3.11 ou superior instalado.

Primeiro, clone o repositório e entre na pasta do projeto:

```
git clone <url-do-repositorio>
cd auditor-de-sitemap
```

Agora, vamos criar um ambiente virtual para instalar as dependências sem misturar os pacotes com outros projetos:

```
python3 -m venv .venv
```

Ative o ambiente virtual:

```
# Linux / WSL
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

Com o ambiente ativado, instale as dependências:

```
pip install -r requirements.txt
```

Pronto! Agora vamos conferir se tudo está funcionando:

```
pytest -q
```

Esse comando executa os testes automatizados do projeto. Eles utilizam respostas simuladas, então não precisam acessar a internet.

## 3. Configurando o seu site

Aqui começa a parte mais importante.

Como o projeto é um template, precisamos dizer qual site será auditado.

Primeiro, copie os arquivos de exemplo:

```
cp config/config.yaml config/config.local.yaml
cp .env.example .env
```

O arquivo `config.local.yaml` será responsável pelas configurações do site.

Abra esse arquivo e procure a configuração de sitemap:

```
sources:
  sitemap_index: https://www.seusite.com/sitemap-index.xml
```

Substitua o endereço pelo sitemap principal do site que você deseja auditar.

Depois, configure os papéis dos sitemaps:

```
sources:
  sitemap_index: https://www.seusite.com/sitemap-index.xml
  sitemaps:
    articles: /sitemap-articles.xml
    videos: /sitemap-videos.xml
    homes: /sitemap-pages.xml
```

Cada papel representa um tipo de conteúdo.

Por exemplo, `articles` representa matérias, `videos` representa páginas de vídeos e `homes` representa as páginas principais.

Você não precisa cadastrar todos os tipos. Se o seu site não possui galerias, por exemplo, basta não adicionar esse papel.

## 4. Configurando as regras de dados estruturados

Agora vamos falar sobre o Schema.org.

O auditor consegue verificar se as páginas estão emitindo os dados estruturados esperados. Para isso, ele utiliza arquivos JSON com regras específicas para cada tipo de página.

Esses arquivos ficam dentro de:

```
config/schema_rules/
```

Você encontrará exemplos como:

```
article.json
home.json
video.json
other.json
```

Imagine que você queira verificar uma matéria. Dentro do arquivo `article.json`, pode definir que ela precisa apresentar um `NewsArticle`, com informações como headline, datePublished e author.

Exemplo:

```
{
  "page_type": "article",
  "types": {
    "NewsArticle": {
      "min_count": 1,
      "max_count": 1,
      "required": {
        "headline": "str",
        "datePublished": "str",
        "author": "object_or_list"
      }
    }
  }
}
```

Dessa forma, quando o auditor encontrar uma matéria, ele vai conferir se essas informações estão presentes.

A vantagem é que você consegue alterar as regras diretamente nos arquivos JSON, sem precisar modificar o código Python.

## 5. Executando uma auditoria

Com as configurações prontas, chegou a hora de executar o projeto.

Para iniciar uma auditoria utilizando as quantidades definidas no arquivo de configuração, execute:

```
python -m auditor.main --config config/config.local.yaml
```

Você também pode personalizar a execução.

Por exemplo, se quiser auditar 150 matérias, 10 galerias e não quiser verificar vídeos:

```
python -m auditor.main --articles 150 --galleries 10 --videos 0
```

Outra funcionalidade interessante é a opção `--only-new`.

Ela utiliza um histórico das URLs já auditadas e evita que você fique verificando as mesmas páginas em todas as execuções.

```
python -m auditor.main --articles 100 --only-new
```

Se quiser auditar absolutamente todas as URLs encontradas nos sitemaps, pode utilizar:

```
python -m auditor.main --all
```

Só tenha atenção: essa opção ignora os limites de amostragem e pode demorar bastante, dependendo do tamanho do site.

## 6. Como utilizar o dashboard?

Além dos relatórios, o projeto possui um dashboard local.

Para iniciar o painel, execute:

```
python -m auditor.dashboard --config config/config.local.yaml
```

Ele apresenta cards, gráficos e uma tabela com os resultados das auditorias.

Uma funcionalidade interessante é o botão **Nova rodada**.

Por ele, você consegue informar quantas matérias, galerias e vídeos deseja auditar. O sistema executa uma nova rodada utilizando somente URLs que ainda não foram auditadas.

Isso facilita bastante o acompanhamento contínuo do site.

## 7. Onde encontrar os resultados?

A cada execução, o projeto cria uma nova pasta dentro de `saida/`, identificada pela data e horário da auditoria.

Dentro dela, você encontrará:

- `auditoria.csv`: resultados gerais de cada URL.
- `schema_detalhe.csv`: problemas encontrados nos dados estruturados.
- `resumo.md`: resumo da execução, com contagens e principais alertas.
- `auditoria.xlsx`: planilha completa para análise.
- `cwv_reprovados.xlsx`: páginas que não passaram nas verificações de Core Web Vitals.
- `dashboard/`: versão estática do painel, com os dados daquela execução.

Assim, você consegue consultar os resultados, comparar execuções e identificar problemas que precisam de atenção.

## 8. Algumas limitações importantes

Antes de utilizar a ferramenta, vale conhecer alguns detalhes:

- O auditor não executa JavaScript. Ele analisa o HTML entregue pelo servidor.
- O Core Web Vitals depende da disponibilidade de dados no Google CrUX ou PageSpeed Insights.
- Para utilizar o PageSpeed ou CrUX, você precisa configurar uma chave de API no arquivo `.env`.
- Páginas com pouco tráfego podem não apresentar dados de campo no CrUX.
- O projeto deve ser utilizado somente em sites próprios ou com autorização.

Também é importante configurar os limites de requisições para não sobrecarregar o servidor do site.

---

## Resumindo

A ideia do Sitemap Auditor é facilitar o trabalho de auditoria técnica de um site.

Você configura o sitemap, define quais páginas deseja verificar, ajusta as regras de SEO e Schema e executa a ferramenta.

Ela faz as verificações automaticamente e entrega os resultados organizados em relatórios e em um dashboard.

O mais interessante é que você pode reutilizar esse projeto em diferentes sites. Basta alterar os arquivos de configuração e as regras necessárias, sem precisar desenvolver uma ferramenta nova do zero.

**Minha recomendação:** comece configurando um site com uma quantidade pequena de URLs. Faça a primeira execução, confira os relatórios e veja se as regras estão de acordo com o que você espera. Depois, aumente a amostra e utilize o dashboard para acompanhar as próximas auditorias.
