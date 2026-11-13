"""Modelos dos resultados de auditoria."""

from dataclasses import dataclass, field
from typing import Literal

HTTPCheckOutcome = Literal[
    "success",
    "redirect",
    "http_error",
    "other_http_status",
    "timeout",
    "connection_error",
    "request_error",
]


@dataclass(frozen=True)
class HTTPStatusResult:
    original_url: str
    status_code: int | None
    final_url: str | None
    elapsed_ms: float | None
    redirect_count: int | None
    redirect_statuses: tuple[int, ...]
    outcome: HTTPCheckOutcome
    error_message: str | None
    origins: tuple[str, ...]
    page_type: str

    @property
    def is_down(self) -> bool:
        """``fora_do_ar``: status 4xx/5xx, erro de rede ou timeout."""
        return self.outcome in {
            "http_error",
            "timeout",
            "connection_error",
            "request_error",
        }

    @property
    def is_sitemap_redirect(self) -> bool:
        """Redirect numa URL listada no sitemap é alerta."""
        return self.outcome == "redirect"


SEOSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class SEOFinding:
    """Um achado de SEO ligado a uma regra específica.

    ``severity`` é ``error`` para problemas objetivos (ausência de title ou
    description, noindex, canonical divergente, H1 múltiplo/ausente, URL do
    sitemap diferente da final) e ``warning`` para recomendações sem regra
    oficial do Google (tamanho de title/description fora da faixa, OG ausente).
    """

    rule: str
    severity: SEOSeverity
    message: str


@dataclass(frozen=True)
class SEOResult:
    url: str
    final_url: str | None
    page_type: str
    origins: tuple[str, ...]
    title: str | None
    title_length: int | None
    meta_description: str | None
    meta_description_length: int | None
    canonical: str | None
    has_noindex: bool
    h1_count: int
    open_graph_tags: tuple[str, ...]
    findings: tuple[SEOFinding, ...] = field(default_factory=tuple)
    error_message: str | None = None

    @property
    def seo_ok(self) -> bool:
        """True quando não há achados de severidade ``error``."""
        if self.error_message is not None:
            return False
        return not any(f.severity == "error" for f in self.findings)

    @property
    def problems(self) -> tuple[str, ...]:
        """Mensagens dos achados, para a coluna ``seo_problemas`` do CSV."""
        return tuple(f.message for f in self.findings)


SchemaSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class SchemaFinding:
    """Um achado de schema. Os campos espelham ``schema_detalhe.csv``.

    ``severity`` é ``error`` para problemas objetivos (campo obrigatório
    ausente, tipo errado, contagem errada, @type ausente) e ``warning`` para
    inconsistências toleráveis (ex.: ``ACME!`` vs ``Acme!``).
    """

    schema_type: str  # tipo_schema
    rule: str  # regra
    field_path: str  # caminho_do_campo
    expected: str  # esperado
    found: str  # encontrado
    severity: SchemaSeverity = "error"


@dataclass(frozen=True)
class SchemaResult:
    url: str
    page_type: str
    origins: tuple[str, ...]
    detected_types: tuple[str, ...]
    findings: tuple[SchemaFinding, ...] = field(default_factory=tuple)
    error_message: str | None = None

    @property
    def schema_ok(self) -> bool:
        """True quando não há achados de severidade ``error``."""
        if self.error_message is not None:
            return False
        return not any(f.severity == "error" for f in self.findings)

    @property
    def errors(self) -> tuple[SchemaFinding, ...]:
        """Só os achados que reprovam a URL (severidade ``error``)."""
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[SchemaFinding, ...]:
        """Observações que não reprovam a URL (severidade ``warning``)."""
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def problems(self) -> tuple[str, ...]:
        """Resumo textual só dos erros, para a coluna ``schema_problemas``."""
        return tuple(
            f"{f.schema_type}.{f.field_path}: {f.rule}" for f in self.errors
        )

    @property
    def observations(self) -> tuple[str, ...]:
        """Resumo textual dos avisos, para a coluna ``schema_observacoes``."""
        return tuple(
            f"{f.schema_type}.{f.field_path}: {f.rule}" for f in self.warnings
        )


CWVStrategy = Literal["mobile", "desktop"]
# Fonte do dado de cada métrica: campo (dados reais de usuários via CrUX) ou
# laboratório (Lighthouse), usado quando não há dado de campo.
CWVSource = Literal["field", "lab"]
# Faixas do Google: bom / precisa melhorar / ruim.
CWVBand = Literal["bom", "precisa_melhorar", "ruim", "sem_dado"]


@dataclass(frozen=True)
class CoreWebVitalsResult:
    url: str
    page_type: str
    origins: tuple[str, ...]
    strategy: CWVStrategy
    lcp_ms: float | None
    inp_ms: float | None
    cls: float | None
    tbt_ms: float | None
    # Fonte de LCP/INP/CLS (TBT é sempre laboratório).
    source: CWVSource | None
    # Faixa por métrica (bom/precisa_melhorar/ruim/sem_dado).
    lcp_band: CWVBand
    inp_band: CWVBand
    cls_band: CWVBand
    error_message: str | None = None

    @property
    def is_poor(self) -> bool:
        """``cwv_ruim``: qualquer métrica na faixa ruim."""
        return "ruim" in (self.lcp_band, self.inp_band, self.cls_band)

    @property
    def worst_band(self) -> CWVBand:
        """Pior faixa entre as métricas, para a coluna ``cwv_faixa``."""
        order = {"ruim": 3, "precisa_melhorar": 2, "bom": 1, "sem_dado": 0}
        bands = (self.lcp_band, self.inp_band, self.cls_band)
        return max(bands, key=lambda b: order[b])
