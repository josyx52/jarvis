"""
Clotho Analyzer — identifica a solução por trás de uma nova integração e
produz uma breve análise, usando o mesmo modelo do Jarvis (sem ferramentas).
"""

import json
import os
import re

_SYSTEM = """Você é o Clotho, o motor de identificação de integrações do Jarvis Fates Engine.

Dado o nome, host, porta, tipo de autenticação e notas fornecidos pelo utilizador sobre uma
nova integração com uma ferramenta/sistema empresarial, identifique a solução mais provável
(ex: Zabbix, Checkpoint, Palo Alto, Dynatrace, Darktrace, CrowdStrike, PostgreSQL, MySQL,
Active Directory, Microsoft Graph, Microsoft 365, etc. — ou outra que reconheça pelos indícios dados) e escreva uma análise
curta (2-3 frases, em português) sobre o que esta integração provavelmente monitoriza ou
disponibiliza, e o que o Jarvis deve ter em conta ao ligar-se a ela.

Se não houver indícios suficientes, indique type="Desconhecido" e explique na análise que
informação adicional seria útil.

Responda APENAS com JSON válido, sem markdown, no formato:
{"type": "<nome curto da solução>", "analysis": "<análise>"}
"""


def analyze_integration(name: str, host: str | None, port: int | None,
                         auth_type: str | None, notes: str | None) -> dict:
    from anthropic import AnthropicFoundry
    client = AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )

    details = [f"Nome: {name}"]
    if host:
        details.append(f"Host: {host}")
    if port:
        details.append(f"Porta: {port}")
    if auth_type:
        details.append(f"Autenticação: {auth_type}")
    if notes:
        details.append(f"Notas: {notes}")

    resp = client.messages.create(
        model      = "claude-sonnet-4-6",
        system     = _SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 512,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
        return {
            "type": str(data.get("type") or "Desconhecido")[:120],
            "analysis": str(data.get("analysis") or "")[:2000],
        }
    except (json.JSONDecodeError, AttributeError):
        return {"type": "Desconhecido", "analysis": text[:2000]}
