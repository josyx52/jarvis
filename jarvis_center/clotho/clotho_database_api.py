"""
Clotho — Conector de Bases de Dados.

Permite ligar a um servidor de base de dados externo (PostgreSQL, com detecção
de tecnologia por porta para outros motores) ou gerar uma tabela a partir de um
ficheiro CSV, introspectar o schema e correr queries SQL só-leitura.

Montado em /fates/clotho/databases pelo clotho_api.py.
"""

import base64

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from clotho.clotho_database_store import ClothoDatabaseStore
from clotho.clotho_database_engine import detect_engine, import_csv, introspect_schema, run_query, test_connection

router = APIRouter(prefix="/databases", tags=["clotho-databases"])

_store = ClothoDatabaseStore()


class DatabaseCreateRequest(BaseModel):
    name: str
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    engine: str | None = None
    config: dict | None = None
    notes: str | None = None
    created_by: str | None = None


class DatabaseUpdateRequest(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    engine: str | None = None
    config: dict | None = None
    notes: str | None = None


class QueryRequest(BaseModel):
    sql: str


class CsvUploadRequest(BaseModel):
    name: str
    table_name: str
    content_base64: str


def _strip_secret(db: dict) -> dict:
    db = dict(db)
    config = db.pop("config", None) or {}
    db["has_credentials"] = bool(config)
    return db


@router.get("/")
async def list_databases():
    return {"databases": [_strip_secret(d) for d in _store.list_databases()]}


@router.post("/")
async def create_database(body: DatabaseCreateRequest):
    data = body.model_dump()
    data["source"] = "connection"
    data["engine"] = data.get("engine") or detect_engine(data.get("host"), data.get("port"))
    return _strip_secret(_store.create_database(data))


@router.get("/{database_id}")
async def get_database(database_id: int):
    db = _store.get_database(database_id)
    if not db:
        raise HTTPException(status_code=404, detail="Database not found")
    return _strip_secret(db)


@router.put("/{database_id}")
async def update_database(database_id: int, body: DatabaseUpdateRequest):
    db = _store.get_database(database_id)
    if not db:
        raise HTTPException(status_code=404, detail="Database not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return _strip_secret(_store.update_database(database_id, data))


@router.delete("/{database_id}")
async def delete_database(database_id: int):
    if not _store.delete_database(database_id):
        raise HTTPException(status_code=404, detail="Database not found")
    return {"deleted": database_id}


@router.post("/{database_id}/test")
async def test_database(database_id: int):
    db = _store.get_database(database_id)
    if not db:
        raise HTTPException(status_code=404, detail="Database not found")
    result = test_connection(db)
    _store.set_status(database_id, "ready" if result["success"] else "error")
    return result


@router.post("/{database_id}/introspect")
async def introspect_database(database_id: int):
    db = _store.get_database(database_id)
    if not db:
        raise HTTPException(status_code=404, detail="Database not found")
    schema = introspect_schema(db)
    updated = _store.set_schema_cache(database_id, schema, status="ready" if not schema.get("note") else "error")
    return _strip_secret(updated)


@router.post("/upload_csv")
async def upload_csv(body: CsvUploadRequest):
    try:
        content = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(status_code=422, detail="content_base64 inválido")

    try:
        schema = import_csv(body.table_name, content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    db = _store.create_database({
        "name": body.name,
        "source": "csv",
        "engine": "postgres",
        "table_name": schema["table_name"],
        "status": "ready",
    })
    updated = _store.set_schema_cache(db["id"], {"tables": {schema["table_name"]: {
        "columns": schema["columns"],
        "row_count_estimate": schema["row_count_estimate"],
    }}}, status="ready")
    return _strip_secret(updated)


@router.post("/{database_id}/query")
async def query_database(database_id: int, body: QueryRequest):
    db = _store.get_database(database_id)
    if not db:
        raise HTTPException(status_code=404, detail="Database not found")
    return run_query(db, body.sql)