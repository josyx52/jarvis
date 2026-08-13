"""
FoundryClient — cliente LLM para Claude Sonnet 4.6 via Microsoft Azure AI Foundry.

Usa o SDK oficial da Anthropic (AnthropicFoundry) que suporta autenticacao
por API Key contra o endpoint Azure configurado no projecto Azure AI Foundry configurado.

Interface identica ao OllamaClient para substituicao transparente.
"""

import os

from anthropic import AnthropicFoundry


FOUNDRY_BASE_URL = os.getenv(
    "FOUNDRY_ENDPOINT",
    "https://YOUR-RESOURCE.openai.azure.com/anthropic"
)
FOUNDRY_API_KEY  = os.getenv("FOUNDRY_API_KEY", "")
FOUNDRY_MODEL    = "claude-sonnet-4-6"


class FoundryClient:

    def __init__(
        self,
        model: str = FOUNDRY_MODEL,
        base_url: str = FOUNDRY_BASE_URL,
        api_key: str = FOUNDRY_API_KEY,
    ):
        self.model = model
        self._client = AnthropicFoundry(
            api_key=api_key,
            base_url=base_url,
        )

    def generate(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """
        Interface compativel com OllamaClient.generate().
        Recebe um prompt em texto e devolve a resposta como string.
        """
        message = self._client.messages.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=180.0,
        )

        return message.content[0].text

    def health_check(self) -> bool:
        """Verifica se o endpoint esta acessivel."""
        try:
            r = self.generate("OK", max_tokens=10)
            return bool(r)
        except Exception:
            return False
