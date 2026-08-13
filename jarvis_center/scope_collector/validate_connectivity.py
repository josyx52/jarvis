"""
Validação de acessos — Zabbix e Splunk.

Corre este script NUM HOST COM ACESSO À REDE INTERNA (ex: o próprio servidor
do Jarvis Center) — este sandbox de desenvolvimento não tem rota de rede para
a infra interna do banco, por isso a validação real tem de acontecer aí.

Nunca cole credenciais reais na conversa com o Jarvis — define-as como
variáveis de ambiente antes de correr o script:

  Zabbix:
    ZBX_URL          = http://<host>:<porta>          (sem /api_jsonrpc.php)
    ZBX_AUTH_TOKEN   = <token de API>                 (Zabbix >= 5.4, recomendado)
    ZBX_USER / ZBX_PASSWORD                            (alternativa, user.login)
    ZBX_TEST_HOST    = <nome exacto do host Zabbix a testar>

  Splunk:
    SPLUNK_URL       = https://<host>:8089            (porta de gestão REST)
    SPLUNK_TOKEN     = <token de autenticação>        (Settings -> Tokens, recomendado)
    SPLUNK_USER / SPLUNK_PASSWORD                       (alternativa, Basic auth)

Uso:
    python validate_connectivity.py zabbix
    python validate_connectivity.py splunk
    python validate_connectivity.py all
"""

import os
import sys


def _ok(msg: str):
    print(f"  [OK] {msg}")


def _fail(msg: str):
    print(f"  [FALHA] {msg}")


def validate_zabbix() -> bool:
    print("\n== Zabbix ==")
    url = os.getenv("ZBX_URL")
    if not url:
        _fail("ZBX_URL não definida")
        return False

    from scope_collector.zabbix_client import ZabbixClient, ZabbixError

    client = ZabbixClient(
        base_url=url,
        auth_token=os.getenv("ZBX_AUTH_TOKEN"),
        user=os.getenv("ZBX_USER"),
        password=os.getenv("ZBX_PASSWORD"),
    )

    try:
        version = client.get_api_version()
        _ok(f"API acessível — versão {version}")
    except Exception as e:
        _fail(f"não foi possível contactar {url}/api_jsonrpc.php — {e}")
        return False

    if not client.has_credentials():
        print("  [aviso] ZBX_AUTH_TOKEN / ZBX_USER+ZBX_PASSWORD não definidos — a saltar teste de autenticação")
    else:
        try:
            client.ensure_authenticated()
            _ok("autenticação bem-sucedida")
        except ZabbixError as e:
            _fail(f"autenticação falhou — {e}")
            return False

    test_host = os.getenv("ZBX_TEST_HOST")
    if test_host and client.has_credentials():
        try:
            host_id = client.get_host_id(test_host)
            if host_id:
                _ok(f"host '{test_host}' encontrado (hostid={host_id}) — utilizador tem permissão de leitura")
            else:
                _fail(f"host '{test_host}' não encontrado ou sem permissão de leitura para este utilizador")
                return False
        except Exception as e:
            _fail(f"host.get falhou — {e}")
            return False

        try:
            problems = client.get_active_problems([host_id])
            _ok(f"problem.get acessível — {len(problems)} problema(s) activo(s) em '{test_host}'")
        except Exception as e:
            _fail(f"problem.get falhou — {e}")
            return False
    else:
        print("  [aviso] ZBX_TEST_HOST não definido — a saltar teste de leitura de host/items/problems")

    return True


def validate_splunk() -> bool:
    print("\n== Splunk ==")
    url = os.getenv("SPLUNK_URL")
    if not url:
        _fail("SPLUNK_URL não definida")
        return False

    from scope_collector.splunk_client import SplunkClient, SplunkError

    client = SplunkClient(
        base_url=url,
        token=os.getenv("SPLUNK_TOKEN"),
        user=os.getenv("SPLUNK_USER"),
        password=os.getenv("SPLUNK_PASSWORD"),
    )

    try:
        indexes = client.list_indexes()
        _ok(f"API acessível e autenticada — {len(indexes)} índice(s) visível(eis) para este utilizador/token")
        if indexes:
            print(f"       índices: {', '.join(indexes[:10])}{' ...' if len(indexes) > 10 else ''}")
        else:
            _fail("nenhum índice visível — verificar 'srchIndexesAllowed' do role associado ao token/utilizador")
            return False
    except SplunkError as e:
        _fail(f"não foi possível listar índices em {url} — {e}")
        return False
    except Exception as e:
        _fail(f"erro de ligação a {url} — {e}")
        return False

    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    results = {}
    if target in ("zabbix", "all"):
        results["zabbix"] = validate_zabbix()
    if target in ("splunk", "all"):
        results["splunk"] = validate_splunk()

    print("\n== Resumo ==")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    sys.exit(0 if all(results.values()) else 1)
