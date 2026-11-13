"""Motor declarativo de regras de validação de JSON-LD.

As regras moram em arquivos JSON (um por tipo de página) em
``config/schema_rules/`` e são editáveis sem alterar o código. Este módulo carrega esses arquivos e avalia os
nós do grafo JSON-LD extraídos de uma página.

Formato de um arquivo de regras::

    {
      "page_type": "article",
      "description": "Matéria (NewsArticle)",
      "types": {
        "NewsArticle": {
          "min_count": 1,
          "max_count": 1,
          "required": {
            "headline": "str",
            "datePublished": "str",
            "author": "object_or_list",
            "publisher": "object"
          },
          "iso_dates": ["datePublished", "dateModified"],
          "date_not_before": [["dateModified", "datePublished"]],
          "images_by_dimension": [[1280, 720], [1200, 1200], [960, 1200]]
        },
        "Organization": {"min_count": 1},
        "WebSite": {"min_count": 1},
        "BreadcrumbList": {"min_count": 1}
      },
      "id_consistency": [
        {
          "field_path": "publisher.@id",
          "source_type": "NewsArticle",
          "target_type": "Organization",
          "target_field": "@id",
          "rule": "publisher_organization_same_id",
          "severity": "error"
        }
      ],
      "value_consistency": [
        {
          "field_path": "name",
          "types": ["Organization"],
          "rule": "organization_name_consistent",
          "severity": "warning"
        }
      ]
    }

Os ``types`` validam presença, contagem, tipos de campo, datas ISO 8601, ordem
de datas e contagem de imagens por dimensão. As checagens cruzadas
(``id_consistency`` e ``value_consistency``) cobrem achados como o ``@id`` do publisher divergente e o nome ``ACME!`` vs ``Acme!``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from auditor.models import SchemaFinding

# Tipos de campo reconhecidos nas regras ``required``.
_FIELD_CHECKERS = {
    "str": lambda v: isinstance(v, str) and v.strip() != "",
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, list),
    "object_or_list": lambda v: isinstance(v, (dict, list)),
    "any": lambda v: v is not None,
}


class SchemaRuleError(RuntimeError):
    """Um arquivo de regras é inválido ou não pôde ser carregado."""


@dataclass(frozen=True)
class PageRules:
    page_type: str
    description: str
    types: dict[str, dict[str, Any]]
    id_consistency: tuple[dict[str, Any], ...]
    value_consistency: tuple[dict[str, Any], ...]
    page_url_checks: tuple[dict[str, Any], ...]
    raw_graph_checks: tuple[dict[str, Any], ...]
    duplicate_entity_checks: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RuleSet:
    """Conjunto de regras indexado por tipo de página."""

    by_page_type: dict[str, PageRules] = field(default_factory=dict)

    def for_page_type(self, page_type: str) -> PageRules | None:
        return self.by_page_type.get(page_type)


def load_rules(directory: str | Path) -> RuleSet:
    """Carrega todos os ``*.json`` do diretório de regras.

    Arquivos ocultos e ``.gitkeep`` são ignorados. Cada arquivo precisa definir
    ``page_type`` e um mapa ``types``.
    """
    path = Path(directory)
    by_page_type: dict[str, PageRules] = {}
    if not path.exists():
        return RuleSet(by_page_type)

    for rule_file in sorted(path.glob("*.json")):
        try:
            data = json.loads(rule_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaRuleError(
                f"Arquivo de regras inválido ({rule_file.name}): {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise SchemaRuleError(
                f"Arquivo de regras deve conter um objeto: {rule_file.name}"
            )
        page_type = data.get("page_type")
        if not isinstance(page_type, str) or not page_type:
            raise SchemaRuleError(
                f"Arquivo de regras sem 'page_type' válido: {rule_file.name}"
            )
        types = data.get("types", {})
        if not isinstance(types, dict):
            raise SchemaRuleError(
                f"Campo 'types' deve ser um objeto: {rule_file.name}"
            )
        by_page_type[page_type] = PageRules(
            page_type=page_type,
            description=data.get("description", page_type),
            types=types,
            id_consistency=tuple(data.get("id_consistency", ())),
            value_consistency=tuple(data.get("value_consistency", ())),
            page_url_checks=tuple(data.get("page_url_checks", ())),
            raw_graph_checks=tuple(data.get("raw_graph_checks", ())),
            duplicate_entity_checks=tuple(data.get("duplicate_entity_checks", ())),
        )
    return RuleSet(by_page_type)


def flatten_graph(blocks: list[Any]) -> list[dict[str, Any]]:
    """Achata os blocos JSON-LD em uma lista de nós com ``@type``.

    Abre cada ``@graph`` de nível superior e mantém os nós restantes. Nós sem
    ``@type`` são mantidos (podem ser alvo de checagens específicas).
    """
    nodes: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict) and "@graph" in block:
            graph = block.get("@graph")
            if isinstance(graph, list):
                nodes.extend(n for n in graph if isinstance(n, dict))
            elif isinstance(graph, dict):
                nodes.append(graph)
        elif isinstance(block, dict):
            nodes.append(block)
        elif isinstance(block, list):
            nodes.extend(n for n in block if isinstance(n, dict))
    return nodes


def _node_types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _nodes_of_type(
    nodes: list[dict[str, Any]], schema_type: str
) -> list[dict[str, Any]]:
    return [node for node in nodes if schema_type in _node_types(node)]


def _resolve_path(node: dict[str, Any], path: str) -> Any:
    """Resolve um caminho com pontos (ex.: ``publisher.@id``).

    Em listas, usa o primeiro elemento. Retorna ``None`` se não encontrar.
    """
    current: Any = node
    for part in path.split("."):
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _parse_iso(value: str) -> datetime | None:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _image_dimensions(node: dict[str, Any]) -> list[tuple[int, int]]:
    images = node.get("image")
    if images is None:
        return []
    if isinstance(images, dict):
        images = [images]
    if not isinstance(images, list):
        return []
    dims: list[tuple[int, int]] = []
    for image in images:
        if isinstance(image, dict):
            width = image.get("width")
            height = image.get("height")
            if isinstance(width, (int, float)) and isinstance(
                height, (int, float)
            ):
                dims.append((int(width), int(height)))
    return dims


def _evaluate_type(
    schema_type: str,
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    matches = _nodes_of_type(nodes, schema_type)
    count = len(matches)

    min_count = spec.get("min_count")
    max_count = spec.get("max_count")
    if isinstance(min_count, int) and count < min_count:
        findings.append(
            SchemaFinding(
                schema_type=schema_type,
                rule="min_count",
                field_path="@type",
                expected=f">= {min_count}",
                found=str(count),
            )
        )
    if isinstance(max_count, int) and count > max_count:
        findings.append(
            SchemaFinding(
                schema_type=schema_type,
                rule="max_count",
                field_path="@type",
                expected=f"<= {max_count}",
                found=str(count),
            )
        )

    required = spec.get("required", {})
    required_one_of = spec.get("required_one_of", [])
    iso_dates = spec.get("iso_dates", [])
    date_not_before = spec.get("date_not_before", [])
    images_by_dimension = spec.get("images_by_dimension")

    for node in matches:
        for group in required_one_of:
            if not any(node.get(name) not in (None, "", []) for name in group):
                findings.append(
                    SchemaFinding(
                        schema_type=schema_type,
                        rule="required_one_of",
                        field_path=" | ".join(group),
                        expected="pelo menos um presente",
                        found="nenhum",
                    )
                )
        for field_name, field_type in required.items():
            value = node.get(field_name)
            checker = _FIELD_CHECKERS.get(field_type, _FIELD_CHECKERS["any"])
            if value is None:
                findings.append(
                    SchemaFinding(
                        schema_type=schema_type,
                        rule="required_field_missing",
                        field_path=field_name,
                        expected=f"presente ({field_type})",
                        found="ausente",
                    )
                )
            elif not checker(value):
                findings.append(
                    SchemaFinding(
                        schema_type=schema_type,
                        rule="required_field_type",
                        field_path=field_name,
                        expected=field_type,
                        found=type(value).__name__,
                    )
                )

        for field_name in iso_dates:
            value = node.get(field_name)
            if value is not None and not _is_iso_datetime(value):
                findings.append(
                    SchemaFinding(
                        schema_type=schema_type,
                        rule="iso_date",
                        field_path=field_name,
                        expected="ISO 8601",
                        found=str(value),
                    )
                )

        for later, earlier in date_not_before:
            later_value = node.get(later)
            earlier_value = node.get(earlier)
            if isinstance(later_value, str) and isinstance(earlier_value, str):
                later_dt = _parse_iso(later_value)
                earlier_dt = _parse_iso(earlier_value)
                if (
                    later_dt is not None
                    and earlier_dt is not None
                    and later_dt < earlier_dt
                ):
                    findings.append(
                        SchemaFinding(
                            schema_type=schema_type,
                            rule="date_not_before",
                            field_path=later,
                            expected=f">= {earlier} ({earlier_value})",
                            found=later_value,
                        )
                    )

        if isinstance(images_by_dimension, list):
            present = _image_dimensions(node)
            for dim in images_by_dimension:
                pair = (int(dim[0]), int(dim[1]))
                if pair not in present:
                    findings.append(
                        SchemaFinding(
                            schema_type=schema_type,
                            rule="image_dimension_missing",
                            field_path="image",
                            expected=f"{pair[0]}x{pair[1]}",
                            found=", ".join(f"{w}x{h}" for w, h in present)
                            or "nenhuma",
                        )
                    )

    return findings


def _evaluate_id_consistency(
    check: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[SchemaFinding]:
    source_type = check["source_type"]
    target_type = check["target_type"]
    field_path = check["field_path"]
    target_field = check.get("target_field", "@id")
    rule = check.get("rule", "id_consistency")
    severity = check.get("severity", "error")

    findings: list[SchemaFinding] = []
    sources = _nodes_of_type(nodes, source_type)
    targets = _nodes_of_type(nodes, target_type)
    target_values = {
        t.get(target_field)
        for t in targets
        if isinstance(t.get(target_field), str)
    }
    for source in sources:
        value = _resolve_path(source, field_path)
        if value is None:
            continue
        if value not in target_values:
            findings.append(
                SchemaFinding(
                    schema_type=source_type,
                    rule=rule,
                    field_path=field_path,
                    expected=f"{target_type}.{target_field} "
                    f"({', '.join(sorted(v for v in target_values if v)) or 'nenhum'})",
                    found=str(value),
                    severity=severity,
                )
            )
    return findings


def _evaluate_value_consistency(
    check: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[SchemaFinding]:
    field_path = check["field_path"]
    types = check.get("types", [])
    rule = check.get("rule", "value_consistency")
    severity = check.get("severity", "warning")

    values: list[str] = []
    for schema_type in types:
        for node in _nodes_of_type(nodes, schema_type):
            value = _resolve_path(node, field_path)
            if isinstance(value, str):
                values.append(value)

    unique = sorted(set(values))
    if len(unique) > 1:
        return [
            SchemaFinding(
                schema_type=", ".join(types),
                rule=rule,
                field_path=field_path,
                expected="valor único e consistente",
                found=" | ".join(unique),
                severity=severity,
            )
        ]
    return []


def _normalize_url(value: str) -> str:
    return value.strip().rstrip("/")


def _evaluate_page_url(
    check: dict[str, Any], nodes: list[dict[str, Any]], page_url: str
) -> list[SchemaFinding]:
    """Confere que um campo de um tipo aponta para a URL da página.

    Cobre o achado do vídeo: o ``WebPage`` renderiza o grafo da home, cujo
    ``url``/``@id`` aponta para a home e não para a URL do vídeo.
    """
    schema_type = check["schema_type"]
    field_path = check["field_path"]
    rule = check.get("rule", "page_url_matches")
    severity = check.get("severity", "error")

    findings: list[SchemaFinding] = []
    expected = _normalize_url(page_url)
    for node in _nodes_of_type(nodes, schema_type):
        value = _resolve_path(node, field_path)
        if not isinstance(value, str):
            continue
        # Remove um sufixo de fragmento (#webpage) antes de comparar.
        candidate = _normalize_url(value.split("#", 1)[0])
        if candidate != expected:
            findings.append(
                SchemaFinding(
                    schema_type=schema_type,
                    rule=rule,
                    field_path=field_path,
                    expected=page_url,
                    found=value,
                    severity=severity,
                )
            )
    return findings


def _evaluate_raw_graph(
    check: dict[str, Any], blocks: list[Any]
) -> list[SchemaFinding]:
    """Checagens sobre a forma crua dos blocos, antes do achatamento.

    Cobre os achados da galeria: um nó que deveria ser ``ImageGallery`` sai sem
    ``@type`` no nível do bloco, usa ``id`` em vez de ``@id`` e tem ``@context``
    em ``http://``.
    """
    kind = check.get("kind")
    rule = check.get("rule", "raw_graph")
    severity = check.get("severity", "error")
    findings: list[SchemaFinding] = []

    def walk(node: Any):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    for block in blocks:
        for node in walk(block):
            if kind == "id_without_at" and "id" in node and "@id" not in node:
                findings.append(
                    SchemaFinding(
                        schema_type=node.get("@type", "?") if isinstance(node.get("@type"), str) else "?",
                        rule=rule,
                        field_path="id",
                        expected="@id",
                        found=f"id={node.get('id')}",
                        severity=severity,
                    )
                )
            if kind == "context_http":
                context = node.get("@context")
                if isinstance(context, str) and context.startswith("http://"):
                    findings.append(
                        SchemaFinding(
                            schema_type="@context",
                            rule=rule,
                            field_path="@context",
                            expected="https://schema.org",
                            found=context,
                            severity=severity,
                        )
                    )
    return findings


def _evaluate_duplicate_entity(
    check: dict[str, Any], nodes: list[dict[str, Any]]
) -> list[SchemaFinding]:
    """Avisa quando um tipo aparece mais de uma vez com ``@id`` divergentes.

    Cobre o caso: duas Organizations ``ACME!`` no mesmo grafo, uma com
    ``@id`` ``...example.com#organization`` e outra ``...example.com/#organization``. Para
    dados ligados, são duas entidades distintas — não pode acontecer. Reportado
    como observação (``warning``) por padrão.
    """
    schema_type = check["schema_type"]
    rule = check.get("rule", "duplicate_entity")
    severity = check.get("severity", "warning")

    # Considera também entidades aninhadas (ex.: publisher dentro do
    # NewsArticle), não só os nós de topo do grafo.
    def walk(node: Any):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    ids: list[str] = []
    for root in nodes:
        for node in walk(root):
            if not isinstance(node, dict):
                continue
            if schema_type not in _node_types(node):
                continue
            value = node.get("@id")
            if isinstance(value, str):
                ids.append(value)
    distinct = sorted(set(ids))
    if len(distinct) > 1:
        return [
            SchemaFinding(
                schema_type=schema_type,
                rule=rule,
                field_path="@id",
                expected=f"um único @id para {schema_type}",
                found=" | ".join(distinct),
                severity=severity,
            )
        ]
    return []


def evaluate_page(
    nodes: list[dict[str, Any]],
    rules: PageRules,
    *,
    page_url: str | None = None,
    raw_blocks: list[Any] | None = None,
) -> list[SchemaFinding]:
    """Avalia os nós de uma página contra as regras do seu tipo."""
    findings: list[SchemaFinding] = []
    for schema_type, spec in rules.types.items():
        findings.extend(_evaluate_type(schema_type, spec, nodes))
    for check in rules.id_consistency:
        findings.extend(_evaluate_id_consistency(check, nodes))
    for check in rules.value_consistency:
        findings.extend(_evaluate_value_consistency(check, nodes))
    for check in rules.duplicate_entity_checks:
        findings.extend(_evaluate_duplicate_entity(check, nodes))
    if page_url is not None:
        for check in rules.page_url_checks:
            findings.extend(_evaluate_page_url(check, nodes, page_url))
    if raw_blocks is not None:
        for check in rules.raw_graph_checks:
            findings.extend(_evaluate_raw_graph(check, raw_blocks))
    return findings
