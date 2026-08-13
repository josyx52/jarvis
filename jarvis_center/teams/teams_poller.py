"""
Teams Poller — responde a mensagens do Microsoft Teams em tempo real.

Regras:
  1:1  → só responde se o UPN do remetente está na teams_acl (permitted=TRUE)
  grupo → só responde se o chat_id está em teams_chat_permissions (auto ou admin),
           E a mensagem @menciona o bot (campo "mentions" do Graph API)

Watermarks (última mensagem processada por chat) guardados em Redis.
Histórico de conversa reutiliza as mesmas chaves do teams_api.py.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
import redis as _redis_lib

_log = logging.getLogger("jarvis.teams_poller")

POLL_INTERVAL    = 5           # segundos entre ciclos
MAX_CHATS        = 50          # chats activos a verificar por ciclo
MAX_MSG_LOOKBACK = 10          # mensagens a ler por chat
GRAPH_BASE       = "https://graph.microsoft.com/v1.0"
WATERMARK_PREFIX = "jarvis:teams:watermark:"
WATERMARK_TTL    = 7 * 86400   # 7 dias

# Palavras que o utilizador pode usar para confirmar/cancelar uma acção pendente
_CONFIRM_WORDS = {"sim", "s", "yes", "y", "confirmar", "confirmo", "ok",
                  "avança", "avançar", "aprovar", "aprovo", "confirma"}
_CANCEL_WORDS  = {"não", "nao", "n", "no", "cancelar", "cancelo",
                  "negar", "nego", "recusar", "recuso", "cancel"}

_BOT_UPN = os.getenv("JARVIS_TEAMS_BOT_UPN", "jarvis-bot@example.com")

# Cache user_id → UPN para evitar chamadas repetidas ao Graph
_upn_cache: dict[str, str] = {}


# ── Helpers PG ────────────────────────────────────────────────────────────────

def _pg_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        database=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        connect_timeout=5,
    )


def _is_1on1_permitted(upn: str) -> bool:
    """Verifica se o UPN está autorizado na ACL para chats 1:1."""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT permitted FROM teams_acl WHERE LOWER(upn) = LOWER(%s)", (upn,)
        )
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        _log.warning(f"ACL check falhou para {upn}: {e}")
        return False


def _is_group_permitted(chat_id: str) -> bool:
    """Verifica se o grupo tem permissão (auto ou admin)."""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT auto_permitted, admin_permitted FROM teams_chat_permissions WHERE chat_id = %s",
            (chat_id,),
        )
        row = cur.fetchone()
        conn.close()
        return bool(row and (row[0] or row[1]))
    except Exception as e:
        _log.warning(f"Group permission check falhou para {chat_id}: {e}")
        return False


def _upsert_group_chat(chat_id: str, topic: str, chat_type: str, auto: bool, members: list):
    """Regista ou actualiza um chat de grupo na tabela de permissões."""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO teams_chat_permissions
                (chat_id, chat_type, topic, auto_permitted, admin_permitted, members_snapshot, last_seen_at)
            VALUES (%s, %s, %s, %s, FALSE, %s, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
                topic            = EXCLUDED.topic,
                auto_permitted   = EXCLUDED.auto_permitted,
                members_snapshot = EXCLUDED.members_snapshot,
                last_seen_at     = NOW(),
                updated_at       = NOW()
        """, (chat_id, chat_type, topic or "", auto, json.dumps(members)))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.warning(f"Upsert group chat falhou {chat_id}: {e}")


# ── Classe Principal ───────────────────────────────────────────────────────────

class TeamsPoller:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._redis: _redis_lib.Redis | None = None
        self._bot_user_id: str | None = None
        self._token: str | None = None
        self._token_expires: float = 0.0

    # ── Redis ──────────────────────────────────────────────────────────────────

    def _get_redis(self) -> _redis_lib.Redis | None:
        if self._redis is not None:
            try:
                self._redis.ping()
                return self._redis
            except Exception:
                self._redis = None
        try:
            r = _redis_lib.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            r.ping()
            self._redis = r
        except Exception:
            self._redis = None
        return self._redis

    def _get_watermark(self, chat_id: str) -> str | None:
        r = self._get_redis()
        if r is None:
            return None
        try:
            v = r.get(f"{WATERMARK_PREFIX}{chat_id}")
            return v.decode() if v else None
        except Exception:
            return None

    def _set_watermark(self, chat_id: str, dt_str: str):
        r = self._get_redis()
        if r is None:
            return
        try:
            r.setex(f"{WATERMARK_PREFIX}{chat_id}", WATERMARK_TTL, dt_str)
        except Exception:
            pass

    # ── Token ──────────────────────────────────────────────────────────────────

    def _get_token(self) -> str | None:
        now = time.time()
        if self._token and self._token_expires > now + 60:
            return self._token
        try:
            from clotho.clotho_store import ClothoStore
            from clotho.clotho_oauth import acquire_token_ropc
            store = ClothoStore()
            graph = next(
                (i for i in store.list_integrations() if "graph" in i["name"].lower()),
                None,
            )
            if graph is None:
                _log.error("Integração Microsoft Graph não encontrada")
                return None
            cfg = store.get_integration(graph["id"]).get("config", {})
            self._token = acquire_token_ropc(
                tenant_id=cfg["tenant_id"],
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                username=cfg.get("username", _BOT_UPN),
                password=cfg["password"],
            )
            self._token_expires = now + 3300
            return self._token
        except Exception as e:
            _log.error(f"Erro a obter token Graph: {e}")
            return None

    # ── Graph helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        token = self._get_token()
        if not token:
            return None
        try:
            r = httpx.get(
                f"{GRAPH_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=15,
            )
            if r.status_code == 200:
                return r.json()
            _log.debug(f"GET {path} → {r.status_code}: {r.text[:120]}")
        except Exception as e:
            _log.debug(f"GET {path} erro: {e}")
        return None

    def _post(self, path: str, body: dict) -> bool:
        token = self._get_token()
        if not token:
            return False
        try:
            r = httpx.post(
                f"{GRAPH_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=15,
            )
            if r.status_code in (200, 201):
                return True
            _log.warning(f"POST {path} → {r.status_code}: {r.text[:120]}")
        except Exception as e:
            _log.warning(f"POST {path} erro: {e}")
        return False

    # ── Lógica de polling ──────────────────────────────────────────────────────

    def _resolve_upn(self, user_id: str) -> str:
        """Resolve o UPN de um user_id via Graph, com cache em memória."""
        if not user_id:
            return ""
        if user_id in _upn_cache:
            return _upn_cache[user_id]
        data = self._get(f"/users/{user_id}?$select=userPrincipalName,mail")
        upn = ""
        if data:
            upn = (data.get("userPrincipalName") or data.get("mail") or "").lower()
        _upn_cache[user_id] = upn
        return upn

    def _resolve_bot_id(self):
        if self._bot_user_id:
            return
        data = self._get("/me?$select=id,userPrincipalName")
        if data:
            self._bot_user_id = data.get("id")
            _log.info(f"Bot user ID: {self._bot_user_id}")

    def _poll_once(self):
        self._resolve_bot_id()
        if not self._bot_user_id:
            return

        data = self._get(
            "/me/chats",
            params={
                "$top": str(MAX_CHATS),
                "$select": "id,chatType,topic",
            },
        )
        if not data:
            return

        from api.chat_engine import run_jarvis_loop
        from teams.teams_api import _load_history, _save_history

        for chat in data.get("value", []):
            chat_id  = chat["id"]
            ctype    = chat.get("chatType", "oneOnOne")
            topic    = chat.get("topic") or ""

            try:
                self._process_chat(chat_id, ctype, topic, run_jarvis_loop, _load_history, _save_history)
            except Exception as e:
                _log.error(f"Erro a processar chat {chat_id[:20]}: {e}", exc_info=True)

    def _process_chat(self, chat_id, ctype, topic, run_jarvis_loop, _load_history, _save_history):
        watermark = self._get_watermark(chat_id)

        data = self._get(f"/chats/{chat_id}/messages", {
            "$top": str(MAX_MSG_LOOKBACK),
            "$select": "id,from,body,createdDateTime,messageType,deletedDateTime,mentions",
        })
        if data is None:
            return
        if not data:
            return

        # Graph devolve desc (mais recente primeiro) — inverter para processar por ordem
        # Filtro de watermark feito em Python (API não suporta $filter em /chats/messages)
        raw_msgs = [
            m for m in reversed(data.get("value", []))
            if m.get("messageType") == "message"
            and not m.get("deletedDateTime")
            and (m.get("body", {}).get("content") or "").strip()
            and (not watermark or m.get("createdDateTime", "") > watermark)
        ]
        if not raw_msgs:
            return

        # Separar a última mensagem processada antes de filtrar por permissão
        # para actualizar watermark mesmo em chats não permitidos
        latest_dt = raw_msgs[-1].get("createdDateTime")

        # Filtrar mensagens do próprio bot
        # Nota: Graph pode devolver "from": {"user": null} em mensagens de sistema
        # — usar "or {}" para lidar com user=None explícito
        msgs = [
            m for m in raw_msgs
            if ((m.get("from") or {}).get("user") or {}).get("id") != self._bot_user_id
        ]

        for msg in msgs:
            sender       = (msg.get("from") or {}).get("user") or {}
            sender_id    = sender.get("id", "")
            sender_upn   = (sender.get("userPrincipalName") or "").lower()
            if not sender_upn and sender_id:
                sender_upn = self._resolve_upn(sender_id)
            sender_name  = sender.get("displayName", "")
            content      = (msg.get("body", {}).get("content") or "").strip()
            msg_dt       = msg["createdDateTime"]

            is_group = ctype in ("group", "meeting")

            # ── Verificação de permissão ───────────────────────────────────────
            if not is_group:
                # 1:1 — verificar ACL
                if not _is_1on1_permitted(sender_upn):
                    _log.debug(f"1:1 bloqueado (não está na ACL): {sender_upn}")
                    self._set_watermark(chat_id, msg_dt)
                    continue
            else:
                # Grupo — verificar permissão + @mention
                if not _is_group_permitted(chat_id):
                    _log.debug(f"Grupo sem permissão: {chat_id[:20]}")
                    self._set_watermark(chat_id, msg_dt)
                    continue

                mentions = msg.get("mentions", [])
                bot_mentioned = any(
                    m.get("mentioned", {}).get("user", {}).get("id") == self._bot_user_id
                    for m in mentions
                )
                if not bot_mentioned:
                    _log.debug(f"Grupo: mensagem sem @mention do bot, a ignorar")
                    self._set_watermark(chat_id, msg_dt)
                    continue

            # ── Strip HTML básico (Graph devolve HTML no body) ─────────────────
            import re as _re
            clean_content = _re.sub(r"<[^>]+>", " ", content).strip()
            if not clean_content:
                self._set_watermark(chat_id, msg_dt)
                continue

            label = f"[{ctype}] {chat_id[:16]}…"
            _log.info(f"{label} | {sender_name}: {clean_content[:80]}")

            # ── Verificar se é uma confirmação/cancelamento de acção pendente ──
            word_norm = clean_content.strip().lower().rstrip("!.?,")

            from api.chat_engine import pop_teams_pending, execute_tool as _execute_tool
            pending = pop_teams_pending(chat_id)

            if pending is not None and word_norm in _CONFIRM_WORDS:
                _log.info(f"  → Confirmação recebida, a executar {pending['tool']}")
                try:
                    result_json, _ = _execute_tool(pending["tool"], {
                        k: pending[k] for k in pending if k != "tool"
                    })
                    import json as _json_mod
                    result = _json_mod.loads(result_json) if isinstance(result_json, str) else result_json
                    # Formatar resultado resumido para Teams
                    if isinstance(result, dict) and result.get("error"):
                        reply = f"❌ Erro ao executar: {result['error']}"
                    elif isinstance(result, dict) and "results" in result:
                        lines = [f"✅ Acção executada em {len(result.get('results', {}))} máquina(s)."]
                        for host, out in list((result.get("results") or {}).items())[:5]:
                            lines.append(f"**{host}**: {str(out)[:300]}")
                        if result.get("unreachable"):
                            lines.append(f"⚠️ Inacessíveis: {', '.join(result['unreachable'][:5])}")
                        reply = "\n".join(lines)
                    else:
                        out_text = str(result.get("output") or result_json)[:1000]
                        reply = f"✅ Executado.\n```\n{out_text}\n```"
                except Exception as e:
                    _log.error(f"Erro ao executar acção confirmada: {e}", exc_info=True)
                    reply = f"❌ Erro ao executar a acção: {e}"

            elif pending is not None and word_norm in _CANCEL_WORDS:
                _log.info("  → Cancelamento recebido")
                reply = "Acção cancelada."

            elif pending is not None:
                # Resposta ambígua — repõe a acção pendente e pede clareza
                from api.chat_engine import _store_teams_pending
                _store_teams_pending(chat_id, pending)
                reply = "Não percebi. Responde **CONFIRMAR** para executar ou **CANCELAR** para cancelar."

            else:
                # ── Fluxo normal: Jarvis loop ──────────────────────────────────
                history = _load_history(chat_id)
                prefix  = f"[{sender_name}] " if sender_name else ""
                history.append({"role": "user", "content": f"{prefix}{clean_content}"})

                try:
                    reply = run_jarvis_loop(
                        history,
                        max_tokens=4096,
                        has_fates_access=True,
                        channel="teams",
                        chat_id=chat_id,
                    )
                except Exception as e:
                    _log.error(f"Jarvis loop error: {e}")
                    reply = "Desculpa, ocorreu um erro interno. Tenta novamente."

                history.append({"role": "assistant", "content": reply})
                _save_history(chat_id, history)

            ok = self._post(
                f"/chats/{chat_id}/messages",
                {"body": {"content": reply, "contentType": "text"}},
            )
            if ok:
                _log.info(f"  → Resposta enviada ({len(reply)} chars)")
            else:
                _log.warning(f"  → Falhou ao enviar resposta para {chat_id[:20]}")

            self._set_watermark(chat_id, msg_dt)

        # Actualizar watermark para o timestamp mais recente (mesmo sem resposta)
        if latest_dt and not self._get_watermark(chat_id):
            self._set_watermark(chat_id, latest_dt)

    # ── Thread ─────────────────────────────────────────────────────────────────

    def _loop(self):
        _log.info(f"Teams Poller iniciado (interval={POLL_INTERVAL}s)")
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                _log.error(f"Erro no ciclo de polling: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL)
        _log.info("Teams Poller parado")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TeamsPoller")
        self._thread.start()

    def stop(self):
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and (self._thread is not None) and self._thread.is_alive()


# Instância global usada pelo ingestion_api e admin API
_poller = TeamsPoller()


def start_poller():
    _poller.start()


def stop_poller():
    _poller.stop()


def poller_status() -> dict:
    return {
        "running": _poller.is_running,
        "bot_user_id": _poller._bot_user_id,
        "poll_interval_s": POLL_INTERVAL,
    }
