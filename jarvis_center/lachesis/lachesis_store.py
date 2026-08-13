"""
LachesisStore — persistência PostgreSQL para o Lachesis (Jarvis Fates Engine):
tarefas agendadas, relatórios, comportamentos ensinados e Asclepion Systems.
"""

import json
import os
import secrets

import psycopg2
import psycopg2.extras

from lachesis.lachesis_schedule import compute_next_run


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class LachesisStore:

    # ── Tarefas / Relatórios ─────────────────────────────────────────────────

    def list_tasks(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_tasks ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_task(self, task_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_task(self, data: dict) -> dict:
        schedule = data["schedule"]
        next_run = compute_next_run(schedule)
        trigger_config = data.get("trigger_config")
        flow_definition = data.get("flow_definition")
        trigger_type = data.get("trigger_type", "schedule")
        webhook_token = secrets.token_urlsafe(32) if trigger_type == "webhook_in" else None
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO lachesis_tasks (
                    name, instruction, task_type, schedule, webhook_id,
                    enabled, next_run_at, created_by,
                    trigger_type, trigger_config, flow_definition, webhook_token
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    data["name"],
                    data["instruction"],
                    data.get("task_type", "agentless"),
                    json.dumps(schedule),
                    data.get("webhook_id"),
                    data.get("enabled", True),
                    next_run,
                    data.get("created_by"),
                    trigger_type,
                    json.dumps(trigger_config) if trigger_config is not None else None,
                    json.dumps(flow_definition) if flow_definition is not None else None,
                    webhook_token,
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_task(self, task_id: int, data: dict) -> dict | None:
        existing = self.get_task(task_id)
        if not existing:
            return None

        schedule = data.get("schedule", existing["schedule"])
        next_run = existing["next_run_at"]
        if "schedule" in data:
            next_run = compute_next_run(schedule)

        trigger_config = data.get("trigger_config", existing.get("trigger_config"))
        flow_definition = data.get("flow_definition", existing.get("flow_definition"))
        trigger_type = data.get("trigger_type", existing.get("trigger_type", "schedule"))
        webhook_token = existing.get("webhook_token")
        if trigger_type == "webhook_in" and not webhook_token:
            webhook_token = secrets.token_urlsafe(32)

        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE lachesis_tasks
                SET name = %s, instruction = %s, task_type = %s, schedule = %s,
                    webhook_id = %s, enabled = %s, next_run_at = %s, updated_at = NOW(),
                    trigger_type = %s, trigger_config = %s, flow_definition = %s,
                    webhook_token = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    data.get("name", existing["name"]),
                    data.get("instruction", existing["instruction"]),
                    data.get("task_type", existing["task_type"]),
                    json.dumps(schedule),
                    data.get("webhook_id", existing["webhook_id"]),
                    data.get("enabled", existing["enabled"]),
                    next_run,
                    trigger_type,
                    json.dumps(trigger_config) if trigger_config is not None else None,
                    json.dumps(flow_definition) if flow_definition is not None else None,
                    webhook_token,
                    task_id,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def save_flow(self, task_id: int, flow_definition: dict, instruction_summary: str | None = None) -> dict | None:
        """Grava o flow_definition compilado (e opcionalmente o resumo textual) sem tocar no resto da tarefa."""
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if instruction_summary is not None:
                cur.execute(
                    """
                    UPDATE lachesis_tasks
                    SET flow_definition = %s, instruction = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (json.dumps(flow_definition), instruction_summary, task_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE lachesis_tasks
                    SET flow_definition = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (json.dumps(flow_definition), task_id),
                )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_task(self, task_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM lachesis_tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_enabled(self, task_id: int, enabled: bool) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE lachesis_tasks
                SET enabled = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (enabled, task_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_task_by_webhook_token(self, token: str) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_tasks WHERE webhook_token = %s", (token,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def due_tasks(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM lachesis_tasks
                WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= NOW()
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def record_run_start(self, task_id: int) -> int:
        """Cria o registo de execução e limpa next_run_at para evitar dupla-execução."""
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO lachesis_runs (task_id, status) VALUES (%s, 'running') RETURNING id",
                (task_id,),
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE lachesis_tasks SET next_run_at = NULL, last_status = 'running', updated_at = NOW() WHERE id = %s",
                (task_id,),
            )
            return run_id
        finally:
            conn.close()

    def record_run_finish(self, run_id: int, status: str, result_text: str | None,
                           error: str | None, webhook_delivered: bool = False) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE lachesis_runs
                SET finished_at = NOW(), status = %s, result_text = %s,
                    error = %s, webhook_delivered = %s
                WHERE id = %s
                """,
                (status, result_text, error, webhook_delivered, run_id),
            )
        finally:
            conn.close()

    def update_after_run(self, task_id: int, last_status: str, next_run_at) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE lachesis_tasks
                SET last_run_at = NOW(), last_status = %s, next_run_at = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (last_status, next_run_at, task_id),
            )
        finally:
            conn.close()

    def list_runs(self, task_id: int, limit: int = 20) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM lachesis_runs WHERE task_id = %s ORDER BY started_at DESC LIMIT %s",
                (task_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_run(self, run_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Webhooks (lista simples p/ select da UI) ────────────────────────────

    def list_webhooks_brief(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT id, name FROM notification_webhooks WHERE active = TRUE ORDER BY name")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_webhook(self, webhook_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM notification_webhooks WHERE id = %s", (webhook_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Comportamentos ensinados ────────────────────────────────────────────

    def list_behaviors(self, kind: str | None = None) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if kind:
                cur.execute("SELECT * FROM lachesis_behaviors WHERE kind = %s ORDER BY created_at DESC", (kind,))
            else:
                cur.execute("SELECT * FROM lachesis_behaviors ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def list_active_behaviors(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_behaviors WHERE active = TRUE ORDER BY created_at")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def list_infra_facts(self) -> list[dict]:
        """Factos de infraestrutura activos (kind='infra_fact') — Casa do Conhecimento."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM lachesis_behaviors WHERE kind = 'infra_fact' AND active = TRUE "
                "ORDER BY category NULLS LAST, created_at"
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def create_behavior(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO lachesis_behaviors
                    (title, instruction, active, created_by, kind, category, version, source, evidence,
                     scope_type, scope_value)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    data["title"], data["instruction"], data.get("active", True), data.get("created_by"),
                    data.get("kind", "behavior"), data.get("category"), data.get("version"),
                    data.get("source", "manual"), data.get("evidence"),
                    data.get("scope_type", "global"), data.get("scope_value"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_behavior(self, behavior_id: int, data: dict) -> dict | None:
        existing = self._get_behavior(behavior_id)
        if not existing:
            return None
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE lachesis_behaviors
                SET title = %s, instruction = %s, active = %s,
                    kind = %s, category = %s, version = %s, source = %s, evidence = %s,
                    scope_type = %s, scope_value = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (
                    data.get("title", existing["title"]),
                    data.get("instruction", existing["instruction"]),
                    data.get("active", existing["active"]),
                    data.get("kind", existing.get("kind", "behavior")),
                    data.get("category", existing.get("category")),
                    data.get("version", existing.get("version")),
                    data.get("source", existing.get("source", "manual")),
                    data.get("evidence", existing.get("evidence")),
                    data.get("scope_type", existing.get("scope_type", "global")),
                    data.get("scope_value", existing.get("scope_value")),
                    behavior_id,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _get_behavior(self, behavior_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM lachesis_behaviors WHERE id = %s", (behavior_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_behavior(self, behavior_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM lachesis_behaviors WHERE id = %s", (behavior_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_active(self, behavior_id: int, active: bool) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "UPDATE lachesis_behaviors SET active = %s, updated_at = NOW() WHERE id = %s RETURNING *",
                (active, behavior_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Asclepion Systems ────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM asclepion_profiles ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_profile(self, profile_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM asclepion_profiles WHERE id = %s", (profile_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_profile(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO asclepion_profiles (
                    name, targets, os_type, machine_type, benchmark, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    data["name"],
                    data["targets"],
                    data.get("os_type", "windows"),
                    data.get("machine_type", "workstation"),
                    data["benchmark"],
                    data.get("created_by"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_profile(self, profile_id: int, data: dict) -> dict | None:
        existing = self.get_profile(profile_id)
        if not existing:
            return None
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE asclepion_profiles
                SET name = %s, targets = %s, os_type = %s, machine_type = %s,
                    benchmark = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (
                    data.get("name", existing["name"]),
                    data.get("targets", existing["targets"]),
                    data.get("os_type", existing["os_type"]),
                    data.get("machine_type", existing["machine_type"]),
                    data.get("benchmark", existing["benchmark"]),
                    profile_id,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_profile(self, profile_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM asclepion_profiles WHERE id = %s", (profile_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    def set_checklist(self, profile_id: int, checklist: dict, status: str = "ready") -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE asclepion_profiles
                SET checklist = %s, status = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (json.dumps(checklist), status, profile_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_asclepion_run(self, profile_id: int, target: str) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO asclepion_runs (profile_id, target, status)
                VALUES (%s, %s, 'running')
                RETURNING *
                """,
                (profile_id, target),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_asclepion_run(self, run_id: int, status: str, score=None,
                              results=None, summary: str | None = None,
                              error: str | None = None) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE asclepion_runs
                SET status = %s, finished_at = NOW(), score = %s,
                    results = %s, summary = %s, error = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    status, score,
                    json.dumps(results) if results is not None else None,
                    summary, error, run_id,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_asclepion_runs(self, profile_id: int, limit: int = 20) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM asclepion_runs WHERE profile_id = %s ORDER BY started_at DESC LIMIT %s",
                (profile_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_asclepion_run(self, run_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM asclepion_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


_CATEGORY_LABELS = {
    "edr_xdr":              "EDR/XDR",
    "firewall":              "Firewall",
    "siem_agent":            "SIEM / Agentes (Zabbix, Splunk, etc.)",
    "workstation_baseline":  "Baseline de Workstation",
    "application":           "Aplicações",
    "infra_general":         "Infraestrutura Geral",
}


def get_behaviors_prompt_suffix(message_text: str = "") -> str:
    """Bloco a anexar ao JARVIS_SYSTEM com os comportamentos ensinados e o
    conhecimento de infraestrutura activos (Casa do Conhecimento).

    Factos 'global' (ex: EDR padrão) vão sempre — são poucos e aplicam-se a
    toda a infraestrutura. Factos com âmbito 'host'/'target' (ex: "o agentless
    não consegue chegar ao DC-X") só são injectados quando esse host/alvo é
    reconhecido na mensagem actual do utilizador — recuperação por contexto,
    não despejo cego de tudo o que já se aprendeu (isso gastaria tokens à toa
    e não escala à medida que o número de máquinas com lições cresce)."""
    store = LachesisStore()
    behaviors = [b for b in store.list_active_behaviors() if b.get("kind", "behavior") == "behavior"]
    facts = store.list_infra_facts()

    global_facts  = [f for f in facts if f.get("scope_type", "global") == "global"]
    scoped_facts  = [f for f in facts if f.get("scope_type", "global") != "global"]

    out = ""
    if behaviors:
        lines = "\n".join(f"- {b['title']}: {b['instruction']}" for b in behaviors)
        out += f"\n\n## Comportamentos ensinados\n{lines}"

    if global_facts:
        by_category: dict[str, list[dict]] = {}
        for f in global_facts:
            by_category.setdefault(f.get("category") or "infra_general", []).append(f)

        sections = []
        for cat, items in by_category.items():
            label = _CATEGORY_LABELS.get(cat, cat)
            item_lines = "\n".join(
                f"- {f['title']}" + (f" (v{f['version']})" if f.get("version") else "") + f": {f['instruction']}"
                for f in items
            )
            sections.append(f"### {label}\n{item_lines}")

        out += "\n\n## Conhecimento de infraestrutura\n" + "\n\n".join(sections)

    if scoped_facts and message_text:
        text_lower = message_text.lower()
        matches = [f for f in scoped_facts if f.get("scope_value") and f["scope_value"].lower() in text_lower]
        if matches:
            lines = "\n".join(
                f"- [{f['scope_value']}] {f['title']}: {f['instruction']}"
                + (f" (evidência: {f['evidence']})" if f.get("evidence") else "")
                for f in matches
            )
            out += (
                "\n\n## Conhecimento específico desta conversa\n"
                "Já sabes isto sobre máquina(s)/alvo(s) mencionados nesta mensagem — usa antes de "
                "repetir uma investigação já feita:\n" + lines
            )

    return out
