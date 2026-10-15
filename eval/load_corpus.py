"""语料装载脚本：把 eval/corpus 下的 HTML 上传到知识服务并等待处理完成。

用途：把「上传 → 解析 → 切分 → 向量化 → 版本激活」这条链路变成可重复执行的步骤，
使评估（run_eval.py）可以在任意环境（本地 Compose / NAS）重建同一语料。

用法：
    # 管理密钥通过 KNOWLEDGE_MANAGEMENT_KEY 提供，必须确认隔离测试环境。
    python eval/load_corpus.py --base http://localhost:8000 --confirm-test-environment
    python eval/load_corpus.py --base http://localhost:8000 --confirm-test-environment --only banfa

注意：multipart 必须显式用 UTF-8 编码。Windows GBK 控制台直接拼接中文表单字节
会导致标题乱码（服务端本身无问题），因此本脚本统一走显式 UTF-8 编码。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
import uuid

try:
    from .http_client import ConfigurationError, NoRedirectHandler, Redactor, validate_base, validate_api_key
except ImportError:
    from http_client import ConfigurationError, NoRedirectHandler, Redactor, validate_base, validate_api_key

# 固定 documentId：保证重复装载是「同文档新版本」而不是不断产生新文档，
# 同时顺带验证版本切换（上传前该文档无记录时版本从 1 开始）。
CORPUS = [
    {
        "key": "banfa",
        "file": "eval/corpus/beijing_gongre_guanli_banfa.html",
        "metadata": {
            "documentId": "doc00000000000000000000000000001",
            "title": "北京市供热采暖管理办法",
            "publisher": "北京市人民政府",
            "sourceUrl": "https://www.beijing.gov.cn/zhengce/zhengcefagui/",
            "documentType": "MANAGEMENT_MEASURE",
            "region": "北京市",
            "applicability": "北京市",
            "effectiveStatus": "TO_VERIFY",
            "publicationStatus": "PUBLISHED",
            "accessScope": "PUBLIC",
            "projectIds": [],
        },
    },
    {
        "key": "zanting",
        "file": "eval/corpus/beijing_zanting_huifu.html",
        "metadata": {
            "documentId": "doc00000000000000000000000000002",
            "title": "北京市居民集中供热暂停和恢复供热指导意见",
            "publisher": "北京市城市管理委员会",
            "sourceUrl": "https://csglw.beijing.gov.cn/",
            "documentType": "GUIDANCE",
            "region": "北京市",
            "applicability": "北京市",
            "effectiveStatus": "TO_VERIFY",
            "publicationStatus": "PUBLISHED",
            "accessScope": "PUBLIC",
            "projectIds": [],
        },
    },
    {
        "key": "yingji",
        "file": "eval/corpus/beijing_yingji_yuan.html",
        "metadata": {
            "documentId": "doc00000000000000000000000000003",
            "title": "北京市供热突发公共事件应急预案",
            "publisher": "北京市城市管理委员会",
            "sourceUrl": "https://csglw.beijing.gov.cn/",
            "documentType": "EMERGENCY_PLAN",
            "region": "北京市",
            "applicability": "北京市",
            "effectiveStatus": "TO_VERIFY",
            "publicationStatus": "PUBLISHED",
            "accessScope": "PUBLIC",
            "projectIds": [],
        },
    },
]


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes, mime: str):
    boundary = "----knowledge" + uuid.uuid4().hex
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(value.encode("utf-8") + b"\r\n")
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    lines.append(f"Content-Type: {mime}\r\n\r\n".encode())
    lines.append(data + b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(lines)


def _request(url: str, key: str, body: bytes | None = None, content_type: str | None = None):
    headers = {"X-API-Key": key}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.build_opener(NoRedirectHandler()).open(req, timeout=180) as r:
        return json.loads(r.read())


def upload(base: str, item: dict, key: str) -> dict:
    with open(item["file"], "rb") as f:
        data = f.read()
    fields = {"metadata": json.dumps(item["metadata"], ensure_ascii=False)}
    boundary, body = _multipart(fields, "file", os.path.basename(item["file"]), data, "text/html")
    return _request(
        f"{base}/knowledge/ingestion-jobs",
        key,
        body,
        f"multipart/form-data; boundary={boundary}",
    )


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--base", default=None)
    ap.add_argument("--key-env", default="KNOWLEDGE_MANAGEMENT_KEY", help="管理密钥环境变量名，不接受明文密钥参数")
    ap.add_argument("--dataset", help="标准化评测集 manifest，上传其完整语料清单")
    ap.add_argument("--confirm-test-environment", action="store_true", help="确认目标是与生产隔离的测试环境")
    ap.add_argument("--only", default=None, help="只装载某个 key（banfa/zanting/yingji）")
    ap.add_argument("--timeout", type=int, default=900, help="单文档等待上限（秒）")
    args = ap.parse_args()

    if not args.confirm_test_environment:
        ap.error("装载语料必须加 --confirm-test-environment 确认隔离测试环境")
    try:
        args.base = validate_base(args.base if args.base is not None else os.environ.get("KNOWLEDGE_BASE"))
    except ConfigurationError as exc:
        ap.error(str(exc))
    try:
        args.key = validate_api_key(os.environ.get(args.key_env))
    except ConfigurationError as exc:
        ap.error(str(exc))
    redactor = Redactor(args.key, os.environ.get("KNOWLEDGE_QUERY_KEY", ""))
    if args.dataset:
        try:
            try:
                from .dataset import load_dataset
            except ImportError:
                from dataset import load_dataset
            corpus = load_dataset(args.dataset).corpus_items()
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
    else:
        corpus = CORPUS
    targets = [c for c in corpus if not args.only or c["key"] == args.only]
    if not targets:
        ap.error("没有匹配的语料，未发出上传请求")
    print(f"目标服务：{args.base}    文档数：{len(targets)}\n")

    accepted = []
    for item in targets:
        resp = upload(args.base, item, args.key)
        accepted.append((item, resp))
        print(
            f"[上传] {item['metadata']['title']:<30} "
            f"job={resp['jobId'][:8]} doc={resp['documentId'][:8]} ver={resp['documentVersion']} "
            f"status={resp['status']}"
        )

    print("\n等待处理完成...")
    for item, resp in accepted:
        job_id = resp["jobId"]
        deadline = time.time() + args.timeout
        last = None
        while time.time() < deadline:
            st = _request(f"{args.base}/knowledge/ingestion-jobs/{job_id}", args.key)
            status = st.get("status")
            if status != last:
                print(f"  {item['key']:<8} -> {status}")
                last = status
            if status in ("SUCCEEDED", "FAILED"):
                if status == "FAILED":
                    print(redactor.text(f"    ❌ error={st.get('errorCode')} {st.get('errorMessage')}"))
                break
            time.sleep(3)
        else:
            print(f"  {item['key']:<8} -> 超时（{args.timeout}s）")

    print("\n装载流程结束。请先核对任务状态、导出快照，再按 eval/RUNNER_GUIDE.md 配置执行。")


if __name__ == "__main__":
    main()
