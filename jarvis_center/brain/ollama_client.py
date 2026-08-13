import requests


class OllamaClient:

    def __init__(
        self,
        model="gpt-oss:120b-cloud",
        base_url="http://localhost:11434"
    ):
        self.model = model
        self.base_url = base_url
        self.generate_url = f"{base_url}/api/generate"

    def generate(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 8000
    ) -> str:

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens
            }
        }

        response = requests.post(
            self.generate_url,
            json=payload,
            timeout=120
        )

        response.raise_for_status()

        data = response.json()

        return data["response"]