"""
Cliente para o DeepSeek-V4-Flash exposto no recurso Azure "RootJarvisProfile"
(endpoint compatível com OpenAI v1).

Âmbito estrito: só classifica qual dos 29 motivos autorizados corresponde a
um texto livre, quando o parser determinístico (rootreset/reasons.py) não
encontra correspondência exacta. Nunca recebe "tools"/ferramentas, nunca
decide sobre reset de senha, WinRM ou qualquer execução — isso mantém-se
inteiramente em rootreset/client.py e rootreset/service.py. Se não estiver
configurado ou falhar por qualquer motivo, devolve None silenciosamente e o
chamador cai no erro determinístico normal (fail-safe, não fail-open).
"""

import json
import os

import requests

from rootreset.reasons import REASON_TITLES, match_free_text

_ENDPOINT = os.getenv("DEEPSEEK_ENDPOINT", "").rstrip("/")
_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
_MODEL = os.getenv("DEEPSEEK_MODEL", "DeepSeek-V4-Flash")
_TIMEOUT = 15

_SYSTEM = (
    "Escolhes, de uma lista fechada de motivos, qual corresponde ao texto do "
    "utilizador. Responde APENAS com um objecto JSON: "
    '{"motivo": "<título exacto da lista>"} ou {"motivo": null} se nenhum '
    "corresponder claramente. Nunca inventes um motivo fora da lista."
)


def classify_motivo_fallback(free_text: str) -> str | None:
    """Devolve o slug do motivo mais próximo ao texto livre, ou None se não
    houver correspondência clara, se o DeepSeek não estiver configurado, ou
    se a chamada falhar por qualquer razão (timeout, HTTP, JSON inválido)."""
    if not _ENDPOINT or not _API_KEY:
        return None

    catalog = "\n".join(f"- {t}" for t in REASON_TITLES)
    user_content = f'Motivos válidos:\n{catalog}\n\nTexto do utilizador: "{free_text}"'

    try:
        resp = requests.post(
            f"{_ENDPOINT}/chat/completions",
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 100,
                "temperature": 0,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()

        parsed = json.loads(content)
        title = parsed.get("motivo")
        if not title:
            return None
        return match_free_text(title)
    except Exception:
        return None
