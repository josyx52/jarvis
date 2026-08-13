"""
access_requests.service — execução genérica de um "tipo de pedido" definido
pelo admin. A acção é sempre uma tool Clotho já criada e testada no
Laboratório (Fates → Clotho) — nunca código Python novo por tipo de pedido.
Isto é o que permite ao admin criar um tipo de pedido de acesso novo (ex:
desbloqueio de conta AD, reset de senha de outra aplicação) sem deploy,
desde que já exista uma tool Clotho que faça essa acção via HTTP.

Ainda em fase de laboratório: sem parser de chat dedicado (ao contrário do
rootreset, que tem o perfil "root-only") — os pedidos são criados via API
(POST /access-requests/types/{id}/request). Mesmo padrão de aprovação e
entrega automática do rootreset: pending_approval -> processing -> ok/error/
denied, resultado empurrado para o chat de quem pediu via
rootreset/openwebui_push.py (módulo genérico, apesar do nome do pacote).
"""

import json

from access_requests.result_cache import store_result
from access_requests.store import AccessRequestsStore

_store = AccessRequestsStore()


def _call_clotho_tool(integration_name: str, tool_name: str, params: dict) -> dict:
    """Resolve e chama uma tool Clotho — mesmo mecanismo de
    api/chat_engine.py::_call_integration_tool, reaproveitado aqui em vez de
    duplicado (mesmas duas funções: build_tool_request + execute_test_request)."""
    from clotho.clotho_store import ClothoStore
    from clotho.clotho_tester import build_tool_request, execute_test_request

    store = ClothoStore()
    match = next(
        (i for i in store.list_integrations() if i["name"].lower() == integration_name.lower()),
        None,
    )
    if not match:
        return {"ok": False, "error": f"Integração '{integration_name}' não encontrada."}

    integ = store.get_integration(match["id"])
    tool = (integ.get("tools") or {}).get("tools", {}).get(tool_name)
    if not tool:
        return {"ok": False, "error": f"Tool '{tool_name}' não encontrada na integração '{integration_name}'."}

    resolved_request = build_tool_request(tool["request"], params)
    response = execute_test_request(integ, resolved_request, body_encoding=tool.get("body_encoding"))

    if not response.get("ok"):
        return {"ok": False, "error": response.get("error") or f"HTTP {response.get('status_code')}"}

    raw = response.get("body_snippet") or ""
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "A tool não devolveu JSON válido — result_field não pode ser extraído."}

    return {"ok": True, "body": body}


def format_final_message(req: dict, req_type: dict) -> str:
    """Texto mostrado ao utilizador no chat — genérico, não sabe nada sobre
    o mecanismo por trás da tool Clotho (mesma filosofia do rootreset:
    o utilizador só precisa de saber se recebeu o resultado ou não)."""
    target = req["target"]
    if req["status"] == "denied":
        motivo_negado = req.get("error_message") or "sem motivo indicado"
        return f"🚫 **Pedido negado** — {req_type['name']} ({target})\nMotivo do administrador: {motivo_negado}"

    if req["status"] == "error":
        return (
            f"⚠️ **Não foi possível concluir o pedido** — {req_type['name']} ({target})\n"
            f"Já ficou registado e um administrador vai analisar."
        )

    # status == "ok"
    result_field = req_type.get("result_field")
    if not result_field:
        return f"✅ **Pedido aprovado — {req_type['name']} ({target})**"

    from access_requests.result_cache import get_result
    result = get_result(req["id"])
    if result is None:
        return (
            f"✅ O teu pedido ({req_type['name']}, {target}) foi aprovado, mas o resultado já não "
            f"está disponível (expirou). Pede novamente se ainda precisares."
        )

    valor = result.get(result_field)
    return (
        f"✅ **Pedido aprovado — {req_type['name']} ({target})**\n\n"
        f"```json\n{json.dumps({result_field: valor}, ensure_ascii=False, indent=2)}\n```"
    )


def run_approval(request_id: int, approved_by: str) -> None:
    """Corre em thread de fundo (a tool Clotho pode demorar — timeout de 60s
    em execute_test_request). Isolado num try/except que apanha tudo: o
    utilizador tem sempre de receber resposta."""
    from rootreset.openwebui_push import push_message

    req = _store.get(request_id)
    if req is None:
        return
    req_type = _store.get_type(req["request_type_id"])
    if req_type is None:
        updated = _store.finish(request_id, "error", "Tipo de pedido já não existe.", approved_by)
    else:
        status = "error"
        error_message = None
        try:
            params = {req_type["target_param"]: req["target"]}
            result = _call_clotho_tool(req_type["integration_name"], req_type["tool_name"], params)
            if result["ok"]:
                status = "ok"
                store_result(request_id, result["body"])
            else:
                error_message = result.get("error") or "Falha desconhecida na tool Clotho."
        except Exception as e:
            error_message = f"Erro inesperado: {e}"

        updated = _store.finish(request_id, status, error_message, approved_by)

    req_type = req_type or {"name": "Pedido", "result_field": None}
    delivered = push_message(
        updated.get("requester_user_id"), updated.get("chat_id"), updated.get("message_id"),
        format_final_message(updated, req_type),
    )
    if delivered:
        _store.mark_delivered(request_id)
