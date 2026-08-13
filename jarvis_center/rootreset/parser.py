"""
Parsing determinístico (sem LLM) dos comandos do perfil de chat "root-only".

Duas sintaxes suportadas, ambas com o hostname sempre como último token:
  root_<slug> <host>              ex: root_instalacao_kalano wkslptak72
  $root <texto do motivo> <host>  ex: $root instalação do kalano wkslptak72

O motivo não precisa de estar na whitelist (ver reasons.py) — se não
corresponder a nenhum dos 29 motivos oficiais nem ao fallback do DeepSeek,
é aceite na mesma como motivo livre/personalizado (slug "custom_...", fica
registado e visível ao admin em /pedidos tal como qualquer outro pedido).
A whitelist existe para dar sugestões rápidas (autocomplete "$"), não para
bloquear pedidos legítimos fora dela.
"""

from dataclasses import dataclass

from rootreset.reasons import display_text, is_known_slug, match_free_text, slugify_custom


class RootCommandError(Exception):
    """Levantado quando a mensagem claramente tenta ser um comando root mas
    está malformada (falta o hostname, sintaxe incompleta, etc.)."""


@dataclass
class ParsedRootCommand:
    motivo_slug: str
    motivo_text: str
    hostname: str


def parse_root_command(text: str) -> ParsedRootCommand | None:
    """Devolve o comando parseado, None se a mensagem não parece um comando
    root (para o service decidir mostrar a ajuda), ou levanta
    RootCommandError se parece um comando root mas está malformado."""
    stripped = text.strip()
    if not stripped:
        return None

    tokens = stripped.split()
    first = tokens[0]

    if first.lower().startswith("root_"):
        if len(tokens) != 2:
            raise RootCommandError(
                "Formato inválido. Usa: root_<motivo> <hostname> (ex: root_instalacao_kalano wkslptak72)"
            )
        slug = first[len("root_"):].lower()
        hostname = tokens[1]
        if is_known_slug(slug):
            return ParsedRootCommand(motivo_slug=slug, motivo_text=display_text(slug), hostname=hostname)
        motivo_text = slug.replace("_", " ")
        return ParsedRootCommand(motivo_slug=slugify_custom(motivo_text), motivo_text=motivo_text, hostname=hostname)

    if first.lower() in ("$root", "root"):
        if len(tokens) < 3:
            raise RootCommandError(
                "Formato inválido. Usa: $root <motivo> <hostname> (ex: $root instalação do kalano wkslptak72)"
            )
        hostname = tokens[-1]
        motivo_text = " ".join(tokens[1:-1])
        slug = match_free_text(motivo_text)
        if slug is None:
            from rootreset.deepseek_client import classify_motivo_fallback
            slug = classify_motivo_fallback(motivo_text)
        if slug is not None:
            return ParsedRootCommand(motivo_slug=slug, motivo_text=display_text(slug), hostname=hostname)
        # Motivo livre — não está na whitelist, mas é um pedido válido na mesma.
        return ParsedRootCommand(motivo_slug=slugify_custom(motivo_text), motivo_text=motivo_text, hostname=hostname)

    return None
