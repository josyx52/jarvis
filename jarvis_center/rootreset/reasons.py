"""
Whitelist de motivos aceites para pedidos de reset root — perfil de chat
"root-only" (ver rootreset/service.py).

Lista oficial de motivos (mesma lista usada no ServiceDesk). O slug de cada
motivo (usado na forma "root_<slug> <host>") é derivado automaticamente do
título — não há duas fontes de verdade para manter sincronizadas. A forma
livre ("$root <texto> <host>") compara o texto normalizado (sem acentos,
sem pontuação, minúsculas) contra o título normalizado.
"""

import os
import time
import unicodedata

REASON_TITLES: list[str] = [
    "INSTALAÇÃO DO ACS SESSION MGR",
    "INSTALAÇÃO DO GLOBAL PROTECT 6.2.8",
    "INSTALAÇÃO DE DRIVER (SOM)",
    "INSTALAÇÃO DO OFFICE 2019",
    "INSTALAÇÃO DO OFFICE 365",
    "MELHORIA DE PERFORMANCE",
    "INSTALAÇÃO DO PORTAL DE BALCÃO",
    "DESINSTALAÇÃO DO PORTAL DE BALCÃO",
    "REPARAÇÃO DO OFFICE 365",
    "REPARAÇÃO DO ONE DRIVE",
    "INSTALAÇÃO DO BLOOMBERG",
    "ACTUALIZAÇÃO DE DRIVERS",
    "DESINSTALAÇÃO DO OFFICE 365",
    "INSTALAÇÃO DO ADOBE SIGN",
    "INSTALAÇÃO DO POWER BI",
    "ACTUALIZAÇÃO DO POWER BI",
    "DESINSTALAÇÃO DO POWER BI",
    "ACTUALIZAÇÕES PENDENTES",
    "PARAMETRIZAÇÃO DE WORKSTATION",
    "CONFIGURAÇÃO DO CISCO IP COMMUNICATOR",
    "INSTALAÇÃO DO SG PONTO",
    "INSTALAÇÃO DO ADOBE ACROBAT",
    "LIMPEZA DOS FICHEIROS TEMPORÁRIOS",
    "DESINSTALAÇÃO DO MS TEAMS",
    "REPARAÇÃO DO KANALO",
    "REPARAÇÃO DO JAVA (PORTAL DE BALCÃO)",
    "INSTALAÇÃO DE CERTIFICADORA",
    "REPARAÇÃO DO OUTLOOK",
    "INSTALAÇÃO DO ALTITUDE",
]


def _normalize(text: str) -> str:
    """minúsculas, sem acentos, só alfanumérico+espaço — para comparação tolerante
    a pontuação, parênteses e pontos (ex: "6.2.8", "(SOM)")."""
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return " ".join(text.split())


def _slugify(text: str) -> str:
    return "_".join(_normalize(text).split())


def slugify_custom(text: str) -> str:
    """Slug para um motivo LIVRE (fora da lista) — prefixo "custom_" para nunca
    colidir com um slug da whitelist, mesmo que o texto normalizado coincida."""
    return "custom_" + _slugify(text)[:80]


# slug -> título oficial (mostrado ao utilizador e gravado como motivo_text)
REASONS: dict[str, str] = {_slugify(title): title for title in REASON_TITLES}

# normalizado -> slug, para o matching de texto livre ($root <texto> <host>)
_BY_NORMALIZED_TITLE: dict[str, str] = {_normalize(title): slug for slug, title in REASONS.items()}


_CUSTOM_CACHE_TTL = 60  # segundos
_custom_cache: dict[str, str] = {}
_custom_cache_at: float = 0.0


def _load_custom_reasons() -> dict[str, str]:
    """Motivos criados pelo admin (Fates → Clotho → "root_motivos",
    tabela root_reset_custom_reasons), além dos oficiais hardcoded acima.
    Cache curta em memória: parser.py chama is_known_slug/match_free_text a
    cada mensagem do chat root-only — uma ida à BD por mensagem seria frágil
    (chat quebra se a BD estiver lenta/em baixo). Falha na BD mantém a
    última cache boa conhecida (ou vazia, se ainda não carregou nenhuma)."""
    global _custom_cache, _custom_cache_at
    now = time.time()
    if now - _custom_cache_at < _CUSTOM_CACHE_TTL:
        return _custom_cache
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            database=os.getenv("POSTGRES_DB", "jarvis"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            connect_timeout=3,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT slug, title FROM root_reset_custom_reasons")
            _custom_cache = dict(cur.fetchall())
            _custom_cache_at = now
        finally:
            conn.close()
    except Exception:
        pass
    return _custom_cache


def all_reasons() -> dict[str, str]:
    """Whitelist oficial (hardcoded, REASON_TITLES) + motivos custom criados
    pelo admin. Usado pelo endpoint /rootreset/reasons (autocomplete "$") e
    pela gestão de auto-aprovação."""
    merged = dict(REASONS)
    merged.update(_load_custom_reasons())
    return merged


def is_known_slug(slug: str) -> bool:
    return slug in all_reasons()


def match_free_text(text: str) -> str | None:
    """Devolve o slug cujo título corresponde ao texto livre, ou None."""
    norm = _normalize(text)
    slug = _BY_NORMALIZED_TITLE.get(norm)
    if slug is not None:
        return slug
    for custom_slug, title in _load_custom_reasons().items():
        if _normalize(title) == norm:
            return custom_slug
    return None


def display_text(slug: str) -> str:
    """Título oficial do motivo, para gravar/mostrar."""
    return all_reasons().get(slug, slug.replace("_", " "))
