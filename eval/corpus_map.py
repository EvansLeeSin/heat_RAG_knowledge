"""离线语料快照校验与证据映射；不调用检索 API。

先用 export_snapshot.py 从隔离测试库导出，再通过 KNOWLEDGE_EVAL_SNAPSHOT
指定快照供 run_eval.py / calibrate_threshold.py 使用。原文 anchors 的文档身份
已在标准化案例中明确声明；evidence_bindings.json 仅用于历史标注兼容。
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

DEFAULT_BINDINGS = Path(__file__).with_name("evidence_bindings.json")


class CorpusValidationError(ValueError):
    """快照或证据标注不可信；必须在执行检索前中止。"""


def canonical_hash(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_structure(documents: list[dict], chunks: list[dict]) -> None:
    if not documents:
        raise CorpusValidationError("快照没有文档")
    docs = {}
    for doc in documents:
        doc_id = doc.get("documentId")
        version = doc.get("version")
        if not isinstance(doc_id, str) or not doc_id or doc_id in docs:
            raise CorpusValidationError("文档 ID 缺失或重复")
        if type(version) is not int or version < 1:
            raise CorpusValidationError(f"文档 {doc_id} 的活动版本无效")
        docs[doc_id] = doc
    seen = set()
    positions = set()
    counts = Counter()
    for chunk in chunks:
        chunk_id = chunk.get("chunkId")
        doc_id = chunk.get("documentId")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
            raise CorpusValidationError("chunk ID 缺失或重复")
        seen.add(chunk_id)
        if doc_id not in docs or chunk.get("version") != docs[doc_id]["version"]:
            raise CorpusValidationError(f"chunk {chunk_id} 不属于清单中的活动文档版本")
        index = chunk.get("chunkIndex")
        if type(index) is not int or index < 0 or (doc_id, index) in positions:
            raise CorpusValidationError(f"chunk {chunk_id} 的位置无效或重复")
        positions.add((doc_id, index))
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            raise CorpusValidationError(f"chunk {chunk_id} 的内容为空")
        if chunk.get("contentHash") != content_hash(content):
            raise CorpusValidationError(f"chunk {chunk_id} 的内容哈希不匹配")
        counts[doc_id] += 1
    for doc_id, doc in docs.items():
        if counts[doc_id] == 0 or doc.get("chunkCount") != counts[doc_id]:
            raise CorpusValidationError(f"文档 {doc_id} 的 chunk 数量为零或与清单不一致")


def build_snapshot(documents: list[dict], chunks: list[dict]) -> dict:
    """由离线导出行创建快照；不包含向量、凭据和服务器文件路径。"""
    documents = sorted((dict(d) for d in documents), key=lambda d: d["documentId"])
    chunks = sorted((dict(c) for c in chunks), key=lambda c: (c["documentId"], c["chunkIndex"], c["chunkId"]))
    counts = Counter(c["documentId"] for c in chunks)
    for doc in documents:
        doc["chunkCount"] = counts[doc["documentId"]]
    for chunk in chunks:
        chunk["contentHash"] = content_hash(chunk["content"])
    _validate_structure(documents, chunks)
    payload = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "exportMethod": "postgres-repeatable-read-read-only",
        "documentIds": [d["documentId"] for d in documents],
        "documentCount": len(documents),
        "chunkCount": len(chunks),
        "corpusHash": canonical_hash({"documents": documents, "chunks": chunks}),
        "documents": documents,
        "chunks": chunks,
    }
    return {**payload, "snapshotId": canonical_hash(payload)}


def validate_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        raise CorpusValidationError("不支持的快照格式；不能使用旧的检索结果列表")
    payload = {k: v for k, v in snapshot.items() if k != "snapshotId"}
    if snapshot.get("snapshotId") != canonical_hash(payload):
        raise CorpusValidationError("快照整体哈希不匹配")
    if snapshot.get("exportMethod") != "postgres-repeatable-read-read-only":
        raise CorpusValidationError("快照来源不是受控只读数据库导出")
    documents, chunks = snapshot.get("documents"), snapshot.get("chunks")
    if not isinstance(documents, list) or not isinstance(chunks, list):
        raise CorpusValidationError("快照缺少 documents/chunks")
    if not all(isinstance(row, dict) for row in documents + chunks):
        raise CorpusValidationError("快照文档或 chunk 格式无效")
    _validate_structure(documents, chunks)
    if snapshot.get("documentIds") != [d["documentId"] for d in documents]:
        raise CorpusValidationError("文档清单不一致")
    if snapshot.get("documentCount") != len(documents) or snapshot.get("chunkCount") != len(chunks):
        raise CorpusValidationError("快照总数量不一致")
    if snapshot.get("corpusHash") != canonical_hash({"documents": documents, "chunks": chunks}):
        raise CorpusValidationError("语料哈希不匹配")
    return snapshot


def load_snapshot(path: str | Path | None = None) -> dict:
    path = path or os.environ.get("KNOWLEDGE_EVAL_SNAPSHOT")
    if not path:
        raise CorpusValidationError("请先离线导出快照，再设置 KNOWLEDGE_EVAL_SNAPSHOT 或传入 --snapshot")
    with open(path, encoding="utf-8") as f:
        return validate_snapshot(json.load(f))


def chunks_from_snapshot(snapshot: dict) -> list[dict]:
    """展开已校验快照；执行器可只读取文件一次，避免文件两次读取间变动。"""
    by_id = {d["documentId"]: d for d in snapshot["documents"]}
    return [{**by_id[c["documentId"]], **c, "documentMetadata": by_id[c["documentId"]]}
            for c in snapshot["chunks"]]


def fetch_chunks(path: str | Path | None = None) -> list[dict]:
    """保留调用方函数名，但只加载已校验的离线快照，绝不请求检索服务。"""
    return chunks_from_snapshot(load_snapshot(path))


def load_bindings(path: str | Path = DEFAULT_BINDINGS) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise CorpusValidationError("不支持的证据绑定格式")
    bindings = {}
    for doc in data.get("documents", []):
        doc_id = doc.get("documentId")
        if not isinstance(doc_id, str) or not doc_id:
            raise CorpusValidationError("证据绑定缺少 documentId")
        for anchor in doc.get("anchors", []):
            if not isinstance(anchor, str) or not anchor.strip() or anchor in bindings:
                raise CorpusValidationError("证据锚点为空或重复绑定")
            bindings[anchor] = doc_id
    if not bindings:
        raise CorpusValidationError("证据绑定为空")
    return bindings


def resolve(anchor: str, chunks: list[dict], document_id: str, version: int | None = None) -> list[str]:
    """限定文档身份；同一活动版本内重叠切分允许命中多个 chunk。"""
    if not isinstance(anchor, str) or not anchor.strip() or not document_id:
        raise CorpusValidationError("证据必须包含非空 anchor 和文档 ID")
    hits = [c for c in chunks if c["documentId"] == document_id and anchor in c["content"]
            and (version is None or c["version"] == version)]
    if not hits:
        raise CorpusValidationError(f"证据未命中：documentId={document_id}, anchor={anchor!r}")
    if len({c["version"] for c in hits}) != 1:
        raise CorpusValidationError(f"证据跨版本歧义：documentId={document_id}, anchor={anchor!r}")
    return sorted({c["chunkId"] for c in hits})


def _prepare_standard_case(case: dict, chunks: list[dict]) -> dict:
    try:
        from .dataset import validate_case, document_allowed
    except ImportError:
        from dataset import validate_case, document_allowed
    validate_case(case)
    if case["review_status"] != "SOURCE_CHECKED":
        raise CorpusValidationError(f"案例待审核：{case['case_id']}")
    docs = {c["documentId"]: c.get("documentMetadata", c) for c in chunks}
    expected, ids = case["expected"], set()
    if not set(expected["forbidden_document_ids"]) <= set(docs):
        raise CorpusValidationError("禁止文档不在快照中，不能运行残缺的边界语料")
    for kind in ("evidence", "excluded_evidence"):
        for proof in expected[kind]:
            doc_id = proof["document_id"]
            matches = resolve(proof["anchor"], chunks, doc_id, proof.get("version"))
            allowed = document_allowed(docs[doc_id], case["request"])
            if allowed != (kind == "evidence"):
                raise CorpusValidationError(f"证据可用性与快照/查询条件矛盾：{doc_id}")
            if kind == "evidence":
                ids.update(matches)
    return {**case, "_relevant": ids, "_corpus_document_ids": sorted(docs),
            "_corpus_chunk_ids": sorted({c["chunkId"] for c in chunks}),
            "_scope_forbidden": [doc_id for doc_id, metadata in docs.items()
                                 if not document_allowed(metadata, case["request"])]}


def prepare_cases(cases: list[dict], chunks: list[dict], bindings: dict[str, str] | None = None, *, dataset=None) -> tuple[list[dict], list[dict]]:
    """全部标注校验成功才返回；旧标注须显式传绑定，新标注使用内嵌证据。"""
    if dataset is not None:
        try:
            from .dataset import validate_snapshot_corpus
        except ImportError:
            from dataset import validate_snapshot_corpus
        validate_snapshot_corpus(dataset, chunks)
    positives, negatives, errors = [], [], []
    for index, case in enumerate(cases, 1):
        if isinstance(case, dict) and "schema_version" in case:
            try:
                prepared = _prepare_standard_case(case, chunks)
                (negatives if case["type"] == "N" else positives).append(prepared)
            except ValueError as exc:
                errors.append(f"案例 {case.get('case_id', index)}：{exc}")
            continue
        label = f"案例 {index} ({case.get('query', '')[:30]})"
        kind, anchors = case.get("type"), case.get("anchors")
        if kind not in {"K", "S", "N"} or not isinstance(case.get("query"), str) or not case["query"].strip():
            errors.append(f"{label}：类型或 query 无效")
            continue
        if not isinstance(anchors, list) or not all(isinstance(a, str) and a.strip() for a in anchors):
            errors.append(f"{label}：anchors 必须是非空字符串列表")
            continue
        if kind == "N":
            if anchors:
                errors.append(f"{label}：负样本不能声明正证据")
            else:
                negatives.append(dict(case))
            continue
        if not anchors:
            errors.append(f"{label}：正样本没有证据")
            continue
        ids = set()
        for anchor in anchors:
            try:
                doc_id = (bindings or {}).get(anchor)
                if not doc_id:
                    raise CorpusValidationError(f"anchor 未绑定文档：{anchor!r}")
                ids.update(resolve(anchor, chunks, doc_id))
            except CorpusValidationError as exc:
                errors.append(f"{label}：{exc}")
        positives.append({**case, "_relevant": ids})
    if errors:
        raise CorpusValidationError("证据预检失败，未执行检索：\n" + "\n".join(errors))
    return positives, negatives


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=None)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dataset", help="标准化评测集 manifest，默认使用 beijing-v1")
    group.add_argument("--annotations", help="仅为历史复现保留的旧标注文件")
    ap.add_argument("--bindings", default=str(DEFAULT_BINDINGS), help="仅适用于旧标注")
    args = ap.parse_args()
    try:
        snapshot = load_snapshot(args.snapshot)
        chunks = fetch_chunks(args.snapshot)
        if args.annotations:
            with open(args.annotations, encoding="utf-8") as f:
                cases = [json.loads(line) for line in f if line.strip() and not line.lstrip().startswith("#")]
            positives, negatives = prepare_cases(cases, chunks, load_bindings(args.bindings))
        else:
            try:
                from .dataset import load_dataset, DEFAULT_DATASET
            except ImportError:
                from dataset import load_dataset, DEFAULT_DATASET
            dataset = load_dataset(args.dataset or DEFAULT_DATASET)
            positives, negatives = prepare_cases(dataset.cases, chunks, dataset=dataset)
    except (CorpusValidationError, OSError, ValueError) as exc:
        ap.exit(1, f"预检失败：{exc}\n")
    print(f"snapshotId={snapshot['snapshotId']}")
    print(f"文档 {snapshot['documentCount']}，chunk {snapshot['chunkCount']}；正样本 {len(positives)}，负样本 {len(negatives)}；全部证据预检通过")


if __name__ == "__main__":
    main()
