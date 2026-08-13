"""
Clotho — motor do Conector de Bases de Dados.

Suporta ligações PostgreSQL nativamente (psycopg2, já instalado) e tabelas
importadas de CSV para o schema `clotho_data` da base de dados local do Jarvis.
Outros motores (MySQL/MSSQL/Oracle/...) são identificados pela porta mas, sem
driver instalável neste ambiente (constrangimento de ACLs do gMSA), ficam
limitados a registo/metadados — testes de ligação e queries devolvem uma nota
clara em vez de falharem silenciosamente.
"""

import csv
import io
import os
import re
from datetime import datetime

import psycopg2
import psycopg2.extras


_PORT_ENGINES = {
    5432: "postgres",
    3306: "mysql",
    1433: "mssql",
    1521: "oracle",
}

_READ_ONLY_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_MUTATING_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|MERGE|COPY|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)
_MAX_ROWS = 500


def _local_conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


def detect_engine(host: str | None, port: int | None) -> str:
    if port in _PORT_ENGINES:
        return _PORT_ENGINES[port]
    return "postgres"


def slugify_name(name: str) -> str:
    """Converte um nome livre (ex: 'Vendas 2025') num identificador sem espaços
    nem caracteres especiais, para uso em menções $db.nome$."""
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "db"


def _remote_conn(db: dict):
    config = db.get("config") or {}
    return psycopg2.connect(
        host=db.get("host"),
        port=db.get("port") or 5432,
        dbname=db.get("db_name"),
        user=config.get("username"),
        password=config.get("password"),
        connect_timeout=5,
    )


def test_connection(db: dict) -> dict:
    if db.get("engine") != "postgres":
        return {"success": False, "message": f"Driver para '{db.get('engine')}' não disponível neste ambiente."}
    try:
        conn = _remote_conn(db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            return {"success": True, "message": f"Ligado: {version}"}
        finally:
            conn.close()
    except Exception as e:
        return {"success": False, "message": str(e)}


def _introspect_postgres(conn, schema: str) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    tables: dict = {}
    for row in cur.fetchall():
        tbl = tables.setdefault(row["table_name"], {"columns": [], "row_count_estimate": None})
        tbl["columns"].append({"name": row["column_name"], "type": row["data_type"]})

    for table_name, info in tables.items():
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')
            info["row_count_estimate"] = cur.fetchone()["count"]
        except Exception:
            info["row_count_estimate"] = None

    return {"tables": tables}


def introspect_schema(db: dict) -> dict:
    if db.get("source") == "csv":
        conn = _local_conn()
        try:
            full = _introspect_postgres(conn, "clotho_data")
            table_name = db.get("table_name")
            if table_name and table_name in full["tables"]:
                return {"tables": {table_name: full["tables"][table_name]}}
            return full
        finally:
            conn.close()

    if db.get("engine") != "postgres":
        return {"tables": {}, "note": f"Driver para '{db.get('engine')}' não disponível neste ambiente."}

    conn = _remote_conn(db)
    try:
        return _introspect_postgres(conn, "public")
    finally:
        conn.close()


def _sanitize_identifier(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name.strip().lower())
    if not name or not re.match(r"[A-Za-z_]", name):
        name = f"t_{name}"
    return name


def _infer_column_type(values: list[str]) -> str:
    sample = [v for v in values if v not in (None, "")]
    if not sample:
        return "TEXT"

    def is_int(v):
        try:
            int(v)
            return True
        except ValueError:
            return False

    def is_float(v):
        try:
            float(v)
            return True
        except ValueError:
            return False

    def is_timestamp(v):
        try:
            datetime.fromisoformat(v)
            return True
        except ValueError:
            return False

    if all(is_int(v) for v in sample):
        return "BIGINT"
    if all(is_float(v) for v in sample):
        return "DOUBLE PRECISION"
    if all(is_timestamp(v) for v in sample):
        return "TIMESTAMP"
    return "TEXT"


def import_csv(table_name: str, file_bytes: bytes) -> dict:
    """Cria/recarrega clotho_data.<table_name> a partir de um CSV. Devolve o schema da tabela."""
    table_name = _sanitize_identifier(table_name)
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV vazio")

    header = [_sanitize_identifier(h) for h in rows[0]]
    sample_rows = rows[1:201]
    columns = []
    for idx, col_name in enumerate(header):
        values = [r[idx] if idx < len(r) else "" for r in sample_rows]
        columns.append({"name": col_name, "type": _infer_column_type(values)})

    conn = _local_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f'DROP TABLE IF EXISTS clotho_data."{table_name}"')
        cols_sql = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
        cur.execute(f'CREATE TABLE clotho_data."{table_name}" ({cols_sql})')

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows[1:])
        buf.seek(0)
        cur.copy_expert(
            f'COPY clotho_data."{table_name}" FROM STDIN WITH (FORMAT csv, HEADER true, NULL \'\')',
            buf,
        )

        cur.execute(f'SELECT COUNT(*) FROM clotho_data."{table_name}"')
        row_count = cur.fetchone()[0]
    finally:
        conn.close()

    return {"table_name": table_name, "columns": columns, "row_count_estimate": row_count}


def run_query(db: dict, sql: str) -> dict:
    sql = sql.strip().rstrip(";").strip()
    if not _READ_ONLY_START.match(sql):
        return {"error": "Só são permitidas queries SELECT/WITH (só-leitura)."}
    if ";" in sql:
        return {"error": "Apenas uma instrução SQL é permitida (sem ';' adicional)."}
    if _MUTATING_KEYWORDS.search(sql):
        return {"error": "A query contém palavras-chave não permitidas (só-leitura)."}

    if "limit" not in sql.lower():
        sql = f"{sql} LIMIT {_MAX_ROWS}"

    if db.get("source") == "csv":
        conn = _local_conn()
    elif db.get("engine") == "postgres":
        conn = _remote_conn(db)
    else:
        return {"error": f"Driver para '{db.get('engine')}' não disponível neste ambiente."}

    try:
        cur = conn.cursor()
        if db.get("source") == "csv":
            cur.execute("SET search_path TO clotho_data, public")
        cur.execute(sql)
        columns = [c.name for c in cur.description] if cur.description else []
        rows = cur.fetchall()
        truncated = len(rows) >= _MAX_ROWS
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:_MAX_ROWS]],
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()