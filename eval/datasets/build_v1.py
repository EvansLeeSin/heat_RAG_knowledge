"""一次性、可重复的 V1 评测数据迁移；只生成本地文件，不覆盖已有不同内容。

保留 annotations.jsonl 原样。修订问题/证据全部写入 audit.json，禁止隐藏删样本。
后续修改应创建新版本，不能修改本脚本后覆盖已发布 V1。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from eval.corpus_map import load_bindings
from eval.dataset import sha256_file, document_allowed, load_dataset
from eval.load_corpus import CORPUS

VERSION = "1.0.0"
EMPTY_REQUEST = {"region": None, "documentTypes": [], "effectiveStatus": [], "authorizedProjectIds": []}
PENALTY = "由城市管理综合行政执法机关责令限期改正，逾期未改正的，可以处500元以上5000元以下罚款"
CONTRACT = "用户与供热单位之间视为存在事实供热采暖合同关系"
CHANGES = {
    "BJ-K-004": {"anchors": [CONTRACT], "reason": "原锚点截断为半词，扩展为完整合同关系表述"},
    "BJ-K-005": {"anchors": [PENALTY], "reason": "原锚点同时出现在第二十一条和第三十三条；改为能支持处罚机关的独特原文"},
    "BJ-K-008": {"anchors": ["主动与用户确认是否有恢复供热需求", "供热单位应恢复正常供热", "按照继续暂停供热执行协议"], "reason": "问题涉及协议期满处理，补齐不续签及无法联系用户的分支"},
    "BJ-K-017": {"query": "供热突发公共事件响应程序中，现场应先做哪些了解、调解和控制工作", "note": "应急预案4.2.1", "reason": "原问题覆盖完整响应程序，但证据只支持初始现场处置；缩小问题范围"},
    "BJ-K-023": {"query": "市供热突发公共事件应急工作组成员单位有哪些", "anchors": ["市供热突发公共事件应急工作组成员单位包括"], "note": "应急预案2.1.2", "reason": "原问题混淆指挥部与工作组，且章节误记为2.2"},
    "BJ-K-026": {"anchors": ["应立即分析判断影响正常供热的可能性", "因供热设施、设备发生故障可能影响正常供热", "因供热燃料出现短缺可能影响正常供热", "因供电、供水系统发生故障可能影响正常供热", "因天气持续低温可能影响正常供热", "因其它自然灾害可能影响正常供热"], "reason": "问题问哪些情况，原证据只含引导句；补齐五类触发情况"},
    "BJ-S-001": {"query": "楼里的共用暖气管道坏了，维修责任由谁承担？", "reason": "家里暖气不热可能涉及自用设施或温度不达标；明确限定共用设施"},
    "BJ-S-003": {"query": "热力公司遇到供热事故，要准备哪些抢修人员和物资？", "reason": "原问题问事故由谁组织，证据实际规定供热单位的队伍与物资保障"},
    "BJ-S-004": {"query": "困难住户集中、欠费严重，热力公司无法正常供暖时，政府怎么兜底？", "reason": "补充特殊群体集中、无法保障正常供热等救助条件，避免扩大为所有欠费用户"},
    "BJ-S-007": {"query": "供热事故可能影响正常供暖，如果需要上报，应什么时候报、报给谁？", "anchors": ["如需上报，要立即将发生事件时间、地点、性质、可能影响的程度、影响时间以及应对措施报市供热应急办"], "reason": "原问题为个人报修后无人处理，证据是区县及供热企业预警上报规则，两者主体不一致"},
    "BJ-S-008": {"query": "住户拆改共用暖气设施、加大采暖面积，可能由谁责令改正？", "anchors": [PENALTY], "reason": "原问题问加装换热器，原证据未直接支持该设备情形；改为明确受第三十三条支持的问题"},
    "BJ-S-009": {"query": "居民楼室外供热管道和室内共用设施，通常由热力公司承担哪些责任？", "reason": "原问题要求物业与热力公司完整责任划分，第二十条只直接说明供热单位职责"},
    "BJ-S-011": {"anchors": [CONTRACT], "reason": "与K-004一致，扩展为完整合同关系表述"},
}


def evidence(doc_id, anchor, locator):
    return {"document_id": doc_id, "anchor": anchor, "source_locator": locator}


def make_case(case_id, query, kind, tags, request, proofs, note, *, excluded=None, forbidden=None):
    return {"schema_version": 1, "case_id": case_id, "dataset_version": VERSION, "split": "development",
            "query": query, "type": kind, "tags": tags, "request": deepcopy(request),
            "expected": {"behavior": "NO_EVIDENCE" if kind == "N" else "EVIDENCE", "evidence": proofs,
                         "excluded_evidence": excluded or [], "forbidden_document_ids": forbidden or [],
                         "reason": note}, "review_status": "SOURCE_CHECKED", "note": note}


def save_new(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content.encode("utf-8"):
            raise ValueError(f"禁止覆盖已存在的不同内容：{path}；请创建新版本")
        return
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write(content)


def publish(directory, dataset_id, cases, items, description):
    body = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
    save_new(directory / "cases.jsonl", body)
    corpus = []
    for item in items:
        file_path = ROOT.parent / item["file"]
        corpus.append({"file": "../../corpus/" + file_path.relative_to(ROOT / "corpus").as_posix(),
                       "sha256": sha256_file(file_path), "metadata": item["metadata"]})
    manifest = {"schema_version": 1, "dataset_id": dataset_id, "dataset_version": VERSION,
                "split": "development", "description": description, "case_file": "cases.jsonl",
                "cases_sha256": sha256_file(directory / "cases.jsonl"), "case_count": len(cases), "corpus": corpus}
    save_new(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    load_dataset(directory / "manifest.json")


def build_beijing():
    bindings = load_bindings()
    rows = []
    for number, line in enumerate((ROOT / "annotations.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.startswith("#"):
            rows.append((number, json.loads(line)))
    cases, audit, counters = [], [], Counter()
    for line_number, original in rows:
        old = deepcopy(original)
        kind = old["type"]
        counters[kind] += 1
        case_id = f"BJ-{kind}-{counters[kind]:03d}"
        change = CHANGES.get(case_id, {})
        query = change.get("query", old["query"])
        anchors = change.get("anchors", old["anchors"])
        note = change.get("note", old["note"])
        # 修订锚点仍属于原案例的文档，不按文本相似度猜测新归属。
        doc_id = bindings[old["anchors"][0]] if old["anchors"] else None
        proofs = [evidence(doc_id, anchor, note) for anchor in anchors]
        tags = [{"K": "keyword", "S": "semantic", "N": "out_of_domain"}[kind]]
        cases.append(make_case(case_id, query, kind, tags, EMPTY_REQUEST, proofs, note))
        after = {"query": query, "type": kind, "anchors": anchors, "note": note}
        audit.append({"case_id": case_id, "legacy_line": line_number, "status": "SOURCE_CHECKED",
                      "action": "REVISED" if change else "MIGRATED_UNCHANGED", "before": old, "after": after,
                      "reason": change.get("reason", "原问题及锚点保留；正样本核对指定正文，负样本为本语料范围外话题")})
    directory = Path(__file__).parent / "beijing-v1"
    items = deepcopy(CORPUS)
    publish(directory, "heat-retrieval-beijing", cases, items,
            "81条历史案例的可追溯迁移，13条修订；仅开发集，非冻结验收集；文本核对不是法规有效性审查。")
    report = {"legacy_file": "eval/annotations.jsonl", "legacy_sha256": sha256_file(ROOT / "annotations.jsonl"),
              "original_count": len(rows), "migrated_count": len(cases), "removed_count": 0,
              "revised_count": len(CHANGES), "review_scope": "本地HTML正文对照；无真实数据库/检索或领域专家验收",
              "cases": audit}
    save_new(directory / "audit.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def build_boundaries():
    definitions = [
        ("beijing_current", "evalfixture000000000000000000001", "评测专用：北京现场核查规程", "北京市", "CURRENT", "OPERATION_REGULATION", "PUBLIC", [], "北京核查记录必须填写设备编号、现场压力和隔离阀位置"),
        ("shanghai_current", "evalfixture000000000000000000002", "评测专用：上海现场核查规程", "上海市", "CURRENT", "OPERATION_REGULATION", "PUBLIC", [], "上海核查记录必须填写管段编号、现场流量和旁通阀位置"),
        ("beijing_superseded", "evalfixture000000000000000000003", "评测专用：北京现场核查旧规程", "北京市", "SUPERSEDED", "OPERATION_REGULATION", "PUBLIC", [], "旧版北京核查记录采用纸质台账并填写值班人姓名"),
        ("beijing_manual", "evalfixture000000000000000000004", "评测专用：北京热表设备说明", "北京市", "CURRENT", "EQUIPMENT_MANUAL", "PUBLIC", [], "热表设备检修记录填写仪表序列号、电池状态和显示读数"),
        ("project_a", "evalfixture000000000000000000005", "评测专用：项目甲现场处置资料", "北京市", "CURRENT", "OPERATION_REGULATION", "PROJECT", ["EVAL-A"], "项目甲现场处置必须核对甲区阀门编号并登记甲区作业单"),
        ("project_b", "evalfixture000000000000000000006", "评测专用：项目乙现场处置资料", "北京市", "CURRENT", "OPERATION_REGULATION", "PROJECT", ["EVAL-B"], "项目乙现场处置必须核对乙区旁通阀编号并登记乙区复核单"),
        ("null_region", "evalfixture000000000000000000007", "评测专用：地域未标注资料", None, "CURRENT", "OPERATION_REGULATION", "PUBLIC", [], "地域未标注的核查记录采用双人复核并登记交接单"),
    ]
    items, proofs = [], {}
    for key, doc_id, title, region, status, doc_type, scope, projects, anchor in definitions:
        items.append({"file": f"eval/corpus/fixtures/{key}.html", "metadata": {
            "documentId": doc_id, "title": title, "publisher": "虚构评测资料",
            "documentType": doc_type, "region": region, "applicability": region or "北京市",
            "effectiveStatus": status, "publicationStatus": "TEST_ONLY", "accessScope": scope, "projectIds": projects}})
        proofs[key] = evidence(doc_id, anchor, "第十条（虚构评测条款）")
    cases = []

    def add(key, query, tags, region=None, types=None, statuses=None, projects=None, negative=False):
        request = {"region": region, "documentTypes": types or [], "effectiveStatus": statuses or [],
                   "authorizedProjectIds": projects or []}
        forbidden = [item["metadata"]["documentId"] for item in items if not document_allowed(item["metadata"], request)]
        note = "条件排除目标证据，应无证据；目标文档仍须存在于快照" if negative else "条件内应命中指定虚构条款"
        proof = deepcopy(proofs[key])
        cases.append(make_case(f"BOUNDARY-{len(cases)+1:03d}", query, "N" if negative else "K", tags,
                               request, [] if negative else [proof], note,
                               excluded=[proof] if negative else [], forbidden=forbidden))

    # 不加地域/类型/状态过滤，真实暴露相同条款号下的文档身份和排序歧义。
    add("beijing_current", "评测专用北京现场核查规程第十条要求记录什么", ["article_ambiguity"])
    add("shanghai_current", "评测专用上海现场核查规程第十条要求记录什么", ["article_ambiguity"])
    add("beijing_current", "京检01 北京核查记录需要填写什么", ["region_filter"], "北京市", statuses=["CURRENT"])
    add("shanghai_current", "沪检02 上海核查记录需要填写什么", ["region_filter", "excluded_evidence"], "北京市", statuses=["CURRENT"], negative=True)
    add("beijing_superseded", "旧检03 旧版北京核查记录采用什么台账", ["status_filter"], "北京市", statuses=["SUPERSEDED"])
    add("beijing_superseded", "旧检03 旧版北京核查记录采用什么台账", ["status_filter", "excluded_evidence"], "北京市", statuses=["CURRENT"], negative=True)
    add("beijing_manual", "表检04 热表设备检修记录填写什么", ["type_filter"], "北京市", ["EQUIPMENT_MANUAL"], ["CURRENT"])
    add("beijing_manual", "表检04 热表设备检修记录填写什么", ["type_filter", "excluded_evidence"], "北京市", ["OPERATION_REGULATION"], ["CURRENT"], negative=True)
    add("project_a", "甲检05 项目甲现场处置需要核对什么", ["project_scope"], projects=["EVAL-A"])
    add("project_a", "甲检05 项目甲现场处置需要核对什么", ["project_scope", "public_only"], negative=True)
    add("project_a", "甲检05 项目甲现场处置需要核对什么", ["project_scope", "cross_project"], projects=["EVAL-B"], negative=True)
    add("project_b", "乙检06 项目乙现场处置需要核对什么", ["project_scope"], projects=["EVAL-B"])
    add("project_b", "乙检06 项目乙现场处置需要核对什么", ["project_scope", "cross_project"], projects=["EVAL-A"], negative=True)
    add("null_region", "无域07 地域未标注的核查记录采用什么复核方式", ["null_region"])
    add("null_region", "无域07 地域未标注的核查记录采用什么复核方式", ["null_region", "region_filter"], "北京市", negative=True)
    add("beijing_current", "京检01 北京核查记录需要填写什么", ["combined_filters"], "北京市", ["OPERATION_REGULATION"], ["CURRENT"], ["EVAL-A"])
    publish(Path(__file__).parent / "boundaries-v1", "heat-retrieval-boundaries", cases, items,
            "7份独立虚构语料、16条边界案例；仅开发集，必须与真实业务语料隔离；不代表真实检索已通过。")


if __name__ == "__main__":
    build_beijing()
    build_boundaries()
    print("V1数据生成/一致性校验完成；未上传语料，未执行检索")
