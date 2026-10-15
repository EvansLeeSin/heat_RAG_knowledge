"""阈值标定审核入口：当前公开检索响应不足以可靠标定，禁止旧的阈值推荐。

python eval/calibrate_threshold.py --audit  # 仅审核，退出0
python eval/calibrate_threshold.py          # 标定不可用，退出2，不发HTTP

说明见 eval/METRICS_GUIDE.md；不改变任何服务阈值，不增设跳过门槛的接口。
"""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .run_eval import configure_console
    from .dataset import sha256_file
except ImportError:
    from run_eval import configure_console
    from dataset import sha256_file

FINDINGS = (
    "检索结果已经过当前相似度/关键词阈值过滤，不能用它们评估更低阈值。",
    "gate.bestSimilarity仅是过滤前候选的最佳相似度，且四位小数；不是每条正确证据的原始分数。",
    "VECTOR模式仍可能插入EXACT_MATCH条款；1-score只有在COSINE_DISTANCE类型下才有相似度含义。",
    "旧脚本排除未进入top50的正样本，分母变小导致命中率虚高；top50也不是全库分数分布。",
    "空结果不能默认相似度为0：可能为阈值过滤或Embedding不可用，不能因此认定正确拒答。",
    "缺少正/负样本或全部正样本未命中时，旧min/max/median和除法还会失败。",
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("dataset_path", nargs="?", help="兼容历史参数；不会加载或检索该数据集")
    parser.add_argument("--audit", action="store_true", help="只审核可用性，不进行标定")
    args = parser.parse_args(argv)
    print("阈值标定状态：BLOCKED_POST_GATE_DATA（接口数据不满足可靠标定条件）")
    for index, finding in enumerate(FINDINGS, 1):
        print(f"{index}. {finding}")
    source = Path(__file__).resolve().parents[1] / "app/services/search.py"
    print(f"审核依据：app/services/search.py，SHA256={sha256_file(source)}")
    print("本次没有调用检索/Embedding，没有推荐阈值，也没有修改配置。")
    print("恢复标定需要另行授权获取受控、未过门槛、带scoreType和通路健康状态的原始候选数据，并固定完整分母。")
    return 0 if args.audit else 2


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
