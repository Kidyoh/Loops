"""pgvector helpers.

HNSW on plain `vector` is capped at 2000 dimensions. Larger embeddings (e.g. 4096-dim
Qwen3-Embedding-8B) are indexed via a `halfvec` expression index, and queries must use
the same expression for the index to be picked up.
"""

from sqlalchemy import TextClause, text

HNSW_MAX_VECTOR_DIM = 2000


def uses_halfvec(dim: int) -> bool:
    return dim > HNSW_MAX_VECTOR_DIM


def hnsw_index_sql(dim: int) -> str:
    if uses_halfvec(dim):
        return (
            "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks "
            f"USING hnsw ((embedding::halfvec({dim})) halfvec_cosine_ops)"
        )
    return (
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def cosine_distance_sql(dim: int, param: str = ":qvec") -> TextClause:
    """Cosine distance between chunks.embedding and a bound query vector (pass it at execute)."""
    if uses_halfvec(dim):
        expr = f"(chunks.embedding::halfvec({dim})) <=> (CAST(CAST({param} AS text) AS vector)::halfvec({dim}))"
    else:
        expr = f"chunks.embedding <=> CAST(CAST({param} AS text) AS vector)"
    return text(expr)


def to_pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
