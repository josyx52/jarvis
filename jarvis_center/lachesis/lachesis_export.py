"""
Lachesis — exportação de resultados de tarefas/relatórios e auditorias Asclepion
em múltiplos formatos (txt, md, json, csv, pdf).
"""

import csv
import io
import json

from lachesis.lachesis_pdf import pdf_document as _pdf_document

_TASK_TYPE_LABELS = {
    "integration": "Tarefa de Integração",
    "agentless": "Agentless",
    "database": "Banco de Dados",
}


def _task_type_label(task_type: str) -> str:
    return _TASK_TYPE_LABELS.get(task_type, task_type)


def render_task_run(run: dict, task: dict, fmt: str) -> tuple[bytes, str, str]:
    """Devolve (conteúdo, media_type, nome_ficheiro) para um lachesis_run."""
    fmt = (fmt or "txt").lower()
    base_name = f"lachesis_{task['id']}_run_{run['id']}"
    result_text = run.get("result_text") or ""

    if fmt == "json":
        payload = {
            "task": {"id": task["id"], "name": task["name"], "instruction": task["instruction"], "task_type": task["task_type"]},
            "run": {
                "id": run["id"], "status": run["status"],
                "started_at": str(run["started_at"]), "finished_at": str(run["finished_at"]) if run["finished_at"] else None,
                "result_text": result_text, "error": run.get("error"),
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", f"{base_name}.json"

    if fmt == "md":
        content = (
            f"# {task['name']}\n\n"
            f"- **Tipo:** {_task_type_label(task['task_type'])}\n"
            f"- **Execução:** {run['started_at']}\n"
            f"- **Estado:** {run['status']}\n\n"
            f"## Resultado\n\n{result_text}\n"
        )
        return content.encode("utf-8"), "text/markdown", f"{base_name}.md"

    if fmt == "pdf":
        meta = [
            f"Tipo: {_task_type_label(task['task_type'])}",
            f"Execucao: {run['started_at']}",
            f"Estado: {run['status']}",
        ]
        content = _pdf_document(task["name"], meta, result_text)
        return content, "application/pdf", f"{base_name}.pdf"

    # txt (default)
    content = f"{task['name']}\nExecucao: {run['started_at']}\nEstado: {run['status']}\n\n{result_text}\n"
    return content.encode("utf-8"), "text/plain", f"{base_name}.txt"


def render_asclepion_run(run: dict, profile: dict, fmt: str) -> tuple[bytes, str, str]:
    """Devolve (conteúdo, media_type, nome_ficheiro) para um asclepion_run."""
    fmt = (fmt or "json").lower()
    base_name = f"asclepion_{profile['id']}_run_{run['id']}"
    results = run.get("results") or []

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "title", "severity", "pass", "output", "note"])
        for item in results:
            writer.writerow([
                item.get("id", ""), item.get("title", ""), item.get("severity", ""),
                "sim" if item.get("pass") else "nao",
                (item.get("output") or "")[:1000], item.get("note", ""),
            ])
        return buf.getvalue().encode("utf-8"), "text/csv", f"{base_name}.csv"

    if fmt == "pdf":
        meta = [
            f"Perfil: {profile['name']}",
            f"Benchmark: {profile['benchmark']}",
            f"Alvo: {run['target']}",
            f"Score: {run.get('score')}%",
            f"Execucao: {run['started_at']}",
            f"Estado: {run['status']}",
        ]
        table = [["ID", "Titulo", "Severidade", "Passou", "Nota"]]
        for item in results:
            table.append([
                str(item.get("id", "")), str(item.get("title", "")), str(item.get("severity", "")),
                "Sim" if item.get("pass") else "Nao", str(item.get("note", "")),
            ])
        content = _pdf_document(profile["name"], meta, run.get("summary") or "", table=table if len(table) > 1 else None)
        return content, "application/pdf", f"{base_name}.pdf"

    # json (default)
    payload = {
        "profile": {"id": profile["id"], "name": profile["name"], "benchmark": profile["benchmark"]},
        "run": {
            "id": run["id"], "target": run["target"], "status": run["status"],
            "score": float(run["score"]) if run.get("score") is not None else None,
            "started_at": str(run["started_at"]), "finished_at": str(run["finished_at"]) if run["finished_at"] else None,
            "results": results, "summary": run.get("summary"),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", f"{base_name}.json"
