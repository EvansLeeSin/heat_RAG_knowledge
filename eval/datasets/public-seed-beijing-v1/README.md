# 北京公开业务种子开发评测集 v1.0.0

本版固化用户已逐题确认的3题（2个正样本、1个负样本）。仅 development，不是冻结验收集，不代表整体检索、生成或Agent质量合格。

## 固定内容

- `manifest.json`、`cases.jsonl`：标准化题目、条件、原文锚点与版本；不改变旧81题/16题数据集或默认入口。
- `corpus/`：实际入库的三份规范化HTML原始字节副本，哈希与数据库sourceContentHash一致。显式sourceTextFormat=html-body；不自动把官网整页当正文。
- `snapshot.json`：从本机当前三个seed-bj-*文档以只读repeatable-read导出的完整10个chunk，无向量数组或凭据；与教学时的chunk身份和正文一致。
- `gold-bindings.json`：本快照映射为2/1/0个chunk；锚点不依赖被测检索结果。
- `reviews/`、`provenance.json`：逐题提案、用户确认、来源和哈希。

## 离线校验

在项目根目录执行：

```bash
python eval/dataset.py eval/datasets/public-seed-beijing-v1/manifest.json
python eval/corpus_map.py --help
```

快照证据映射已通过prepare_cases校验；本次没有重新运行HTTP检索、调用模型或写数据库。历史教学结果仍在原walkthrough目录，不当作新基线实验。

## 限制与后续

1. 管理办法/指导意见仍为TO_VERIFY，FAQ仍为REFERENCE_ONLY；地区北京市、PUBLIC、无项目授权。网页时间不冒充数据库发文日期。
2. 第三题的NO_EVIDENCE只针对这3份资料，不证明现实无小程序。补充相关资料后必须新建数据集/快照版本并重新审核，不修改本版本。
3. 原始问题与query改写不是同一个实验变量；heat-parent可能改写查询并截断片段，需另存实际LLM输入和工具记录。
4. 这版仅固化检索预期，尚未标注或验收最终回答、引用、工具流程。不得把前两题检索满分视作回答满分。
5. 不要在当前业务库混装旧81题语料、清库或因教学结果调阈值。真实批次执行和heat-parent改造另行确认。
6. 调用API的URL和密钥不放题库；run_eval需显式指定本manifest和snapshot，不改变工具原默认项。
