"""
Clotho Tester — propõe um pedido HTTP de teste para uma integração, executa-o
contra o sistema real e resume o resultado, usando o mesmo modelo do Jarvis.
"""

import json
import os
import re

import requests
import urllib3

from clotho.clotho_oauth import acquire_token, acquire_token_ropc, acquire_token_generic
from clotho.clotho_session_auth import acquire_session_token, clear_cache as clear_session_cache

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_PREPARE_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

Dado o tipo de solução já identificado, host, porta, tipo de autenticação e a análise prévia de
uma integração, proponha UM pedido HTTP simples (de preferência GET) para um endpoint típico de
health/status/versão/API dessa solução, que sirva para testar a ligação.

REGRA ABSOLUTA — construção do URL:
- Se o campo "Host" começar com "http://" ou "https://", use-o DIRECTAMENTE como prefixo (não altere nada).
- Caso contrário, use "https://<host>" como base.
- A porta no URL SÓ deve ser incluída se o campo "Porta" foi explicitamente fornecido. Se não há porta, NÃO adicione nenhuma porta — nem a porta padrão da solução (proibido assumir 8089 para Splunk, 9200 para Elasticsearch, 5601 para Kibana, etc.).

Para o cabeçalho de autenticação:
- se auth_type for "api_key" ou "token", inclua um cabeçalho típico da solução com o placeholder
  literal "{{secret}}" no valor (ex: "Authorization": "Bearer {{secret}}");
- se auth_type for "oauth2_client_credentials" ou "session_login", NÃO inclua cabeçalhos de
  autenticação — o token (OAuth2 ou de sessão) é injectado automaticamente pelo sistema antes de
  cada pedido;
- se auth_type for "basic" ou "none", não inclua cabeçalhos de autenticação (Basic Auth é aplicado
  automaticamente).

Responda APENAS com JSON válido, sem markdown, no formato:
{"method": "GET", "url": "<url completo do endpoint de teste>", "headers": {}, "body": null, "description": "<1 frase sobre o que este pedido verifica>"}
"""

_EVALUATE_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

Avalie o resultado de uma tentativa de teste de uma integração. Analise o pedido HTTP efetuado e
a resposta obtida (ou erro de ligação) e determine:

1. Se a tentativa foi bem-sucedida — considere não só o código HTTP, mas também o conteúdo da
   resposta (ex: um HTTP 200 com um erro JSON-RPC/JSON no corpo NÃO é sucesso).
2. Se não foi bem-sucedida, se o problema é corrigível ajustando o próprio pedido (URL, método,
   cabeçalhos ou corpo) — por exemplo, corpo em falta ou malformado, endpoint incorreto, cabeçalho
   ou content-type errado. NÃO proponha correções para problemas que não se resolvem ajustando o
   pedido (ex: credenciais inválidas, serviço em baixo, host inacessível, DNS, certificado).

Use o "Corpo efetivamente enviado no pedido (wire)" — quando presente — como a fonte de verdade
sobre o que foi transmitido, em vez de assumir o formato a partir do pedido original. Compare-o
com a mensagem de erro da resposta para diagnosticar a causa real (ex: se o corpo na wire aparece
como uma string JSON escapada dentro de outra string, em vez de um objeto JSON, corrija para
enviar um objeto JSON válido). Baseie a correção em evidências observadas, não em suposições.

Responda APENAS com JSON válido, sem markdown, no formato:
{"success": true|false, "note": "<1 frase curta sobre esta tentativa>", "fixed_request": {"method": "...", "url": "...", "headers": {}, "body": null} ou null}

Ao propor fixed_request, mantenha os cabeçalhos de autenticação existentes (incluindo o placeholder
literal "{{secret}}" se presente) e ajuste apenas o que for necessário para corrigir o problema.
"""

_TOOLS_CATALOG_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

Dado o tipo de solução já identificado, host, porta, tipo de autenticação e a análise prévia de uma
integração, gere um CATÁLOGO de "tools" (operações) que o Jarvis (Lachesis) poderá invocar para
consultar esta integração — focando em operações de leitura úteis para monitorização/diagnóstico
de infraestrutura (ex: listar hosts/dispositivos, listar problemas/alertas ativos, estado geral,
versão da API), seguindo as convenções reais da API desta solução.

REGRA ABSOLUTA — construção dos URLs:
- Se o campo "Host" começar com "http://" ou "https://", use-o DIRECTAMENTE como prefixo de todos os endpoints (não altere nada).
- Caso contrário, use "https://<host>" como base.
- A porta SÓ deve aparecer no URL se o campo "Porta" foi explicitamente fornecido. Se não há porta, NÃO adicione nenhuma porta — nem a porta padrão da solução (proibido assumir 8089 para Splunk, 9200 para Elasticsearch, etc.).

Para cada tool, defina um pedido HTTP completo (method, url, headers, body) pronto a executar. Use
o placeholder literal "{{secret}}" em qualquer cabeçalho ou campo do corpo onde for necessário
incluir a credencial. Para parâmetros que variam por chamada (ex: filtros, limites de resultados),
use o placeholder "{{params.<nome>}}" dentro do body/url e declare-os em "params_schema".

Para timestamps relativos ao momento da execução, use "{{now}}" (epoch Unix actual) ou
"{{now-N}}" (epoch actual menos N segundos, ex: "{{now-86400}}" = últimas 24h). Estes são
resolvidos automaticamente — NÃO invente strings como "__epoch_now_minus_86400__".

Tenha atenção a particularidades da API: algumas operações (ex: info/versão) podem não aceitar
cabeçalhos de autenticação, enquanto operações de dados normalmente requerem.

IMPORTANTE sobre autenticação nos cabeçalhos das tools:
- Se auth_type for "oauth2_client_credentials" ou "session_login", NÃO inclua cabeçalhos de
  autenticação — o token (OAuth2 ou de sessão) é injectado automaticamente pelo sistema. Basta
  definir o URL e method corretos.
- Se auth_type for "api_key" ou "token", use o placeholder "{{secret}}" nos cabeçalhos.

Responda APENAS com JSON válido, sem markdown, no formato:
{
  "connection_profile": {"notes": "<1 frase sobre o padrão de autenticação usado por esta API>"},
  "tools": {
    "<tool_name>": {
      "description": "<1 frase sobre o que esta tool faz>",
      "params_schema": {"<param>": "<descrição>"},
      "request": {"method": "...", "url": "...", "headers": {}, "body": null}
    }
  }
}
Gere entre 3 e 6 tools relevantes para monitorização/diagnóstico."""

_TOOL_FROM_DESCRIPTION_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

O utilizador conhece uma funcionalidade específica da API desta integração e descreveu-a em texto
livre. Dado o tipo de solução já identificado, host, porta, tipo de autenticação, análise prévia, e
essa descrição, gere UMA tool (operação) correspondente, com um pedido HTTP completo pronto a
executar, seguindo as convenções reais da API desta solução.

REGRA ABSOLUTA — se o campo "Host" começar com "http://" ou "https://", use-o como URL base directamente. Caso contrário, use "https://<host>". A porta SÓ aparece no URL se foi explicitamente fornecida no campo "Porta" — nunca assuma portas padrão da solução.

Use o placeholder literal "{{secret}}" em qualquer cabeçalho ou campo do corpo onde for necessário
incluir a credencial — EXCEPTO se auth_type for "oauth2_client_credentials" ou "session_login",
casos em que NÃO deve incluir cabeçalhos de autenticação (o token OAuth2 ou de sessão é injectado
automaticamente pelo sistema).

Para parâmetros que variam por chamada, use o placeholder
"{{params.<nome>}}" dentro do body/url e declare-os em "params_schema".

Para timestamps relativos ao momento da execução, use "{{now}}" (epoch Unix actual) ou
"{{now-N}}" (epoch actual menos N segundos, ex: "{{now-86400}}" = últimas 24h).

Responda APENAS com JSON válido, sem markdown, no formato:
{
  "description": "<1 frase sobre o que esta tool faz>",
  "params_schema": {"<param>": "<descrição>"},
  "request": {"method": "...", "url": "...", "headers": {}, "body": null}
}"""

_EDIT_TOOL_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

O utilizador quer modificar uma tool existente com base numa descrição em linguagem natural.
Dado o contexto da integração e a definição actual da tool, aplique as alterações descritas
e devolva a tool actualizada completa.

Preserve todos os campos não mencionados. Preserve os placeholders {{secret}}, {{params.<nome>}},
{{now}} e {{now-N}}. Não altere o nome da tool.

Responda APENAS com JSON válido, sem markdown, no formato:
{
  "description": "<1 frase sobre o que esta tool faz>",
  "params_schema": {"<param>": "<descrição>"},
  "request": {"method": "...", "url": "...", "headers": {}, "body": null}
}"""

_FINAL_SUMMARY_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

Dado o histórico de tentativas de teste de uma integração (incluindo eventuais correções
automáticas feitas ao pedido entre tentativas), escreva um resumo curto (2-4 frases, em português)
sobre o resultado final: se a integração está operacional, o que foi corrigido automaticamente
(se aplicável), e em caso de falha final o que o utilizador deve verificar manualmente.

Responda APENAS com texto simples, sem markdown."""


def _replace_secret(value, secret: str):
    """Substitui {{secret}} recursivamente em qualquer estrutura (str/dict/list)."""
    if isinstance(value, str):
        return value.replace("{{secret}}", secret)
    if isinstance(value, dict):
        return {k: _replace_secret(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_secret(v, secret) for v in value]
    return value


def _replace_username(value, username: str):
    """Substitui {{username}} recursivamente em qualquer estrutura (str/dict/list)."""
    if isinstance(value, str):
        return value.replace("{{username}}", username)
    if isinstance(value, dict):
        return {k: _replace_username(v, username) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_username(v, username) for v in value]
    return value


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def _norm_host(host: str | None) -> str | None:
    """Normaliza barras invertidas em URLs introduzidas pelo utilizador."""
    if not host:
        return host
    # Converte https:\\ ou http:\\ → https:// ou http://
    return re.sub(r'^(https?):[\\/]+', lambda m: m.group(1) + '://', host.strip())


def prepare_test_request(integration: dict) -> dict:
    details = [
        f"Nome: {integration['name']}",
        f"Tipo identificado: {integration.get('type') or 'Desconhecido'}",
    ]
    if integration.get("host"):
        details.append(f"Host: {_norm_host(integration['host'])}")
    if integration.get("port"):
        details.append(f"Porta: {integration['port']}")
    if integration.get("auth_type"):
        details.append(f"Tipo de autenticação: {integration['auth_type']}")
    if integration.get("analysis"):
        details.append(f"Análise prévia: {integration['analysis']}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _PREPARE_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 512,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    return {
        "method": str(data.get("method") or "GET").upper()[:10],
        "url": str(data.get("url") or "")[:500],
        "headers": data.get("headers") if isinstance(data.get("headers"), dict) else {},
        "body": data.get("body"),
        "description": str(data.get("description") or "")[:500],
    }


def execute_test_request(integration: dict, request_spec: dict, body_encoding: str | None = None) -> dict:
    method = str(request_spec.get("method") or "GET").upper()
    url = str(request_spec.get("url") or "")
    headers = dict(request_spec.get("headers") or {})
    body = request_spec.get("body")

    if body_encoding is None:
        body_encoding = _detect_body_encoding(headers, body)

    is_form_encoded = (body_encoding == "form")
    if isinstance(body, str) and not is_form_encoded:
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass

    config = integration.get("config") or {}
    auth = None
    if integration.get("auth_type") == "oauth2_client_credentials":
        try:
            client_id = config.get("client_id", "")
            client_secret = config.get("client_secret", "")
            scope = config.get("scope", "")
            tenant_id = config.get("tenant_id", "")
            token_url = config.get("token_url", "")

            if config.get("grant_type") == "password":
                # ROPC (delegated permissions) só existe no Microsoft Entra.
                token = acquire_token_ropc(
                    tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
                    username=config.get("username", ""), password=config.get("password", ""),
                    scope=scope or "https://graph.microsoft.com/.default",
                )
            elif token_url:
                # OAuth2 Client Credentials genérico (RFC 6749) — qualquer API
                # que não seja Microsoft Entra (ex: CrowdStrike, Okta, Auth0).
                token = acquire_token_generic(token_url, client_id, client_secret, scope)
            elif tenant_id:
                # Compatibilidade com integrações Microsoft Entra existentes
                # (sem token_url configurado) — via MSAL.
                token = acquire_token(tenant_id, client_id, client_secret,
                                       scope or "https://graph.microsoft.com/.default")
            else:
                raise RuntimeError(
                    "Falta 'token_url' na configuração (ou 'tenant_id' se for Microsoft Entra)."
                )
            headers["Authorization"] = f"Bearer {token}"
        except (RuntimeError, requests.RequestException) as e:
            return {"status_code": None, "headers": {}, "body_snippet": "", "ok": False,
                    "error": str(e)[:500]}
    elif integration.get("auth_type") == "basic":
        auth = (config.get("username", ""), config.get("password", ""))
    elif integration.get("auth_type") == "session_login":
        login_body = _replace_username(
            _replace_secret(config.get("login_body") or {}, config.get("secret", "")),
            config.get("username", ""),
        )
        session_header = config.get("session_header") or "X-chkp-sid"
        try:
            token = acquire_session_token(
                integration_id=integration.get("id"),
                login_url=config.get("login_url", ""),
                login_body=login_body,
                session_field=config.get("session_field") or "sid",
                ttl_seconds=int(config.get("ttl_seconds") or 540),
                method=config.get("login_method") or "POST",
            )
            headers[session_header] = token
        except Exception as e:
            return {"status_code": None, "headers": {}, "body_snippet": "", "ok": False,
                    "error": f"Falha no login de sessão: {e}"[:500]}
    else:
        secret = config.get("secret", "")
        headers = {k: v.replace("{{secret}}", secret) if isinstance(v, str) else v
                   for k, v in headers.items()}
        body = _replace_secret(body, secret)

    if not url:
        return {"status_code": None, "headers": {}, "body_snippet": "", "ok": False,
                "error": "Pedido sem URL."}

    try:
        req_kwargs = {}
        if body and is_form_encoded:
            req_kwargs["data"] = body
        elif body:
            req_kwargs["json"] = body

        resp = requests.request(
            method, url,
            headers=headers,
            **req_kwargs,
            auth=auth,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 401 and integration.get("auth_type") == "session_login":
            clear_session_cache(integration.get("id"))
            try:
                token = acquire_session_token(
                    integration_id=integration.get("id"),
                    login_url=config.get("login_url", ""),
                    login_body=login_body,
                    session_field=config.get("session_field") or "sid",
                    ttl_seconds=int(config.get("ttl_seconds") or 540),
                    method=config.get("login_method") or "POST",
                    force=True,
                )
                headers[config.get("session_header") or "X-chkp-sid"] = token
                resp = requests.request(
                    method, url,
                    headers=headers,
                    **req_kwargs,
                    auth=auth,
                    timeout=60,
                    verify=False,
                )
            except Exception:
                pass
        sent_body = resp.request.body
        if isinstance(sent_body, bytes):
            sent_body = sent_body.decode("utf-8", errors="replace")
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body_snippet": resp.text[:200000],
            "sent_body": sent_body[:2000] if sent_body else None,
            "ok": resp.ok,
            "error": None,
        }
    except requests.RequestException as e:
        return {"status_code": None, "headers": {}, "body_snippet": "", "sent_body": None,
                "ok": False, "error": str(e)[:500]}


def evaluate_test_attempt(integration: dict, request_spec: dict, response: dict) -> dict:
    details = [
        f"Integração: {integration['name']} (tipo: {integration.get('type') or 'Desconhecido'})",
        f"Pedido: {request_spec.get('method')} {request_spec.get('url')}",
    ]
    if request_spec.get("headers"):
        details.append(f"Cabeçalhos do pedido: {json.dumps(request_spec['headers'])}")
    if request_spec.get("body") is not None:
        details.append(f"Corpo do pedido: {json.dumps(request_spec['body'])}")
    if response.get("error"):
        details.append(f"Erro de ligação: {response['error']}")
    else:
        details.append(f"Status HTTP: {response.get('status_code')}")
        if response.get("sent_body"):
            details.append(f"Corpo efetivamente enviado no pedido (wire): {response['sent_body']}")
        if response.get("body_snippet"):
            details.append(f"Resposta (excerto): {response['body_snippet'][:1000]}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _EVALUATE_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 600,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    fixed = data.get("fixed_request")
    fixed_request = None
    if isinstance(fixed, dict) and fixed.get("url"):
        fixed_request = {
            "method": str(fixed.get("method") or "GET").upper()[:10],
            "url": str(fixed.get("url") or "")[:500],
            "headers": fixed.get("headers") if isinstance(fixed.get("headers"), dict) else {},
            "body": fixed.get("body"),
            "description": request_spec.get("description", ""),
        }

    return {
        "success": bool(data.get("success", False)),
        "note": str(data.get("note") or "")[:500],
        "fixed_request": fixed_request,
    }


def summarize_test_attempts(integration: dict, attempts: list[dict], success: bool) -> str:
    details = [
        f"Integração: {integration['name']} (tipo: {integration.get('type') or 'Desconhecido'})",
        f"Resultado final: {'sucesso' if success else 'falha'}",
        f"Número de tentativas: {len(attempts)}",
    ]
    for i, attempt in enumerate(attempts, 1):
        req = attempt["request"]
        resp = attempt["response"]
        line = f"Tentativa {i}: {req.get('method')} {req.get('url')} → "
        if resp.get("error"):
            line += f"erro de ligação ({resp['error']})"
        else:
            line += f"HTTP {resp.get('status_code')}"
        line += f" — {attempt.get('note', '')}"
        details.append(line)

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _FINAL_SUMMARY_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 400,
    )

    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()[:1000]


def generate_tools_catalog(integration: dict) -> dict:
    details = [
        f"Nome: {integration['name']}",
        f"Tipo identificado: {integration.get('type') or 'Desconhecido'}",
    ]
    if integration.get("host"):
        details.append(f"Host: {_norm_host(integration['host'])}")
    if integration.get("port"):
        details.append(f"Porta: {integration['port']}")
    if integration.get("auth_type"):
        details.append(f"Tipo de autenticação: {integration['auth_type']}")
    if integration.get("analysis"):
        details.append(f"Análise prévia: {integration['analysis']}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _TOOLS_CATALOG_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 4000,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    tools = {}
    for name, tool in (data.get("tools") or {}).items():
        normalized = _normalize_tool(tool)
        if normalized:
            tools[str(name)[:100]] = normalized

    connection_profile = data.get("connection_profile")
    return {
        "connection_profile": connection_profile if isinstance(connection_profile, dict) else {},
        "tools": tools,
    }


def _detect_body_encoding(headers: dict, body) -> str | None:
    if body is None:
        return None
    ct = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type" and isinstance(v, str):
            ct = v.lower()
            break
    if "x-www-form-urlencoded" in ct:
        return "form"
    if "json" in ct:
        return "json"
    if isinstance(body, str) and "=" in body and "&" in body and not body.strip().startswith("{"):
        return "form"
    return "json"


def _normalize_tool(tool: dict) -> dict | None:
    if not isinstance(tool, dict):
        return None
    request = tool.get("request") or {}
    if not isinstance(request, dict) or not request.get("url"):
        return None
    headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
    body = request.get("body")
    return {
        "description": str(tool.get("description") or "")[:300],
        "params_schema": tool.get("params_schema") if isinstance(tool.get("params_schema"), dict) else {},
        "body_encoding": _detect_body_encoding(headers, body),
        "request": {
            "method": str(request.get("method") or "GET").upper()[:10],
            "url": str(request.get("url") or "")[:500],
            "headers": headers,
            "body": body,
        },
    }


def edit_tool_from_description(integration: dict, existing_tool: dict, description: str) -> dict | None:
    """Actualiza uma tool existente com base numa descrição em linguagem natural."""
    details = [
        f"Nome: {integration['name']}",
        f"Tipo identificado: {integration.get('type') or 'Desconhecido'}",
    ]
    if integration.get("host"):
        details.append(f"Host: {_norm_host(integration['host'])}")
    if integration.get("auth_type"):
        details.append(f"Tipo de autenticação: {integration['auth_type']}")
    if integration.get("analysis"):
        details.append(f"Análise prévia: {integration['analysis']}")
    details.append(f"Definição actual da tool:\n{json.dumps(existing_tool, ensure_ascii=False, indent=2)}")
    details.append(f"Alteração pedida pelo utilizador: {description}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _EDIT_TOOL_SYSTEM,
        messages   = [{"role": "user", "content": "\n\n".join(details)}],
        max_tokens = 2000,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    return _normalize_tool(data)


def generate_tool_from_description(integration: dict, description: str) -> dict | None:
    details = [
        f"Nome: {integration['name']}",
        f"Tipo identificado: {integration.get('type') or 'Desconhecido'}",
    ]
    if integration.get("host"):
        details.append(f"Host: {_norm_host(integration['host'])}")
    if integration.get("port"):
        details.append(f"Porta: {integration['port']}")
    if integration.get("auth_type"):
        details.append(f"Tipo de autenticação: {integration['auth_type']}")
    if integration.get("analysis"):
        details.append(f"Análise prévia: {integration['analysis']}")
    # Pass existing auth header pattern so the LLM replicates it exactly
    existing_tools = (integration.get("tools") or {}).get("tools") or {}
    for existing_tool in existing_tools.values():
        req_headers = (existing_tool.get("request") or {}).get("headers") or {}
        if req_headers:
            details.append(
                f"Cabeçalhos de autenticação já usados por outras tools desta integração "
                f"(copia exatamente este padrão): {json.dumps(req_headers)}"
            )
            break
    details.append(f"Funcionalidade descrita pelo utilizador: {description}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _TOOL_FROM_DESCRIPTION_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 1500,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    return _normalize_tool(data)


_PARAM_PLACEHOLDER    = re.compile(r"^\{\{params\.(\w+)\}\}$")
_NOW_PLACEHOLDER      = re.compile(r"^\{\{now(?:-(\d+))?\}\}$")
_NOW_ISO_PLACEHOLDER  = re.compile(r"^\{\{now_iso(?:\+(\d+))?\}\}$")
_INLINE_NOW           = re.compile(r"\{\{now(?:-(\d+))?\}\}")
_INLINE_NOW_ISO       = re.compile(r"\{\{now_iso(?:\+(\d+))?\}\}")
_INLINE_PARAM         = re.compile(r"\{\{params\.(\w+)\}\}")
_FORM_PARAM_PAIR      = re.compile(r"[&]?[\w.\[\]-]+=\{\{params\.\w+\}\}")
_OMIT = object()


def build_tool_request(tool_request: dict, params: dict | None = None) -> dict:
    """Substitui placeholders "{{params.<nome>}}" pelos valores fornecidos.

    Quando o valor de um campo é exatamente um placeholder, é substituído pelo
    valor com o seu tipo original (ex: lista para "groupids"). Se o parâmetro
    não for fornecido, o campo é omitido — assim parâmetros opcionais não
    enviados não deixam placeholders literais (strings) em campos que a API
    espera como outro tipo (ex: array).

    Para bodies form-encoded (strings com key=value&...), pares cujo valor é
    um placeholder não fornecido são removidos inteiramente.

    Placeholders de tempo:
      {{now}}        → Unix timestamp (inteiro)
      {{now-N}}      → Unix timestamp menos N segundos
      {{now_iso}}    → datetime actual em ISO 8601 UTC (para APIs como Graph Calendar)
      {{now_iso+N}}  → datetime actual + N segundos em ISO 8601 UTC
    """
    import time as _time
    import datetime as _dt
    params = params or {}

    def _iso(offset_s: int = 0) -> str:
        t = _dt.datetime.utcnow() + _dt.timedelta(seconds=offset_s)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def substitute(value):
        if isinstance(value, str):
            # Valor exacto {{now_iso}} ou {{now_iso+N}}
            now_iso_match = _NOW_ISO_PLACEHOLDER.match(value)
            if now_iso_match:
                offset = int(now_iso_match.group(1) or 0)
                return _iso(offset)
            # Valor exacto {{now}} ou {{now-N}}
            now_match = _NOW_PLACEHOLDER.match(value)
            if now_match:
                offset = int(now_match.group(1) or 0)
                return int(_time.time()) - offset
            # Valor inteiro é {{params.X}}
            match = _PARAM_PLACEHOLDER.match(value)
            if match:
                return params.get(match.group(1), _OMIT)
            # Substituir {{now_iso}} e {{now_iso+N}} inline
            value = _INLINE_NOW_ISO.sub(
                lambda m: _iso(int(m.group(1) or 0)), value
            )
            # Substituir {{now}} e {{now-N}} inline
            value = _INLINE_NOW.sub(
                lambda m: str(int(_time.time()) - int(m.group(1) or 0)), value
            )
            # Substituir {{params.X}} inline (quando o param foi fornecido)
            for key, val in params.items():
                value = value.replace("{{params." + str(key) + "}}", str(val))
            # Limpar placeholders não substituídos em form-encoded bodies ou
            # query strings de URLs. O charset da chave em _FORM_PARAM_PAIR
            # exclui '/', ':' e '?' de propósito — com [^&=]+ (qualquer coisa
            # exceto & e =) o regex conseguia recuar até ao início do URL
            # (scheme+host+path) sempre que o primeiro parâmetro da query
            # string ficasse por resolver, apagando o URL inteiro.
            if _INLINE_PARAM.search(value) and "=" in value:
                value = _FORM_PARAM_PAIR.sub("", value)
                value = re.sub(r"&{2,}", "&", value)
                value = value.replace("?&", "?")
                value = value.strip("&")
            return value
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                sv = substitute(v)
                if sv is not _OMIT:
                    result[k] = sv
            return result
        if isinstance(value, list):
            return [sv for sv in (substitute(v) for v in value) if sv is not _OMIT]
        return value

    return substitute(tool_request)
