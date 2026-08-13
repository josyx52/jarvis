import subprocess
import json
from typing import Any

OSQUERY_PATH = r"C:\Program Files\osquery\osqueryi.exe"


class OsqueryTool:

    def run_query(self, query: str) -> Any:

        cmd = [
            OSQUERY_PATH,
            "--json",
            query
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "Erro ao executar osquery"
            )

        stdout = (result.stdout or "").strip()

        if not stdout:
            return []

        return json.loads(stdout)

    def safe_run_query(self, query: str):

        try:
            return self.run_query(query)

        except Exception as e:

            print("[OsqueryTool] query error:", e)

            return []

    def list_tables(self) -> list[str]:

        rows = self.run_query(
            "SELECT name FROM osquery_registry WHERE registry='table';"
        )

        return [row["name"] for row in rows if "name" in row]