#!/usr/bin/env python3
"""
lachesis_recompile_flows.py — Recompila automações do Lachesis para deixarem
de usar IA a cada execução.

Elegível para recompilação:
  - tarefas sem flow_definition (caem sempre em run_jarvis_loop, IA a cada
    corrida); ou
  - tarefas com flow_definition que já contém pelo menos um nó "ai_decision"
    (pode ser possível decompor em passos deterministas).

Em ambos os casos a acção é a mesma: recompilar a instrução original com
compile_instruction_to_flow e substituir o flow_definition gravado. O
compilador decide sozinho, nó a nó, onde é preciso julgamento IA — pedidos
genuinamente ambíguos continuam a ter ai_decision depois de recompilados,
isso é esperado.

Uso:
  python lachesis_recompile_flows.py                        (dry-run — só reporta)
  python lachesis_recompile_flows.py --apply                 (recompila e grava)
  python lachesis_recompile_flows.py --ids 1486,1484,1483    (restringe a estas tarefas)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# A codepage da consola Windows (cp1252/cp850) não sabe imprimir alguns
# caracteres usados nos avisos do compilador (ex: "→") — sem isto, um print()
# lança UnicodeEncodeError a meio do try/except e a tarefa fica marcada como
# falhada mesmo que a compilação e a gravação tenham corrido bem.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Carregar .env se existir (antes de ler os.getenv) — mesmo padrão de start_center.py
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        with open(_env_file, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, BASE_DIR)

from lachesis.lachesis_store import LachesisStore
from lachesis.lachesis_flow_llm import compile_instruction_to_flow

_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lachesis_flow_backups")


def _ai_decision_count(flow_definition: dict | None) -> int:
    if not flow_definition:
        return 0
    return sum(1 for n in flow_definition.get("nodes", []) if n.get("type") == "ai_decision")


def _needs_recompile(task: dict) -> bool:
    flow_definition = task.get("flow_definition")
    return flow_definition is None or _ai_decision_count(flow_definition) > 0


def _state_label(task: dict) -> str:
    flow_definition = task.get("flow_definition")
    if flow_definition is None:
        return "sem flow"
    count = _ai_decision_count(flow_definition)
    return f"{count} nó(s) ai_decision"


def _write_backup(tasks: list[dict]) -> str:
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_BACKUP_DIR, f"backup_{timestamp}.json")
    snapshot = [
        {
            "id": t["id"],
            "name": t["name"],
            "instruction": t["instruction"],
            "task_type": t.get("task_type"),
            "flow_definition": t.get("flow_definition"),
            "trigger_config": t.get("trigger_config"),
        }
        for t in tasks
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return path


def main():
    parser = argparse.ArgumentParser(description="Recompila automações Lachesis para deixarem de usar IA a cada execução.")
    parser.add_argument("--apply", action="store_true", help="Recompila e grava de facto (por omissão só reporta, dry-run).")
    parser.add_argument("--ids", type=str, default=None, help="Lista de ids separados por vírgula (ex: 1486,1484,1483) — restringe a recompilação a essas tarefas, em vez de todas as elegíveis.")
    args = parser.parse_args()

    id_filter = None
    if args.ids:
        id_filter = {int(x.strip()) for x in args.ids.split(",") if x.strip()}

    store = LachesisStore()
    tasks = store.list_tasks()
    if id_filter is not None:
        tasks = [t for t in tasks if t["id"] in id_filter]
    eligible = [t for t in tasks if _needs_recompile(t)]

    print(f"[INFO] {len(tasks)} tarefa(s) no total, {len(eligible)} elegível(eis) para recompilação.")
    for t in eligible:
        instruction_preview = t["instruction"][:80].replace("\n", " ")
        print(f"[ELEGÍVEL] #{t['id']} {t['name']!r} — estado actual: {_state_label(t)} — instrução: {instruction_preview!r}")

    if not eligible:
        print("[INFO] Nada a fazer.")
        return

    if not args.apply:
        print("\n[DRY-RUN] Nenhuma alteração foi gravada. Corre com --apply para recompilar de facto.")
        return

    backup_path = _write_backup(eligible)
    print(f"[BACKUP] Estado actual das tarefas elegíveis gravado em {backup_path}")

    ok = 0
    failed = 0
    still_ai = 0
    for t in eligible:
        task_id = t["id"]
        before = _ai_decision_count(t.get("flow_definition"))
        try:
            sample_payload = (t.get("trigger_config") or {}).get("sample_payload")
            compiled = compile_instruction_to_flow(t["instruction"], t.get("task_type", "agentless"), sample_payload)
            store.update_task(task_id, {"flow_definition": compiled["flow_definition"]})
            after = _ai_decision_count(compiled["flow_definition"])
            warning = f" — aviso: {compiled['parse_warning']}" if compiled.get("parse_warning") else ""
            print(f"[OK] #{task_id} {t['name']!r} — ai_decision {before} -> {after}{warning}")
            ok += 1
            if after > 0:
                still_ai += 1
        except Exception as e:
            print(f"[ERRO] #{task_id} {t['name']!r} — falhou a recompilar: {e}")
            failed += 1

    print(f"\n[RESUMO] {ok} recompilada(s) com sucesso, {failed} falha(s), {still_ai} continuam com ai_decision (esperado para pedidos genuinamente ambíguos).")


if __name__ == "__main__":
    main()
