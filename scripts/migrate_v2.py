"""
scripts/migrate_v2.py
======================
Migración one-shot para pasar tu base de datos existente (wasapi.db,
o Postgres si usas DATABASE_URL) al nuevo esquema. Agrega TODAS las
columnas nuevas introducidas en las tres rondas de mejoras:

  Ronda 1 — Panel Admin, permisos de agentes, plantillas por cliente:
    - users.is_superadmin
    - agents.permissions

  Ronda 2 — Multi-industria, recordatorios, plantillas Meta, documentos,
  opt-out, auto-respuesta:
    - users.industry, business_hours, auto_reply_enabled,
      auto_reply_message, last_whatsapp_error, last_whatsapp_error_at
    - contacts.opted_out, custom_fields
    - campaigns.meta_template_name, meta_template_language, is_recurring
    - conversations.last_inbound_at, last_auto_reply_at
    - campaign_templates.meta_template_name, meta_template_language
    - invoice_items.doc_type

  Ronda 3 — Emparejamiento flexible de documentos (no solo teléfono):
    - invoice_batches.match_field_key
    - invoice_items.match_value

  (las tablas nuevas — custom_field_defs, meta_templates,
  recurring_campaigns — las crea init_db() automáticamente:
  create_all() sí crea tablas que faltan, solo NO agrega columnas
  a tablas que ya existen)

Uso:
    python scripts/migrate_v2.py tu_email@empresa.com

El email es el de la cuenta que quieres convertir en superadmin
(PRIME). Si no pasas ningún email, solo hace la migración de columnas
y deja is_superadmin en False para todos (puedes correrlo de nuevo
después con el email para promover tu cuenta).

Seguro de correr varias veces: si una columna ya existe, lo detecta
y no falla. Funciona igual con SQLite y con Postgres (DATABASE_URL).
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from models.database import engine, init_db

IS_SQLITE = "sqlite" in os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./wasapi.db")

# (tabla, columna, definición SQL para el ALTER TABLE ... ADD COLUMN)
COLUMNS_TO_ADD = [
    ("users", "is_superadmin", "BOOLEAN DEFAULT 0"),
    ("users", "industry", "VARCHAR(100)"),
    ("users", "business_hours", "TEXT"),
    ("users", "auto_reply_enabled", "BOOLEAN DEFAULT 0"),
    ("users", "auto_reply_message", "TEXT"),
    ("users", "last_whatsapp_error", "TEXT"),
    ("users", "last_whatsapp_error_at", "TIMESTAMP"),

    ("agents", "permissions", "VARCHAR(300) DEFAULT ''"),

    ("contacts", "opted_out", "BOOLEAN DEFAULT 0"),
    ("contacts", "custom_fields", "TEXT"),

    ("campaigns", "meta_template_name", "VARCHAR(200)"),
    ("campaigns", "meta_template_language", "VARCHAR(10) DEFAULT 'es'"),
    ("campaigns", "is_recurring", "BOOLEAN DEFAULT 0"),

    ("conversations", "last_inbound_at", "TIMESTAMP"),
    ("conversations", "last_auto_reply_at", "TIMESTAMP"),

    ("campaign_templates", "meta_template_name", "VARCHAR(200)"),
    ("campaign_templates", "meta_template_language", "VARCHAR(10) DEFAULT 'es'"),

    ("invoice_items", "doc_type", "VARCHAR(50) DEFAULT 'Documento'"),
    ("invoice_items", "match_value", "VARCHAR(200)"),
    ("invoice_batches", "match_field_key", "VARCHAR(80) DEFAULT 'phone'"),
]


async def table_exists(conn, table: str) -> bool:
    if IS_SQLITE:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
        )
        return result.fetchone() is not None
    else:
        result = await conn.execute(
            text("SELECT to_regclass(:t)"), {"t": table}
        )
        return result.scalar() is not None


async def column_exists(conn, table: str, column: str) -> bool:
    if IS_SQLITE:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        cols = [row[1] for row in result.fetchall()]
        return column in cols
    else:
        result = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
            {"t": table, "c": column}
        )
        return result.fetchone() is not None


async def migrate(promote_email: str = None):
    # 1) Crea cualquier tabla nueva que falte (custom_field_defs,
    #    meta_templates, recurring_campaigns, campaign_templates...)
    await init_db()

    async with engine.begin() as conn:
        for table, column, coltype in COLUMNS_TO_ADD:
            if not await table_exists(conn, table):
                print(f"[migrate] ⚠️  Tabla {table} no existe todavía (¿primera vez?) — se omite {column}, init_db() ya la crea completa.")
                continue
            if await column_exists(conn, table, column):
                print(f"[migrate] {table}.{column} ya existe, omitiendo.")
                continue
            print(f"[migrate] Agregando {table}.{column} ...")
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))

        if promote_email:
            res = await conn.execute(
                text("UPDATE users SET is_superadmin = 1 WHERE email = :email"),
                {"email": promote_email}
            )
            if res.rowcount:
                print(f"[migrate] ✅ {promote_email} promovido a superadmin.")
            else:
                print(f"[migrate] ⚠️  No se encontró ningún usuario con email {promote_email}.")

    print("[migrate] Listo.")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(migrate(email))
