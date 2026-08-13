"""
Clotho — catálogo de integrações/tools/bases de dados e menções $nome$ para o Jarvis.
"""

import re

from clotho.clotho_store import ClothoStore
from clotho.clotho_database_store import ClothoDatabaseStore
from clotho.clotho_database_engine import slugify_name

_MENTION_PATTERN = re.compile(r"\$([A-Za-z0-9_.\-]+)\$")


def describe_integrations_for_prompt() -> str:
    """Bloco de prompt listando as integrações Clotho configuradas e as suas tools,
    para o Jarvis saber o que pode invocar via call_integration_tool."""
    store = ClothoStore()
    configured = [i for i in store.list_integrations() if i.get("status") == "configured"]
    if not configured:
        return ""

    lines = ["\n\n## Integrações configuradas (Clotho)"]
    for integ in configured:
        full = store.get_integration(integ["id"]) or {}
        catalog = full.get("tools") or {}
        tools = catalog.get("tools") or {}
        profile = catalog.get("connection_profile") or {}
        if tools:
            tool_descs = ", ".join(
                f"{name} ({(tool.get('description') or '').rstrip('.')})"
                for name, tool in tools.items()
            )
            lines.append(f"- {integ['name']}: {tool_descs}")
            lines.append(f'  (usa call_integration_tool com integration_name="{integ["name"]}")')
            guide = profile.get("search_guide")
            if guide:
                lines.append(f"  GUIA DE PESQUISA: {guide}")
        else:
            lines.append(f"- {integ['name']}: sem tools geradas ainda")
    return "\n".join(lines)


def _database_description(db: dict) -> str:
    schema = db.get("schema_cache") or {}
    tables = schema.get("tables") or {}
    if not tables:
        return "schema ainda não introspectado"
    parts = []
    for table_name, info in tables.items():
        cols = ", ".join(f"{c['name']} ({c['type']})" for c in info.get("columns") or [])
        count = info.get("row_count_estimate")
        suffix = f", ~{count} linhas" if count is not None else ""
        parts.append(f"{table_name}: colunas: {cols}{suffix}")
    return " | ".join(parts)


def get_mentions_catalog() -> list[dict]:
    """Catálogo de referências $nome$: integrações, tools de cada integração e
    bases de dados geridas pelo Clotho — usado para autocomplete e para a
    expansão de contexto de menções numa mensagem."""
    items: list[dict] = []

    store = ClothoStore()
    for integ in store.list_integrations():
        if integ.get("status") != "configured":
            continue
        description = integ.get("analysis") or integ.get("notes") or ""
        items.append({
            "id": integ["name"],
            "label": integ["name"],
            "kind": "integration",
            "description": description,
        })

        full = store.get_integration(integ["id"]) or {}
        tools = ((full.get("tools") or {}).get("tools") or {})
        for tool_name, tool in tools.items():
            items.append({
                "id": f"{integ['name']}.{tool_name}",
                "label": f"{integ['name']} · {tool_name}",
                "kind": "tool",
                "description": tool.get("description") or "",
                "params_schema": tool.get("params_schema"),
            })

    db_store = ClothoDatabaseStore()
    for db in db_store.list_databases():
        if db.get("status") != "ready":
            continue
        slug = slugify_name(db.get("table_name") or db["name"])
        items.append({
            "id": f"db.{slug}",
            "label": f"db · {db['name']}",
            "kind": "database",
            "description": _database_description(db),
        })

    return items


def mentions_context_block(text: str) -> str:
    """Expande referências $nome$ encontradas em `text` num bloco de contexto
    para o prompt, resolvendo-as no catálogo do Clotho."""
    found = _MENTION_PATTERN.findall(text or "")
    if not found:
        return ""

    seen = []
    for name in found:
        if name not in seen:
            seen.append(name)

    catalog = {item["id"]: item for item in get_mentions_catalog()}
    catalog_ci = {k.lower(): v for k, v in catalog.items()}

    lines = ["\n\n## Referências $$ nesta mensagem"]
    for name in seen:
        item = catalog.get(name) or catalog_ci.get(name.lower())
        if not item:
            lines.append(f"- {name}: não encontrado no catálogo")
            continue

        if item["kind"] == "integration":
            lines.append(f"- {item['id']} (integração configurada no Clotho): {item['description']}")
            lines.append(f'  (usa call_integration_tool com integration_name="{item["id"]}")')
        elif item["kind"] == "tool":
            integ_name, tool_name = item["id"].split(".", 1)
            lines.append(f'- {item["id"]} (tool da integração "{integ_name}"): "{item["description"]}"')
            if item.get("params_schema"):
                lines.append(f"  params: {item['params_schema']}")
            lines.append(f'  (usa call_integration_tool com integration_name="{integ_name}", tool_name="{tool_name}")')
        elif item["kind"] == "database":
            db_name = item["id"].split(".", 1)[1]
            lines.append(f"- {item['id']} (tabela gerida pelo Clotho): {item['description']}")
            lines.append(f'  -> usa query_database(database_name="{db_name}", sql="SELECT ...")')

    return "\n".join(lines)