"""
Unified LLM client — abstracts Azure AI Foundry (Anthropic) and Ollama.

Foundry  → Anthropic tool_use  (native)
Ollama   → OpenAI-compatible /v1/chat/completions with tool_calls

Both expose the same interface so chat.py is provider-agnostic.
"""

import json
import os
import sys
from dataclasses import dataclass, field

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_config_value


@dataclass
class ToolCall:
    id:   str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text:          str
    tool_calls:    list
    stop_reason:   str    # "end_turn" | "tool_use"
    input_tokens:  int
    output_tokens: int
    _raw:          object = field(default=None, repr=False)


# ── Foundry (Anthropic) ───────────────────────────────────────────────────────

class FoundryLLMClient:

    def __init__(self, model: str):
        import config as _cfg
        from anthropic import AnthropicFoundry
        self.model    = model
        self.provider = "foundry"
        self._client  = AnthropicFoundry(
            api_key  = _cfg.foundry_api_key(),
            base_url = _cfg.foundry_endpoint(),
        )

    def chat(self, messages: list, system: str, tools: list) -> LLMResponse:
        resp = self._client.messages.create(
            model      = self.model,
            system     = system,
            messages   = messages,
            tools      = tools,
            max_tokens = int(get_config_value("llm.max_tokens", 4096)),
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        tcs  = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        ]
        return LLMResponse(
            text          = text,
            tool_calls    = tcs,
            stop_reason   = "tool_use" if resp.stop_reason == "tool_use" else "end_turn",
            input_tokens  = resp.usage.input_tokens,
            output_tokens = resp.usage.output_tokens,
            _raw          = resp.content,
        )

    def append_tool_round(
        self,
        messages: list,
        resp:     LLMResponse,
        results:  list,          # list of (tool_call_id, result_json)
    ):
        messages.append({"role": "assistant", "content": resp._raw})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc_id, "content": content}
            for tc_id, content in results
        ]})


# ── Center Proxy ─────────────────────────────────────────────────────────────

class CenterLLMClient:
    """LLM client que envia pedidos ao endpoint /llm/chat do Jarvis Center."""

    def __init__(self, model: str):
        import config as _cfg
        self.model    = model
        self.provider = "center"
        self._url     = _cfg.center_api_url().rstrip("/") + "/llm/chat"
        self._key     = _cfg.center_api_key()
        self._http    = httpx.Client(timeout=180.0)

    def chat(self, messages: list, system: str, tools: list) -> LLMResponse:
        payload = {
            "model":      self.model,
            "system":     system,
            "messages":   messages,
            "tools":      tools,
            "max_tokens": int(get_config_value("llm.max_tokens", 4096)),
        }
        try:
            resp = self._http.post(
                self._url,
                json=payload,
                headers={"X-API-Key": self._key},
            )
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
            # Ligação keep-alive fechada pelo server — reconectar e tentar uma vez
            self._http = httpx.Client(timeout=180.0)
            resp = self._http.post(
                self._url,
                json=payload,
                headers={"X-API-Key": self._key},
            )
        resp.raise_for_status()
        data = resp.json()

        tcs = [
            ToolCall(id=tc["id"], name=tc["name"], args=tc["args"])
            for tc in data.get("tool_calls", [])
        ]
        # _raw = content blocks serializados — necessário para multi-turn com tool_use
        raw = data.get("content") or (
            [{"type": "text", "text": data["text"]}] if data.get("text") else []
        )
        return LLMResponse(
            text          = data.get("text", ""),
            tool_calls    = tcs,
            stop_reason   = "tool_use" if tcs and data.get("stop_reason") == "tool_use" else "end_turn",
            input_tokens  = data.get("input_tokens", 0),
            output_tokens = data.get("output_tokens", 0),
            _raw          = raw,
        )

    def append_tool_round(self, messages: list, resp: LLMResponse, results: list):
        messages.append({"role": "assistant", "content": resp._raw})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc_id, "content": content}
            for tc_id, content in results
        ]})


# ── Ollama ────────────────────────────────────────────────────────────────────

def _to_openai_tools(anthropic_tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in anthropic_tools
    ]


class OllamaLLMClient:

    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model    = model
        self.base_url = base_url.rstrip("/")
        self.provider = "ollama"
        self._http    = httpx.Client(timeout=120.0)

    def chat(self, messages: list, system: str, tools: list) -> LLMResponse:
        full    = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": full, "stream": False}
        if tools:
            payload["tools"] = _to_openai_tools(tools)

        resp = self._http.post(f"{self.base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()

        data    = resp.json()
        choice  = data["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "stop")

        text = message.get("content") or ""
        tcs  = []
        for tc in (message.get("tool_calls") or []):
            fn   = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:    args = json.loads(args)
                except Exception: args = {}
            tcs.append(ToolCall(
                id   = tc.get("id", f"call_{len(tcs)}"),
                name = fn.get("name", ""),
                args = args,
            ))

        usage = data.get("usage") or {}
        return LLMResponse(
            text          = text,
            tool_calls    = tcs,
            stop_reason   = "tool_use" if finish == "tool_calls" else "end_turn",
            input_tokens  = usage.get("prompt_tokens", 0),
            output_tokens = usage.get("completion_tokens", 0),
            _raw          = message,
        )

    def append_tool_round(
        self,
        messages: list,
        resp:     LLMResponse,
        results:  list,
    ):
        raw = resp._raw
        messages.append({
            "role":       "assistant",
            "content":    raw.get("content"),
            "tool_calls": raw.get("tool_calls") or [],
        })
        for tc, (tc_id, content) in zip(resp.tool_calls, results):
            messages.append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "name":         tc.name,
                "content":      content,
            })


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ollama_models(base_url: str = "http://localhost:11434") -> list:
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=5.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def ollama_running(base_url: str = "http://localhost:11434") -> bool:
    try:
        httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3.0).raise_for_status()
        return True
    except Exception:
        return False


def get_foundry_models(api_key: str | None = None, base_url: str | None = None) -> list[str]:
    """Query available models from the Foundry/Anthropic endpoint."""
    try:
        import config as _cfg
        from anthropic import AnthropicFoundry
        client = AnthropicFoundry(
            api_key  = api_key or _cfg.foundry_api_key(),
            base_url = base_url or _cfg.foundry_endpoint(),
        )
        return [m.id for m in client.models.list()]
    except Exception:
        return []


def foundry_running(api_key: str | None = None, base_url: str | None = None) -> bool:
    return bool(get_foundry_models(api_key, base_url))


def get_client(model: str) -> "FoundryLLMClient | CenterLLMClient | OllamaLLMClient":
    import config as _cfg
    # ini config tem prioridade sobre DB para o provider
    ini_provider = _cfg.llm_provider()
    provider     = ini_provider or get_config_value("llm.provider", "foundry")
    ollama_url   = get_config_value("llm.ollama_url", "http://localhost:11434")
    if provider == "ollama":
        return OllamaLLMClient(model=model, base_url=ollama_url)
    if provider == "center":
        return CenterLLMClient(model=model)
    return FoundryLLMClient(model=model)
