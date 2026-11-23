"""
migrations.py - Upgrades an existing database when new columns are added.

`create_all()` creates missing TABLES but never adds new COLUMNS to tables that
already exist. This small helper adds any missing (nullable) columns, so your old
chat.db keeps working after an update.

In bigger projects this job is done by Alembic, a proper migration tool that
keeps a versioned history of every schema change.
"""

from sqlalchemy import inspect, text


def add_missing_columns(engine, metadata) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if table.name not in tables:
                continue  # brand-new table: create_all() already made it
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"Can't add required column {table.name}.{column.name} automatically. "
                        "Delete the database file (development) or use Alembic."
                    )
                column_type = column.type.compile(dialect=engine.dialect)
                connection.execute(text(
                    f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(column.name)} {column_type}"
                ))
                print(f"[database] Added column {table.name}.{column.name}")
