#!/usr/bin/env python3
"""Regenerate docs/SCHEMA.md from a live Postgres database.

Introspects information_schema (tables, columns, primary keys, foreign
keys, check constraints, indexes) and renders a Mermaid ERD plus a
per-table column reference. This is the only place the schema is
documented in prose form — the migrations in db/migrations/*.sql remain
the source of truth for the actual DDL; this script just reflects
whatever they produced.

Usage:
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/video_qna \
        python3 scripts/gen-schema-doc.py

Run this (and commit the result) after adding or changing a migration.
CI's `migrations` job re-runs it against a fresh DB and fails the build
if docs/SCHEMA.md doesn't match — see .github/workflows/pr-validation.yml.
"""
import os
import re
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit(
        "psycopg2-binary is required: pip install psycopg2-binary\n"
        "(not a backend/ dependency on purpose — this script only runs "
        "in CI/dev tooling, not the app itself)"
    )

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/video_qna"
)
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "SCHEMA.md"

TABLES_SQL = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name;
"""

COLUMNS_SQL = """
    SELECT c.table_name, c.column_name, c.data_type, c.udt_name,
           format_type(a.atttypid, a.atttypmod) AS full_type,
           c.is_nullable, c.column_default,
           (SELECT ccu.column_name IS NOT NULL
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.table_schema = c.table_schema
              AND tc.table_name = c.table_name
              AND tc.constraint_type = 'PRIMARY KEY'
              AND ccu.column_name = c.column_name) AS is_pk
    FROM information_schema.columns c
    JOIN pg_catalog.pg_class pc
      ON pc.relname = c.table_name AND pc.relnamespace = (
          SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = c.table_schema)
    JOIN pg_catalog.pg_attribute a
      ON a.attrelid = pc.oid AND a.attname = c.column_name AND a.attnum > 0
    WHERE c.table_schema = 'public'
    ORDER BY c.table_name, c.ordinal_position;
"""

FKS_SQL = """
    SELECT
        tc.table_name AS from_table,
        kcu.column_name AS from_column,
        ccu.table_name AS to_table,
        ccu.column_name AS to_column,
        rc.delete_rule
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name
     AND ccu.table_schema = tc.table_schema
    JOIN information_schema.referential_constraints rc
      ON rc.constraint_name = tc.constraint_name
     AND rc.constraint_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    ORDER BY from_table, from_column;
"""

CHECKS_SQL = """
    SELECT tc.table_name, cc.check_clause
    FROM information_schema.table_constraints tc
    JOIN information_schema.check_constraints cc
      ON cc.constraint_name = tc.constraint_name
     AND cc.constraint_schema = tc.table_schema
    WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = 'public'
      AND tc.constraint_name NOT LIKE '%_not_null'
    ORDER BY tc.table_name;
"""

INDEXES_SQL = """
    SELECT tablename, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = 'public' AND indexname NOT LIKE '%_pkey'
    ORDER BY tablename, indexname;
"""

UNIQUE_SQL = """
    SELECT tc.table_name, string_agg(kcu.column_name, ', ' ORDER BY kcu.ordinal_position)
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
    GROUP BY tc.table_name, tc.constraint_name
    ORDER BY tc.table_name;
"""

# Mermaid erDiagram doesn't have a real type system; map Postgres types to
# short tokens that stay legible in the diagram.
MERMAID_TYPE = {
    "integer": "int",
    "bigint": "bigint",
    "smallint": "smallint",
    "text": "text",
    "character varying": "varchar",
    "numeric": "numeric",
    "boolean": "bool",
    "uuid": "uuid",
    "timestamp with time zone": "timestamptz",
    "jsonb": "jsonb",
    "tsvector": "tsvector",
    "ARRAY": "array",
    "USER-DEFINED": "vector",
}


def fetch_all(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def mermaid_type(col):
    if col["data_type"] == "ARRAY":
        return MERMAID_TYPE["ARRAY"]
    if col["data_type"] == "USER-DEFINED":
        # Mermaid erDiagram types can't contain "(" / ")" or spaces, so a
        # concrete pgvector type like vector(4096) has to be flattened.
        return re.sub(r"[^A-Za-z0-9_]", "", col["full_type"])
    return MERMAID_TYPE.get(col["data_type"], col["udt_name"])


def render_erd(tables, columns_by_table, fks, pk_by_table):
    lines = ["```mermaid", "erDiagram"]
    for fk in fks:
        # child (has the FK) }o--|| parent (referenced), one parent to many children
        lines.append(
            f'    {fk["to_table"]} ||--o{{ {fk["from_table"]} : '
            f'"{fk["from_column"]}"'
        )
    for table in tables:
        lines.append(f"    {table} {{")
        for col in columns_by_table[table]:
            attrs = []
            if col["column_name"] in pk_by_table.get(table, set()):
                attrs.append("PK")
            lines.append(
                f'        {mermaid_type(col)} {col["column_name"]}'
                + (f' "{" ".join(attrs)}"' if attrs else "")
            )
        lines.append("    }")
    lines.append("```")
    return "\n".join(lines)


def render_table_reference(table, columns, fks_by_from, checks_by_table,
                            indexes_by_table, unique_by_table):
    out = [f"### `{table}`", "", "| Column | Type | Nullable | Default | Notes |",
           "|---|---|---|---|---|"]
    fk_by_col = {fk["from_column"]: fk for fk in fks_by_from.get(table, [])}
    for col in columns:
        notes = []
        if col["is_pk"]:
            notes.append("PK")
        if col["column_name"] in fk_by_col:
            fk = fk_by_col[col["column_name"]]
            notes.append(
                f'FK → `{fk["to_table"]}.{fk["to_column"]}` '
                f'(ON DELETE {fk["delete_rule"]})'
            )
        default = col["column_default"] or ""
        # gen_random_uuid()/now() etc render fine as-is; trim overly long
        # generated-column expressions so the table stays readable.
        if default and len(default) > 60:
            default = default[:57] + "..."
        out.append(
            f'| `{col["column_name"]}` | `{col["full_type"]}` | '
            f'{"yes" if col["is_nullable"] == "YES" else "no"} | '
            f'{"`" + default + "`" if default else "—"} | '
            f'{"; ".join(notes) if notes else "—"} |'
        )
    if table in checks_by_table:
        out.append("")
        out.append("Check constraints:")
        for cc in checks_by_table[table]:
            out.append(f"- `{cc}`")
    if table in unique_by_table:
        out.append("")
        out.append("Unique constraints: " +
                    ", ".join(f"`({u})`" for u in unique_by_table[table]))
    if table in indexes_by_table:
        out.append("")
        out.append("Indexes:")
        for idx in indexes_by_table[table]:
            out.append(f"- `{idx}`")
    out.append("")
    return "\n".join(out)


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    tables = [r["table_name"] for r in fetch_all(cur, TABLES_SQL)]
    all_columns = fetch_all(cur, COLUMNS_SQL)
    fks = fetch_all(cur, FKS_SQL)
    checks = fetch_all(cur, CHECKS_SQL)
    indexes = fetch_all(cur, INDEXES_SQL)
    uniques = fetch_all(cur, UNIQUE_SQL)
    cur.close()
    conn.close()

    columns_by_table = {t: [] for t in tables}
    for col in all_columns:
        columns_by_table.setdefault(col["table_name"], []).append(col)

    pk_by_table = {}
    for col in all_columns:
        if col["is_pk"]:
            pk_by_table.setdefault(col["table_name"], set()).add(col["column_name"])

    fks_by_from = {}
    for fk in fks:
        fks_by_from.setdefault(fk["from_table"], []).append(fk)

    checks_by_table = {}
    for c in checks:
        clause = re.sub(r"\s+", " ", c["check_clause"]).strip()
        checks_by_table.setdefault(c["table_name"], []).append(clause)

    indexes_by_table = {}
    for i in indexes:
        indexes_by_table.setdefault(i["tablename"], []).append(i["indexdef"])

    unique_by_table = {}
    for u in uniques:
        unique_by_table.setdefault(u["table_name"], []).append(u["string_agg"])

    parts = [
        "<!-- AUTO-GENERATED by scripts/gen-schema-doc.py — do not hand-edit. -->",
        "<!-- Regenerate after any change under db/migrations/, then commit -->",
        "<!-- the result. CI (pr-validation.yml, `migrations` job) checks   -->",
        "<!-- this file for drift against a freshly-migrated database.     -->",
        "",
        "# Database schema",
        "",
        "Generated from the tables that exist after applying every migration",
        "in [`db/migrations/`](../db/migrations/) in order. The migrations",
        "themselves (each has a comment explaining *why*) are the source of",
        "truth — this file is a reflection of them for quick reference, not",
        "a second place to hand-edit the schema.",
        "",
        "## ER diagram",
        "",
        render_erd(tables, columns_by_table, fks, pk_by_table),
        "",
        "## Tables",
        "",
    ]
    for table in tables:
        parts.append(
            render_table_reference(
                table, columns_by_table[table], fks_by_from,
                checks_by_table, indexes_by_table, unique_by_table,
            )
        )

    OUT_PATH.write_text("\n".join(parts).rstrip() + "\n")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
