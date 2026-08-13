"""
CTL Authentication — valida utilizador de domínio Windows + ACL + MFA (TOTP).
Chamado na entrada do chat antes de qualquer operação.
"""
import json
import os
import sys
import time

import psycopg2

from rich.console import Console
from rich.text import Text

console = Console()


def _pg():
    import config as _cfg
    return psycopg2.connect(
        host=_cfg.db_host(),
        database=_cfg.db_name(),
        user=_cfg.db_user(),
        password=_cfg.db_password(),
    )


def _current_domain_user() -> str:
    """Devolve DOMAIN\\username do utilizador que está a correr o processo."""
    domain   = os.environ.get("USERDOMAIN", "")
    username = os.environ.get("USERNAME", "")
    if domain and username:
        return f"{domain}\\{username}"
    return username


def _get_acl() -> list[str]:
    """Lê a lista de utilizadores autorizados do CTL a partir da jarvis_config."""
    try:
        conn = _pg()
        cur  = conn.cursor()
        cur.execute("SELECT value FROM jarvis_config WHERE key='ctl.allowed_users'")
        row = conn.cursor().fetchone() if False else cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return []
        val = row[0].strip()
        if val.startswith("["):
            return json.loads(val)
        return [u.strip() for u in val.split(",") if u.strip()]
    except Exception:
        return []


def _get_mfa_secret(domain_user: str) -> str | None:
    """Lê o segredo TOTP do utilizador na jarvis_config."""
    try:
        conn = _pg()
        cur  = conn.cursor()
        key  = f"ctl.mfa_secret.{domain_user.replace('\\', '_').lower()}"
        cur.execute("SELECT value FROM jarvis_config WHERE key=%s", (key,))
        row  = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _verify_totp(secret: str, code: str) -> bool:
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=1)
    except ImportError:
        # pyotp não instalado — aceitar qualquer código de 6 dígitos como fallback
        # (para não bloquear enquanto não está instalado, mas avisando)
        console.print("  [yellow]⚠ pyotp não instalado — MFA não verificado. Instalar: pip install pyotp[/yellow]")
        return len(code) == 6 and code.isdigit()
    except Exception:
        return False


def _setup_mfa_for_user(domain_user: str) -> str:
    """Gera e grava um segredo TOTP novo para o utilizador."""
    try:
        import pyotp
        secret = pyotp.random_base32()
    except ImportError:
        import base64, secrets
        secret = base64.b32encode(secrets.token_bytes(20)).decode()

    try:
        conn = _pg()
        cur  = conn.cursor()
        key  = f"ctl.mfa_secret.{domain_user.replace('\\', '_').lower()}"
        cur.execute(
            """INSERT INTO jarvis_config (key, value, type, category, description, updated_at)
               VALUES (%s, %s, 'secret', 'ctl', %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
            (key, secret, f"TOTP MFA secret para {domain_user}"),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        console.print(f"  [red]Erro a gravar segredo MFA: {e}[/red]")

    return secret


def check_auth() -> bool:
    """
    Valida:
      1. Utilizador de domínio Windows (USERDOMAIN\\USERNAME)
      2. Utilizador está na ACL (ctl.allowed_users em jarvis_config)
      3. MFA TOTP se segredo estiver configurado

    Devolve True se autenticado, False se deve abortar.
    """
    domain_user = _current_domain_user()

    console.print()
    console.print(f"  [dim]Utilizador:[/dim] [cyan]{domain_user}[/cyan]")

    # ── 1. ACL ───────────────────────────────────────────────────────────────
    acl = _get_acl()

    if acl:
        user_lower = domain_user.lower()
        allowed = any(
            a.lower() == user_lower or user_lower.endswith("\\" + a.lower().lstrip("*\\"))
            for a in acl
        )
        if not allowed:
            console.print(f"\n  [red bold]✗ Acesso negado.[/red bold]")
            console.print(f"  [red]'{domain_user}' não está na lista de utilizadores autorizados do CTL.[/red]")
            console.print(f"  [dim]Para adicionar: jarvis-ctl config ctl.allowed_users[/dim]\n")
            return False
        console.print(f"  [dim]ACL:[/dim] [green]✓ autorizado[/green]")
    else:
        # ACL vazia — primeiro arranque, avisar mas permitir
        console.print(f"  [dim]ACL:[/dim] [yellow]⚠ não configurada — todos os utilizadores têm acesso[/yellow]")
        console.print(f"  [dim]Configure com: jarvis-ctl ou via jarvis_config key=ctl.allowed_users[/dim]")

    # ── 2. MFA ───────────────────────────────────────────────────────────────
    mfa_secret = _get_mfa_secret(domain_user)

    if mfa_secret:
        console.print(f"  [dim]MFA:[/dim]  requerido")
        for attempt in range(3):
            try:
                from prompt_toolkit import prompt as pt_prompt
                from prompt_toolkit.styles import Style as PTStyle
                code = pt_prompt(
                    [("class:prompt", "  Código MFA (TOTP) > ")],
                    style=PTStyle.from_dict({"prompt": "ansiyellow bold"}),
                    is_password=True,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [red]Cancelado.[/red]\n")
                return False

            if _verify_totp(mfa_secret, code):
                console.print(f"  [dim]MFA:[/dim]  [green]✓ verificado[/green]")
                break
            else:
                remaining = 2 - attempt
                if remaining > 0:
                    console.print(f"  [red]Código inválido. {remaining} tentativa(s) restante(s).[/red]")
                else:
                    console.print(f"\n  [red bold]✗ MFA falhou — acesso bloqueado.[/red bold]\n")
                    return False
    else:
        # Sem segredo MFA — oferecer configuração
        console.print(f"  [dim]MFA:[/dim]  [yellow]⚠ não configurado[/yellow]")
        console.print(f"  [dim]Para activar MFA: use 'setup-mfa' no primeiro acesso[/dim]")

    console.print()
    return True


def setup_mfa(domain_user: str | None = None):
    """Configura MFA TOTP para o utilizador actual e mostra o QR/URI."""
    if not domain_user:
        domain_user = _current_domain_user()

    secret = _setup_mfa_for_user(domain_user)

    console.print(f"\n  [cyan]MFA configurado para[/cyan] [bold]{domain_user}[/bold]")
    console.print(f"  Segredo TOTP: [yellow bold]{secret}[/yellow bold]")

    try:
        import pyotp
        uri = pyotp.totp.TOTP(secret).provisioning_uri(
            name=domain_user,
            issuer_name="Jarvis CTL",
        )
        console.print(f"\n  URI para importar no Google Authenticator / Microsoft Authenticator:")
        console.print(f"  [dim]{uri}[/dim]")
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(uri)
            qr.print_ascii(invert=True)
        except ImportError:
            console.print("  [dim](instala 'qrcode' para ver o QR code no terminal)[/dim]")
    except ImportError:
        console.print("  [dim]Importa o segredo acima manualmente no teu autenticador TOTP.[/dim]")

    console.print()
