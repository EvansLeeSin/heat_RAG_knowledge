"""Idempotent bootstrap for the fresh, local WSL database; no data imports."""
from sqlalchemy import text
from app.core.database import get_engine
from app.models import Base


def main() -> None:
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_chunk_search_vector "
            "ON knowledge_chunk USING GIN (search_vector)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_document_scope "
            "ON knowledge_document (access_scope)"
        ))
    # Do not train an IVFFlat index against an empty database. Create/rebuild
    # that index separately after real embeddings have been ingested.
    print("WSL database schema and keyword indexes ready; no documents imported.")


if __name__ == "__main__":
    main()
