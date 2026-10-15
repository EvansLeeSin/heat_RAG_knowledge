"""版本化检索评测集的严格加载器；仅本地校验，无网络或数据库访问。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

DEFAULT_DATASET = Path(__file__).parent / "datasets/beijing-v1/manifest.json"
REQUEST_KEYS = {"region", "documentTypes", "effectiveStatus", "authorizedProjectIds"}
STATUSES = {"CURRENT", "TO_VERIFY", "SUPERSEDED", "REFERENCE_ONLY"}
# GUIDANCE 是现有评测入库脚本使用的历史值；服务检索按实际字符串过滤。
DOCUMENT_TYPES = {"EMERGENCY_PLAN", "MANAGEMENT_MEASURE", "OPERATION_REGULATION", "EQUIPMENT_MANUAL", "OTHER", "GUIDANCE"}


class DatasetValidationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetValidationError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(f"{path.name} JSON 格式错误：{exc.msg}") from exc


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _strings(value, nonempty=False) -> bool:
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(_text(item) for item in value) and len(value) == len(set(value)))


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise DatasetValidationError("request 必须明确包含且仅包含 region/documentTypes/effectiveStatus/authorizedProjectIds")
    if request["region"] is not None and not _text(request["region"]):
        raise DatasetValidationError("region 必须为非空字符串或 null")
    for key in REQUEST_KEYS - {"region"}:
        if not _strings(request[key]):
            raise DatasetValidationError(f"request.{key} 必须为无重复的非空字符串列表（允许 []）")
    if not set(request["effectiveStatus"]) <= STATUSES:
        raise DatasetValidationError("effectiveStatus 包含未知状态")
    if not set(request["documentTypes"]) <= DOCUMENT_TYPES:
        raise DatasetValidationError("documentTypes 包含未知类型")
    return request


def validate_case(case: dict) -> dict:
    required = {"schema_version", "case_id", "dataset_version", "split", "query", "type", "tags",
                "request", "expected", "review_status", "note"}
    if not isinstance(case, dict) or set(case) != required:
        raise DatasetValidationError("案例字段缺失或未知；请使用标准化案例，不会自动猜测旧标注含义")
    if type(case["schema_version"]) is not int or case["schema_version"] != 1:
        raise DatasetValidationError("不支持的案例 schema_version")
    if not _text(case["case_id"]) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,63}", case["case_id"]):
        raise DatasetValidationError("case_id 格式无效")
    if not isinstance(case["dataset_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", case["dataset_version"]):
        raise DatasetValidationError("dataset_version 应为明确的 x.y.z 版本")
    if case["split"] not in {"development", "acceptance"} or case["type"] not in {"K", "S", "N"}:
        raise DatasetValidationError("split/type 无效")
    if not _text(case["query"]) or not _strings(case["tags"], nonempty=True) or not _text(case["note"]):
        raise DatasetValidationError("query/tags/note 无效")
    if case["review_status"] not in {"SOURCE_CHECKED", "NEEDS_REVIEW"}:
        raise DatasetValidationError("review_status 无效")
    validate_request(case["request"])
    expected = case["expected"]
    if not isinstance(expected, dict) or set(expected) != {"behavior", "evidence", "excluded_evidence", "forbidden_document_ids", "reason"}:
        raise DatasetValidationError("expected 字段缺失或未知")
    behavior = expected["behavior"]
    if behavior != ("NO_EVIDENCE" if case["type"] == "N" else "EVIDENCE"):
        raise DatasetValidationError("预期行为与 K/S/N 类型不一致")
    if not _strings(expected["forbidden_document_ids"]) or not _text(expected["reason"]):
        raise DatasetValidationError("禁止文档清单或预期理由无效")
    for key in ("evidence", "excluded_evidence"):
        evidence = expected[key]
        if not isinstance(evidence, list):
            raise DatasetValidationError(f"{key} 必须为列表")
        seen = set()
        for item in evidence:
            if not isinstance(item, dict) or not {"document_id", "anchor", "source_locator"} <= set(item) or set(item) - {"document_id", "anchor", "source_locator", "version"}:
                raise DatasetValidationError("证据必须明确包含 document_id/anchor/source_locator，可选 version")
            if not all(_text(item[k]) for k in ("document_id", "anchor", "source_locator")) or len(item["document_id"]) > 32:
                raise DatasetValidationError("证据身份、原文或定位说明为空，或文档 ID 超过数据库 32 字符上限")
            if "version" in item and (type(item["version"]) is not int or item["version"] < 1):
                raise DatasetValidationError("证据 version 无效")
            identity = (item["document_id"], item["anchor"], item.get("version"))
            if identity in seen:
                raise DatasetValidationError("证据重复")
            seen.add(identity)
    if behavior == "EVIDENCE" and (not expected["evidence"] or expected["excluded_evidence"]):
        raise DatasetValidationError("正样本必须有标准证据，不能声明被排除证据")
    if behavior == "NO_EVIDENCE" and expected["evidence"]:
        raise DatasetValidationError("无证据案例不能声明可用标准证据")
    if any(e["document_id"] in expected["forbidden_document_ids"] for e in expected["evidence"]):
        raise DatasetValidationError("标准证据文档同时被禁止")
    return case


def document_allowed(metadata: dict, request: dict) -> bool:
    """镜像现有服务的文档范围规则，仅用于金标准合法性校验。"""
    scope = metadata.get("accessScope")
    if scope == "PUBLIC":
        pass
    elif scope == "PROJECT":
        if not set(metadata.get("projectIds", [])) & set(request["authorizedProjectIds"]):
            return False
    else:
        return False
    return ((request["region"] is None or metadata.get("region") == request["region"])
            and (not request["documentTypes"] or metadata.get("documentType") in request["documentTypes"])
            and (not request["effectiveStatus"] or metadata.get("effectiveStatus") in request["effectiveStatus"]))


class _MainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.main_depth = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self.div_depth += 1
            if dict(attrs).get("id") == "mainText":
                self.main_depth = self.div_depth

    def handle_endtag(self, tag):
        if tag == "div":
            if self.div_depth == self.main_depth:
                self.main_depth = None
            self.div_depth -= 1

    def handle_data(self, data):
        if self.main_depth is not None:
            self.parts.append(data)


class _BodyText(HTMLParser):
    """仅供显式声明的规范化 HTML；不把原始网页自动当作正文。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.hidden_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_body = True
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)

    def handle_data(self, data):
        if self.in_body and not self.hidden_depth:
            self.parts.append(data)


def source_text(path: Path, *, text_format: str = "mainText") -> str:
    if not isinstance(text_format, str) or text_format not in {"mainText", "html-body"}:
        raise DatasetValidationError(f"未知 sourceTextFormat：{text_format}")
    parser = _MainText() if text_format == "mainText" else _BodyText()
    parser.feed(path.read_text(encoding="utf-8"))
    if not parser.parts:
        raise DatasetValidationError(f"{path.name} 缺少 {text_format} 正文")
    return "".join(parser.parts)


@dataclass
class Dataset:
    path: Path
    manifest: dict
    cases: list[dict]

    def corpus_items(self) -> list[dict]:
        return [{"key": d["metadata"]["documentId"], "file": str((self.path.parent / d["file"]).resolve()),
                 "metadata": deepcopy(d["metadata"])} for d in self.manifest["corpus"]]


def load_dataset(path: str | Path = DEFAULT_DATASET, *, require_ready: bool = True) -> Dataset:
    path = Path(path).resolve()
    manifest = read_json(path)
    required = {"schema_version", "dataset_id", "dataset_version", "split", "description", "case_file",
                "cases_sha256", "case_count", "corpus"}
    if not isinstance(manifest, dict) or set(manifest) != required or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise DatasetValidationError("评测集 manifest 格式无效")
    if not _text(manifest["dataset_id"]) or not _text(manifest["description"]):
        raise DatasetValidationError("评测集身份或说明无效")
    if not _text(manifest["case_file"]) or type(manifest["case_count"]) is not int or manifest["case_count"] < 1:
        raise DatasetValidationError("案例文件或数量无效")
    case_path = path.parent / manifest["case_file"]
    if sha256_file(case_path) != manifest["cases_sha256"]:
        raise DatasetValidationError("案例文件哈希不匹配，请创建新版本而不是修改旧版")
    cases = []
    for number, line in enumerate(case_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            case = json.loads(line, object_pairs_hook=_object)
            validate_case(case)
        except ValueError as exc:
            raise DatasetValidationError(f"{case_path.name}:{number}：{exc}") from exc
        cases.append(case)
    if len(cases) != manifest["case_count"]:
        raise DatasetValidationError("案例数量不一致")
    ids, inputs = set(), set()
    for case in cases:
        if case["case_id"] in ids:
            raise DatasetValidationError(f"case_id 重复：{case['case_id']}")
        ids.add(case["case_id"])
        identity = (case["query"], json.dumps(case["request"], sort_keys=True, ensure_ascii=False))
        if identity in inputs:
            raise DatasetValidationError(f"问题和查询条件重复：{case['case_id']}")
        inputs.add(identity)
        if case["dataset_version"] != manifest["dataset_version"] or case["split"] != manifest["split"]:
            raise DatasetValidationError(f"案例版本或 split 与 manifest 不一致：{case['case_id']}")
        if require_ready and case["review_status"] != "SOURCE_CHECKED":
            raise DatasetValidationError(f"案例待审核，整个评测集不可执行：{case['case_id']}")
    docs, texts = {}, {}
    if not isinstance(manifest["corpus"], list) or not manifest["corpus"]:
        raise DatasetValidationError("必须声明完整评测语料清单")
    for entry in manifest["corpus"]:
        if not isinstance(entry, dict) or set(entry) != {"file", "sha256", "metadata"}:
            raise DatasetValidationError("语料清单字段无效")
        metadata = entry["metadata"]
        if not isinstance(metadata, dict) or not _text(metadata.get("documentId")) or len(metadata["documentId"]) > 32 or not _text(metadata.get("title")):
            raise DatasetValidationError("语料必须声明文档 ID 和标题")
        if not _text(metadata.get("documentType")) or metadata.get("effectiveStatus") not in STATUSES:
            raise DatasetValidationError("语料类型或有效状态无效")
        if metadata.get("region") is not None and not _text(metadata["region"]):
            raise DatasetValidationError("语料地域无效")
        if metadata.get("accessScope") not in {"PUBLIC", "PROJECT"} or not _strings(metadata.get("projectIds")):
            raise DatasetValidationError("语料权限元数据无效")
        if metadata["accessScope"] == "PROJECT" and not metadata["projectIds"]:
            raise DatasetValidationError("项目语料缺少 projectIds")
        doc_id = metadata["documentId"]
        if doc_id in docs:
            raise DatasetValidationError("语料文档 ID 重复")
        file_path = path.parent / entry["file"]
        if sha256_file(file_path) != entry["sha256"]:
            raise DatasetValidationError(f"语料文件哈希不匹配：{doc_id}")
        docs[doc_id], texts[doc_id] = metadata, source_text(
            file_path, text_format=metadata.get("sourceTextFormat", "mainText"))
    for case in cases:
        expected, request = case["expected"], case["request"]
        if not set(expected["forbidden_document_ids"]) <= set(docs):
            raise DatasetValidationError(f"禁止文档不在语料清单中：{case['case_id']}")
        for kind in ("evidence", "excluded_evidence"):
            for evidence in expected[kind]:
                doc_id = evidence["document_id"]
                if doc_id not in docs or evidence["anchor"] not in texts[doc_id]:
                    raise DatasetValidationError(f"原文证据未在指定本地文档中找到：{case['case_id']} / {doc_id}")
                allowed = document_allowed(docs[doc_id], request)
                if allowed != (kind == "evidence"):
                    raise DatasetValidationError(f"证据可用性与查询条件矛盾：{case['case_id']} / {doc_id}")
    return Dataset(path, manifest, deepcopy(cases))


def validate_snapshot_corpus(dataset: Dataset, chunks: list[dict]) -> None:
    docs = {c["documentId"]: c.get("documentMetadata", c) for c in chunks}
    required_ids = {d["metadata"]["documentId"] for d in dataset.manifest["corpus"]}
    if set(docs) != required_ids:
        raise DatasetValidationError("快照文档清单与评测集不一致；请在独立测试语料环境重新导出")
    fields = ("title", "region", "documentType", "effectiveStatus", "accessScope", "projectIds")
    for entry in dataset.manifest["corpus"]:
        metadata = entry["metadata"]
        actual = docs[metadata["documentId"]]
        if actual.get("sourceContentHash") != entry["sha256"]:
            raise DatasetValidationError(f"快照原始文件哈希与评测集不一致：{metadata['documentId']}")
        for field in fields:
            if actual.get(field) != metadata.get(field):
                raise DatasetValidationError(f"快照文档元数据与评测集不一致：{metadata['documentId']} / {field}")


def load_cases(path: str | Path = DEFAULT_DATASET) -> list[dict]:
    return load_dataset(path).cases


def request_body(case: dict, top_k: int, mode: str) -> dict:
    validate_case(case)
    return {"query": case["query"], **deepcopy(case["request"]), "topK": top_k, "retrievalMode": mode}


def check_result_contract(case: dict, results: list[dict]) -> None:
    """禁止文档命中是硬失败，不改 Recall/MRR 等公式。"""
    if "_corpus_document_ids" in case:
        docs, chunks = set(case["_corpus_document_ids"]), set(case["_corpus_chunk_ids"])
        if any(r.get("documentId") not in docs or r.get("chunkId") not in chunks for r in results):
            raise DatasetValidationError(f"{case['case_id']} 返回快照外文档或 chunk；请检查测试环境及活动版本")
    forbidden = set(case["expected"]["forbidden_document_ids"]) | set(case.get("_scope_forbidden", []))
    bad = sorted({r.get("documentId") for r in results if r.get("documentId") in forbidden})
    if bad:
        raise DatasetValidationError(f"{case['case_id']} 返回禁止文档：{', '.join(bad)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?", default=str(DEFAULT_DATASET))
    ap.add_argument("--inspect", action="store_true", help="允许查看待审核案例，执行评测仍会阻止")
    args = ap.parse_args()
    try:
        dataset = load_dataset(args.manifest, require_ready=not args.inspect)
    except (OSError, ValueError) as exc:
        ap.exit(1, f"评测集校验失败：{exc}\n")
    print(f"{dataset.manifest['dataset_id']} v{dataset.manifest['dataset_version']}：{len(dataset.cases)} 条，split={dataset.manifest['split']}；格式、文件哈希和本地原文校验通过（不代表真实检索已通过）")


if __name__ == "__main__":
    main()
