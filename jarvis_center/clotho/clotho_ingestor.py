"""
Clotho Ingestor — cria tools a partir de fontes de informação reais.

Princípio: o utilizador fornece contexto real (cURL, OpenAPI spec, Postman),
o LLM apenas parametriza — nunca inventa endpoints.

Modos:
  parse_curl(curl_text, integration)                  → ~95% fiável
  list_openapi_endpoints(spec)                        → lista disponível
  parse_openapi_endpoint(spec, path, method, integ)   → ~100% fiável
  list_postman_requests(collection)                   → lista disponível
  parse_postman_request(item, integration)            → ~95% fiável
"""

import json
import os
import re
import shlex


# ── LLM ───────────────────────────────────────────────────────────────────────

def _llm(system: str, user_content: str, max_tokens: int = 2000) -> str:
    from anthropic import AnthropicFoundry
    client = AnthropicFoundry(
        api_key=os.getenv("FOUNDRY_API_KEY", ""),
        base_url=os.getenv("FOUNDRY_ENDPOINT", ""),
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=max_tokens,
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    return re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()


def _normalize(tool: dict) -> dict | None:
    from clotho.clotho_tester import _normalize_tool
    return _normalize_tool(tool)


# ══════════════════════════════════════════════════════════════════════════════
# MODO 1 — cURL
# ══════════════════════════════════════════════════════════════════════════════

_CURL_PARAM_SYSTEM = """Você é o Clotho, motor de ingestão do Jarvis Fates Engine.

Recebe um pedido HTTP já parseado (method, url, headers, body) e o contexto da integração.
O seu trabalho é parametrizá-lo para criar uma tool HTTP reutilizável.

REGRAS DE PARAMETRIZAÇÃO:
1. CREDENCIAIS → {{secret}}
   Bearer <token>, Basic <base64>, X-Api-Key: <valor>, apikey=<valor>, password=<valor>.
   Se já tem {{secret}}, deixar como está.

2. VALORES VARIÁVEIS → {{params.nome_descritivo}}
   IDs, UUIDs, emails, hostnames, nomes de entidades, filtros, limites numéricos —
   qualquer valor que claramente é um exemplo e não é um endpoint fixo.

3. TIMESTAMPS → {{now}} (epoch Unix) ou {{now-N}} (N segundos atrás).
   Se a API espera ISO 8601, usar {{params.from_date}} com nota no params_schema.

4. MANTER FIXOS (não parametrizar)
   Paths de endpoints, Content-Type, campos boolean de configuração da chamada.

5. DESCRIPTION: 1 frase em português sobre o que a tool faz.

Responda APENAS com JSON válido sem markdown:
{"description":"...","params_schema":{"nome":"descrição"},"request":{"method":"...","url":"...","headers":{},"body":null}}
"""


def _parse_curl_python(curl_text: str) -> dict:
    """Parseia cURL em componentes usando Python puro, sem LLM."""
    text = re.sub(r"\\\s*\n\s*", " ", curl_text).strip()
    text = re.sub(r"^\$?\s*curl\b\s*", "", text, flags=re.IGNORECASE)

    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    method = "GET"
    url: str | None = None
    headers: dict[str, str] = {}
    body = None
    is_form = False

    i = 0
    while i < len(parts):
        p = parts[i]

        if p in ("-X", "--request") and i + 1 < len(parts):
            method = parts[i + 1].upper()
            i += 2
        elif p in ("-H", "--header") and i + 1 < len(parts):
            hdr = parts[i + 1]
            if ":" in hdr:
                k, v = hdr.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2
        elif p in ("-d", "--data", "--data-raw", "--data-binary") and i + 1 < len(parts):
            raw = parts[i + 1]
            body = None if raw.startswith("@") else raw
            if method == "GET":
                method = "POST"
            i += 2
        elif p == "--data-urlencode" and i + 1 < len(parts):
            val = parts[i + 1]
            body = (body + "&" + val) if body else val
            is_form = True
            if method == "GET":
                method = "POST"
            i += 2
        elif p in ("-F", "--form") and i + 1 < len(parts):
            val = parts[i + 1]
            body = (body + "&" + val) if body else val
            is_form = True
            if method == "GET":
                method = "POST"
            i += 2
        elif p in ("-u", "--user") and i + 1 < len(parts):
            headers["Authorization"] = f"Basic {parts[i + 1]}"
            i += 2
        elif p in ("-G", "--get"):
            method = "GET"
            i += 1
        elif p in ("-L", "--location", "-k", "--insecure", "-s", "--silent",
                   "-v", "--verbose", "-i", "--include", "--compressed", "-n", "--netrc"):
            i += 1
        elif p in ("--connect-timeout", "--max-time", "-m", "--retry",
                   "--retry-delay", "-o", "--output", "-w", "--write-out"):
            i += 2
        elif not p.startswith("-") and url is None:
            url = p.strip("'\"")
            i += 1
        else:
            i += 1

    if body and not is_form and isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            pass

    ct_keys = {k.lower() for k in headers}
    if body is not None and "content-type" not in ct_keys:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if is_form else "application/json"
        )

    return {
        "method": method,
        "url": url or "",
        "headers": headers,
        "body": body if not is_form else (body if isinstance(body, str) else json.dumps(body)),
        "is_form": is_form,
    }


def parse_curl(curl_text: str, integration: dict) -> dict | None:
    """
    Parseia um comando cURL e gera uma tool Clotho parametrizada.
    Python faz o parse estrutural; LLM apenas parametriza sem inventar endpoints.
    """
    parsed = _parse_curl_python(curl_text)
    if not parsed.get("url"):
        return None

    lines = [
        f"Integração: {integration.get('name')} (tipo: {integration.get('type') or 'Desconhecido'})",
        f"Auth type: {integration.get('auth_type') or 'none'}",
        "Pedido HTTP parseado do cURL:",
        f"  method: {parsed['method']}",
        f"  url: {parsed['url']}",
    ]
    if parsed["headers"]:
        lines.append(f"  headers: {json.dumps(parsed['headers'], ensure_ascii=False)}")
    if parsed["body"] is not None:
        body_str = (
            json.dumps(parsed["body"]) if isinstance(parsed["body"], (dict, list))
            else str(parsed["body"])
        )
        lines.append(f"  body: {body_str[:2000]}")
    if parsed["is_form"]:
        lines.append("  encoding: form-urlencoded")

    text = _llm(_CURL_PARAM_SYSTEM, "\n".join(lines))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _normalize(data)


# ══════════════════════════════════════════════════════════════════════════════
# MODO 2 — OpenAPI / Swagger
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_ref(spec: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    obj = spec
    for part in parts:
        obj = obj.get(part.replace("~1", "/").replace("~0", "~"), {})
        if not isinstance(obj, dict):
            return {}
    return obj


def _resolve(spec: dict, obj) -> dict:
    if isinstance(obj, dict) and "$ref" in obj:
        return _resolve_ref(spec, obj["$ref"])
    return obj if isinstance(obj, dict) else {}


def _openapi_base_url(spec: dict, integration: dict) -> str:
    """
    URL base para as tools geradas.
    Prioridade: host configurado na integração > servers da spec.
    O host da integração é o URL real do ambiente — pode diferir do URL público
    na spec (ex: URL interno vs URL de documentação).
    """
    host = (integration.get("host") or "").rstrip("/")
    if host:
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        port = integration.get("port")
        if port and f":{port}" not in host:
            host = f"{host}:{port}"
        return host
    # Fallback: server URL da spec
    for srv in spec.get("servers", []):
        url = (srv if isinstance(srv, dict) else {}).get("url", "")
        if url.startswith("http"):
            return url.rstrip("/")
    # Swagger 2.0
    sw_host = spec.get("host", "")
    if sw_host:
        scheme = (spec.get("schemes") or ["https"])[0]
        base = spec.get("basePath", "/")
        return f"{scheme}://{sw_host}{base}".rstrip("/")
    return ""


def _merged_params(spec: dict, path_item: dict, operation: dict) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for p in path_item.get("parameters", []):
        r = _resolve(spec, p)
        merged[(r.get("name"), r.get("in"))] = r
    for p in operation.get("parameters", []):
        r = _resolve(spec, p)
        merged[(r.get("name"), r.get("in"))] = r
    return list(merged.values())


def _param_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_")


def _schema_example(schema: dict) -> str:
    if "example" in schema:
        return str(schema["example"])
    if "enum" in schema and schema["enum"]:
        return str(schema["enum"][0])
    return {"integer": "10", "boolean": "true", "array": "[]"}.get(schema.get("type", ""), "")


def _build_url(base: str, path: str, params: list[dict]) -> str:
    url = base.rstrip("/") + "/" + path.lstrip("/")
    url = re.sub(
        r"\{([^}]+)\}",
        lambda m: "{{params." + _param_key(m.group(1)) + "}}",
        url,
    )
    qp = [p for p in params if p.get("in") == "query" and p.get("name")]
    if qp:
        url += "?" + "&".join(f"{p['name']}={{{{params.{_param_key(p['name'])}}}}}" for p in qp)
    return url


def _build_params_schema(params: list[dict], spec: dict) -> dict:
    schema: dict[str, str] = {}
    for p in params:
        if p.get("in") not in ("path", "query") or not p.get("name"):
            continue
        k = _param_key(p["name"])
        desc = p.get("description", p["name"]) or p["name"]
        ex = _schema_example(_resolve(spec, p.get("schema", {})))
        if ex:
            desc = f"{desc} (ex: {ex})"
        if p.get("required"):
            desc = f"[obrigatório] {desc}"
        schema[k] = desc[:200]
    return schema


def _body_from_schema(spec: dict, schema: dict, prefix: str = "") -> tuple[object, dict]:
    extra: dict[str, str] = {}
    schema = _resolve(spec, schema)
    stype = schema.get("type", "object")

    if stype == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not props:
            k = prefix or "body"
            extra[k] = "Corpo do pedido (JSON)"
            return f"{{{{params.{k}}}}}", extra
        body: dict = {}
        required = schema.get("required", [])
        for pname, pschema in props.items():
            ps = _resolve(spec, pschema)
            k = _param_key((prefix + "_" + pname).strip("_") if prefix else pname)
            desc = ps.get("description", pname) or pname
            ex = _schema_example(ps)
            if ex:
                desc = f"{desc} (ex: {ex})"
            if pname in required:
                desc = f"[obrigatório] {desc}"
            extra[k] = desc[:200]
            body[pname] = f"{{{{params.{k}}}}}"
        return body, extra

    if stype == "array":
        k = prefix or "items"
        extra[k] = "Array de items"
        return f"{{{{params.{k}}}}}", extra

    k = prefix or "body"
    extra[k] = schema.get("description", k) or k
    return f"{{{{params.{k}}}}}", extra


def _build_body(spec: dict, operation: dict) -> tuple[object, dict]:
    rb = _resolve(spec, operation.get("requestBody", {}))
    if rb:
        content = rb.get("content", {})
        for ct in ("application/json", "application/x-www-form-urlencoded", "multipart/form-data"):
            if ct in content:
                return _body_from_schema(spec, content[ct].get("schema", {}))
        return None, {}
    for p in operation.get("parameters", []):
        rp = _resolve(spec, p)
        if rp.get("in") == "body":
            return _body_from_schema(spec, rp.get("schema", {}))
    return None, {}


def _auth_headers(auth_type: str | None) -> dict:
    if auth_type in ("api_key", "token"):
        return {"Authorization": "Bearer {{secret}}"}
    return {}


_OPENAPI_DESC_SYSTEM = (
    "Escreve 1 frase curta em português (máx 120 caracteres) descrevendo o que faz "
    "este endpoint de API. Responde apenas com a frase, sem JSON nem markdown."
)


def list_openapi_endpoints(spec: dict) -> list[dict]:
    """Lista endpoints disponíveis de uma spec OpenAPI 3.0 / Swagger 2.0."""
    endpoints: list[dict] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            endpoints.append({
                "path": path,
                "method": method.upper(),
                "summary": (op.get("summary") or "")[:200],
                "operation_id": op.get("operationId", ""),
                "tags": op.get("tags", []),
                "deprecated": op.get("deprecated", False),
            })
    return endpoints


def parse_openapi_endpoint(
    spec: dict,
    path: str,
    method: str,
    integration: dict,
) -> dict | None:
    """
    Converte um endpoint OpenAPI numa tool Clotho.
    Estrutura 100% da spec; LLM só escreve a description em português.
    """
    path_item = (spec.get("paths") or {}).get(path)
    if not isinstance(path_item, dict):
        return None
    operation = path_item.get(method.lower())
    if not isinstance(operation, dict):
        return None

    base_url = _openapi_base_url(spec, integration)
    params = _merged_params(spec, path_item, operation)

    url = _build_url(base_url, path, params)
    params_schema = _build_params_schema(params, spec)
    body, body_params = _build_body(spec, operation)
    params_schema.update(body_params)

    headers = _auth_headers(integration.get("auth_type"))
    if body is not None:
        headers.setdefault("Content-Type", "application/json")

    summary = operation.get("summary", "")
    op_desc = (operation.get("description") or "")[:300]
    description = _llm(
        _OPENAPI_DESC_SYSTEM,
        f"{method.upper()} {path}\nSummary: {summary}\nDescription: {op_desc}",
        max_tokens=150,
    )[:300]

    return _normalize({
        "description": description,
        "params_schema": params_schema,
        "request": {"method": method.upper(), "url": url, "headers": headers, "body": body},
    })


def fetch_spec(url: str) -> dict:
    """Faz GET a uma URL de spec OpenAPI e parseia JSON ou YAML."""
    import requests
    import urllib3
    urllib3.disable_warnings()
    try:
        resp = requests.get(url, timeout=30, verify=False)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        raise ValueError(f"Não foi possível obter a spec: {e}") from e

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
        result = yaml.safe_load(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    raise ValueError("Formato não reconhecido (esperado JSON ou YAML OpenAPI)")


def autodiscover_spec(host: str) -> dict | None:
    """Tenta descobrir a spec OpenAPI em paths comuns do host."""
    import requests
    import urllib3
    urllib3.disable_warnings()
    paths = [
        "/openapi.json", "/swagger.json", "/api-docs",
        "/api/v1/openapi.json", "/api/openapi.json",
        "/v1/openapi.json", "/docs/openapi.json",
    ]
    base = host.rstrip("/")
    for p in paths:
        try:
            resp = requests.get(base + p, timeout=5, verify=False)
            if resp.ok:
                try:
                    return resp.json()
                except Exception:
                    pass
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MODO 3 — Postman Collection
# ══════════════════════════════════════════════════════════════════════════════

def _walk_postman(items: list, path: list | None = None) -> list[dict]:
    path = path or []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        cur = path + [name]
        if "request" in item:
            req = item["request"]
            if isinstance(req, dict):
                url_obj = req.get("url", {})
                url_raw = (
                    url_obj if isinstance(url_obj, str)
                    else (url_obj.get("raw", "") if isinstance(url_obj, dict) else "")
                )
                result.append({
                    "path": cur,
                    "name": name,
                    "method": (req.get("method") or "GET").upper(),
                    "url": url_raw[:200],
                })
        if "item" in item:
            result.extend(_walk_postman(item["item"], cur))
    return result


def list_postman_requests(collection: dict) -> list[dict]:
    """Lista requests de uma colecção Postman v2.0 / v2.1."""
    return _walk_postman(collection.get("item", []))


def get_postman_item(collection: dict, item_path: list[str]) -> dict | None:
    """Navega para um request pelo path de nomes na colecção."""
    items = collection.get("item", [])
    node = None
    for seg in item_path:
        node = next((i for i in items if isinstance(i, dict) and i.get("name") == seg), None)
        if not node:
            return None
        items = node.get("item", [])
    return node


def _postman_url(url_obj) -> str:
    if isinstance(url_obj, str):
        return url_obj
    if not isinstance(url_obj, dict):
        return ""
    raw = url_obj.get("raw", "")
    if raw:
        return raw
    host = (
        ".".join(url_obj["host"]) if isinstance(url_obj.get("host"), list)
        else url_obj.get("host", "")
    )
    path = (
        "/" + "/".join(url_obj["path"]) if isinstance(url_obj.get("path"), list)
        else ""
    )
    proto = url_obj.get("protocol", "https")
    qp = url_obj.get("query") or []
    qs = "&".join(
        f"{q['key']}={q.get('value', '')}"
        for q in (qp if isinstance(qp, list) else [])
        if isinstance(q, dict) and "key" in q
    )
    return f"{proto}://{host}{path}" + (f"?{qs}" if qs else "")


_POSTMAN_PARAM_SYSTEM = """Você é o Clotho, motor de ingestão do Jarvis Fates Engine.

Recebe um pedido HTTP extraído de uma colecção Postman e o contexto da integração.
Parametrize-o para criar uma tool HTTP reutilizável.

REGRAS:
1. Variáveis Postman {{var}} → {{params.var}} para variáveis de dados.
   Variáveis de credencial (nomes contendo: token, auth, apikey, key, secret, password, bearer) → {{secret}}.
2. Credenciais hardcoded nos headers/body (Bearer tokens reais, API keys longas) → {{secret}}.
3. Valores hardcoded que são claramente exemplos → {{params.nome_descritivo}}.
4. Manter fixos: paths de endpoints, Content-Type, campos boolean de configuração.
5. DESCRIPTION: 1 frase em português.

Responda APENAS com JSON válido sem markdown:
{"description":"...","params_schema":{"nome":"descrição"},"request":{"method":"...","url":"...","headers":{},"body":null}}
"""


def parse_postman_request(item: dict, integration: dict) -> dict | None:
    """Converte um request Postman numa tool Clotho parametrizada."""
    req = item.get("request", {})
    if not isinstance(req, dict):
        return None

    method = (req.get("method") or "GET").upper()
    url = _postman_url(req.get("url", ""))

    headers: dict[str, str] = {
        h["key"]: h.get("value", "")
        for h in (req.get("header") or [])
        if isinstance(h, dict) and "key" in h and not h.get("disabled")
    }

    body = None
    is_form = False
    body_obj = req.get("body") or {}
    if isinstance(body_obj, dict):
        mode = body_obj.get("mode", "raw")
        if mode == "raw":
            raw = body_obj.get("raw", "")
            if raw:
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    body = raw
        elif mode in ("urlencoded", "formdata"):
            is_form = True
            form_items = body_obj.get("urlencoded") or body_obj.get("formdata") or []
            pairs = [
                f"{fi['key']}={fi.get('value', '')}"
                for fi in (form_items if isinstance(form_items, list) else [])
                if isinstance(fi, dict) and "key" in fi and not fi.get("disabled")
            ]
            body = "&".join(pairs)
        elif mode == "graphql":
            gql = body_obj.get("graphql") or {}
            body = {"query": gql.get("query", ""), "variables": gql.get("variables", {})}

    lines = [
        f"Integração: {integration.get('name')} (tipo: {integration.get('type') or 'Desconhecido'})",
        f"Auth type: {integration.get('auth_type') or 'none'}",
        "Request Postman:",
        f"  method: {method}",
        f"  url: {url}",
    ]
    if headers:
        lines.append(f"  headers: {json.dumps(headers, ensure_ascii=False)}")
    if body is not None:
        body_str = (
            json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        )
        lines.append(f"  body: {body_str[:2000]}")
    if is_form:
        lines.append("  encoding: form-urlencoded")

    text = _llm(_POSTMAN_PARAM_SYSTEM, "\n".join(lines))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _normalize(data)
