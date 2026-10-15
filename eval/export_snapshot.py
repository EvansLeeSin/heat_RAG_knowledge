"""受控离线导出指定文档的活动版本与全部 chunk。

仅连接显式提供的隔离评测库；不请求 HTTP / Embedding，不导出向量或文件路径。
数据库连接来自 KNOWLEDGE_EVAL_DATABASE_URL（不读取应用默认地址或 .env）。
事务采用 REPEATABLE READ + READ ONLY，输出文件不得覆盖已有快照。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from .corpus_map import CorpusValidationError, build_snapshot, load_bindings
except ImportError:  # python eval/export_snapshot.py
    from corpus_map import CorpusValidationError, build_snapshot, load_bindings

DOCUMENT_SQL = """
SELECT a.document_id, a.active_version, d.version, d.title, d.publisher,
       d.source_url, d.document_type, d.region, d.applicability,
       d.effective_status, d.publication_status, d.published_at,
       d.effective_from, d.effective_to, d.access_scope, d.project_ids,
       d.content_hash, d.parser_version
FROM document_active_version a
LEFT JOIN knowledge_document d
  ON d.document_id = a.document_id AND d.version = a.active_version
WHERE a.document_id IN :document_ids
ORDER BY a.document_id
"""

CHUNK_SQL = """
SELECT c.chunk_id, c.document_id, c.version, c.chunk_index,
       c.section_path, c.article_no, c.heading, c.content,
       c.content_hash, c.token_count, c.access_scope, c.project_ids,
       c.tokenizer_version, (c.search_vector IS NOT NULL) AS has_search_vector,
       e.embedding_model, e.embedding_version, e.index_version,
       (e.vector IS NOT NULL) AS has_embedding
FROM knowledge_chunk c
JOIN document_active_version a
  ON a.document_id = c.document_id AND a.active_version = c.version
LEFT JOIN knowledge_embedding e ON e.chunk_id = c.chunk_id
WHERE c.document_id IN :document_ids
ORDER BY c.document_id, c.chunk_index, c.chunk_id
"""


def _timestamp(value):
    return value.isoformat() if value is not None else None


def rows_to_snapshot(document_rows: list[dict], chunk_rows: list[dict], document_ids: list[str]) -> dict:
    actual = {r["document_id"] for r in document_rows}
    missing = set(document_ids) - actual
    if missing:
        raise CorpusValidationError("指定文档没有活动版本：" + ", ".join(sorted(missing)))
    if actual != set(document_ids):
        raise CorpusValidationError("数据库返回了清单外文档")
    documents = []
    for row in document_rows:
        if row["version"] is None or row["version"] != row["active_version"]:
            raise CorpusValidationError(f"活动指针缺少对应文档版本：{row['document_id']}")
        documents.append({
            "documentId": row["document_id"], "version": row["version"],
            "title": row["title"], "publisher": row["publisher"],
            "sourceUrl": row["source_url"], "documentType": row["document_type"],
            "region": row["region"], "applicability": row["applicability"],
            "effectiveStatus": row["effective_status"], "publicationStatus": row["publication_status"],
            "publishedAt": _timestamp(row["published_at"]),
            "effectiveFrom": _timestamp(row["effective_from"]), "effectiveTo": _timestamp(row["effective_to"]),
            "accessScope": row["access_scope"], "projectIds": row["project_ids"],
            "sourceContentHash": row["content_hash"], "parserVersion": row["parser_version"],
        })
    chunks = [{
        "chunkId": row["chunk_id"], "documentId": row["document_id"], "version": row["version"],
        "chunkIndex": row["chunk_index"], "sectionPath": row["section_path"],
        "articleNo": row["article_no"], "heading": row["heading"], "content": row["content"],
        "storedContentHash": row["content_hash"], "tokenCount": row["token_count"],
        "accessScope": row["access_scope"], "projectIds": row["project_ids"],
        "tokenizerVersion": row["tokenizer_version"], "hasSearchVector": bool(row["has_search_vector"]),
        "hasEmbedding": bool(row["has_embedding"]), "embeddingModel": row["embedding_model"],
        "embeddingVersion": row["embedding_version"], "indexVersion": row["index_version"],
    } for row in chunk_rows]
    return build_snapshot(documents, chunks)


def read_active_corpus(connection, document_ids: list[str]) -> dict:
    """必须在只读一致性事务内调用；SQL 没有 topK、LIMIT 或向量近邻操作。"""
    from sqlalchemy import bindparam, text

    def read(sql):
        statement = text(sql).bindparams(bindparam("document_ids", expanding=True))
        return [dict(row) for row in connection.execute(statement, {"document_ids": document_ids}).mappings().all()]

    return rows_to_snapshot(read(DOCUMENT_SQL), read(CHUNK_SQL), document_ids)


def export_database(database_url: str, document_ids: list[str]) -> dict:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise CorpusValidationError("导出只支持 PostgreSQL 评测数据库")
    if not document_ids or any(not isinstance(d, str) or not d.strip() for d in document_ids):
        raise CorpusValidationError("必须显式指定非空的文档白名单")
    document_ids = sorted(set(document_ids))
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                return read_active_corpus(connection, document_ids)
    finally:
        engine.dispose()


def save_snapshot(snapshot: dict, path: str | Path) -> None:
    from tempfile import NamedTemporaryFile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件完整写入后再独占创建最终文件，不覆盖旧记录，不留下半写快照。
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--document-id", action="append", help="允许导出的文档，可重复指定")
    group.add_argument("--bindings", help="使用历史证据绑定文件中的文档白名单")
    group.add_argument("--dataset", help="使用标准化评测集 manifest 的完整文档白名单")
    ap.add_argument("--out", required=True, help="新快照路径，不覆盖已有文件")
    args = ap.parse_args()
    url = os.environ.get("KNOWLEDGE_EVAL_DATABASE_URL")
    if not url:
        ap.exit(1, "缺少 KNOWLEDGE_EVAL_DATABASE_URL；不会连接应用默认数据库\n")
    if Path(args.out).exists():
        ap.exit(1, "输出已存在，请使用新的快照路径\n")
    try:
        if args.dataset:
            try:
                from .dataset import load_dataset
            except ImportError:
                from dataset import load_dataset
            dataset = load_dataset(args.dataset)
            ids = [entry["metadata"]["documentId"] for entry in dataset.manifest["corpus"]]
        else:
            ids = sorted(set(load_bindings(args.bindings).values())) if args.bindings else args.document_id
        snapshot = export_database(url, ids)
        save_snapshot(snapshot, args.out)
    except CorpusValidationError as exc:
        ap.exit(1, f"导出校验失败：{exc}\n")
    except Exception as exc:
        # 不打印数据库 URL、凭据、SQL 参数或供应商异常详情。
        ap.exit(1, f"导出失败（{type(exc).__name__}），请检查依赖、数据库权限和输出目录；未生成有效快照\n")
    print(f"导出成功：{args.out}")
    print(f"文档 {snapshot['documentCount']}，chunk {snapshot['chunkCount']}，corpusHash={snapshot['corpusHash']}")


if __name__ == "__main__":
    main()
