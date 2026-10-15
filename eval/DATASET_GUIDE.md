# 第二项：标准化检索评测案例

> 本文保留第二项的案例说明；第三项执行用法见 [RUNNER_GUIDE.md](RUNNER_GUIDE.md)，第四项离线指标与阈值审核见 [METRICS_GUIDE.md](METRICS_GUIDE.md)。

## 1. 用一句话理解本次修改

第一项确定“有哪些 chunk”；第二项给每道题写清楚：**题目编号、检索条件、应该找到的原文、哪些文档不能返回**。

例如，同一个“项目甲现场处置”问题：

- 调用方有 `EVAL-A` 项目授权：应该检索到项目甲的测试资料；
- 调用方没有项目授权：该资料虽然存在，也不能返回；
- 调用方只有 `EVAL-B` 授权：不能借此看到项目甲的资料。

评测脚本现在会把每条案例的条件真正发送给检索服务。标准证据不会发送给服务，避免把答案泄漏给被测系统。

## 2. 三套独立评测集

| 评测集 | 案例 | 语料 | 用途 |
|---|---:|---:|---|
| `datasets/beijing-v1/manifest.json` | 81（K=29、S=12、N=40） | 原有3份政府HTML | 真实文本的基础检索测试 |
| `datasets/boundaries-v1/manifest.json` | 16（K=9、N=7） | 7份虚构HTML | 条款歧义、地域、类型、有效状态、项目权限、空地域与组合过滤 |
| `datasets/public-seed-beijing-v1/manifest.json` | 3（K=2、N=1） | 本机已入库3份公开业务种子HTML | 用户已逐题确认的教学开发集，附实际完整10个chunk快照 |

三套都为 `dataset_version=1.0.0`，`split=development`。**目前没有冻结验收集，不把开发数据包装成独立验收结果。** 三套使用不同文档 ID，建议使用独立测试库/部署，不混装语料。新增种子集的版本内容已固定，但不是独立验收集；默认入口仍为原81题。用法、来源、审核记录与快照见 [种子集说明](datasets/public-seed-beijing-v1/README.md)。本次仅固化案例并验证真实快照映射，不把历史教学记录冒充新的正式实验批次；没有修改业务库或重跑检索。

虚构文件位于 `corpus/fixtures/`，正文和发布状态均标明评测用途，不能进入生产知识库或作为业务规范。

## 3. 现有案例审核与修改

- `annotations.jsonl` 与 `evidence_bindings.json` 原样保留，供历史复现。
- 标准化案例存入新的 `datasets/beijing-v1/cases.jsonl`；不覆盖旧标注。
- 81条全部迁移，没有删除样本。
- 13条修订有明确原因、旧值、新值和原文件行号，见 `datasets/beijing-v1/audit.json`。
- 正样本核对本地 HTML 正文；负样本核对其属于这三份语料不能回答的域外话题。

主要修订：

| 案例 | 问题 | 修订 |
|---|---|---|
| BJ-K-005 | 问处罚机关，旧锚点也出现在禁止行为条款 | 改为第三十三条独特的处罚原文 |
| BJ-K-008 | 问协议期满处理，证据只包含主动确认 | 补齐不续签和无法联系用户的处理分支 |
| BJ-K-017 | 问完整响应程序，证据只含最初现场工作 | 缩小问题范围到4.2.1 |
| BJ-K-023 | 把工作组成员误称为指挥部成员，章节也有误 | 修正主体和定位为2.1.2 |
| BJ-K-026 | 问哪些触发情况，证据只有引导句 | 补充五类触发情况 |
| BJ-S-001 / 003 / 004 / 009 | 问题范围或主体大于证据支持范围 | 明确共用设施、队伍物资、救助条件及供热单位职责 |
| BJ-S-007 | 问个人报修后多久上报，却标供热企业预警规则 | 改为供热事故预警上报时机和对象 |
| BJ-S-008 | 问加装换热器，锚点不能直接支持该设备情形 | 改为明确受第三十三条支持的问题 |
| BJ-K-004 / BJ-S-011 | 原锚点截断为半词 | 扩展为完整合同关系表述 |

`SOURCE_CHECKED` 只表示本次本地文本对照，不代表领域专家审查、法规仍然有效或真实检索通过。原文历史较早，原有入库有效状态仍保留 `TO_VERIFY`。

新旧题目/证据口径已改变，不能把新集分数直接与旧标注分数解释为算法提升；公平对比必须使用同一评测集版本。

## 4. 案例字段

每行 JSON 为一条案例：

- `schema_version`：案例结构版本，目前1。
- `case_id`：稳定编号，如 `BJ-K-005`、`BOUNDARY-010`；后续增删题目不能重新编号旧案例。
- `dataset_version`：评测内容版本，如 `1.0.0`。
- `split`：`development` 或 `acceptance`；当前都为开发集。
- `query`：问题。
- `type`：K=关键词/条款型，S=语义型，N=预期无证据；N也包含因过滤或权限而不可用的证据，不仅是域外问题。
- `tags`：场景标签，用于后续分组比较。
- `request`：实际发送给检索服务的条件。
- `expected`：金标准和禁止返回的文档，不发送给服务。
- `review_status`：`SOURCE_CHECKED` 或 `NEEDS_REVIEW`。
- `note`：案例说明。

### 查询条件

```json
{
  "region": "北京市",
  "documentTypes": ["OPERATION_REGULATION"],
  "effectiveStatus": ["CURRENT"],
  "authorizedProjectIds": ["EVAL-A"]
}
```

规则与当前业务服务一致：

- 地域为精确匹配；`region=null` 不做地域筛选，不等于“只检索空地域”。
- 类型/有效状态空列表表示不筛选；非空列表在字段内为“或”。
- 不同字段的条件为“且”。
- 授权项目为空时，只能查询 PUBLIC 资料；非空时可以查 PUBLIC 和授权项目相交的 PROJECT 资料。
- `applicability` 提及北京不能替代 `region=北京市`。
- `authorizedProjectIds` 是可信服务调用方的授权声明，仅在测试适配中构造，不能让生产终端用户自行设置。
- 禁止在 `request` 中偷偷加入 `topK`、`retrievalMode` 等字段；这些属于实验执行配置，后续任务统一管理。

`GUIDANCE` 是旧评测入库脚本的实际文档类型，本项保留它；类型词表含义与生产规范的进一步统一不在本项修改。

### 标准证据与被排除证据

```json
{
  "behavior": "EVIDENCE",
  "evidence": [
    {
      "document_id": "doc00000000000000000000000000001",
      "anchor": "应当符合供热规划",
      "source_locator": "管理办法第七条"
    }
  ],
  "excluded_evidence": [],
  "forbidden_document_ids": [],
  "reason": "原文明确规定供热设施应符合供热规划"
}
```

- 正样本必须有 `evidence`；锚点运行时映射为 chunkId。
- `source_locator` 是供人审核的定位说明，不按它猜测或过滤 chunk，避免切分后的章节元数据不一致误杀证据。
- 可选 `version` 精确限定文档版本；未声明时使用已校验快照中的活动版本，不默认假定固定为第1版。
- 无证据案例必须 `behavior=NO_EVIDENCE` 且 `evidence=[]`。
- 因条件排除的负样本使用 `excluded_evidence`：要求目标原文实际存在于完整快照，但在该案例条件下不可访问。缺少目标文档时不能算测试通过。
- `forbidden_document_ids` 是明确禁止的命中；快照预检还会计算当前条件下不可访问的文档。

## 5. 版本清单和预检

`manifest.json` 记录评测集身份、版本、案例数量、案例文件 SHA-256，以及每份语料的原始文件 SHA-256 和入库元数据。

默认本地正文格式为 `div#mainText`。已规范化的入库HTML可以在语料 metadata 显式声明 `sourceTextFormat=html-body`，读取 body 内正文并排除 script/style/noscript；不会自动回退到整页网页。种子集因此可保留实际上传的原始文件字节与数据库 sourceContentHash，不为适配评测器改写或重新上传文档。

执行前检查：

1. JSON字段、类型、重复编号、重复问题与条件、版本一致性。
2. 案例文件和源HTML哈希。
3. 每个锚点是否存在于指定本地正文，证据与查询条件是否矛盾。
4. 实际快照文档清单、原始文件哈希和关键元数据是否与评测集一致。
5. 证据在实际 chunk 中是否全部解析成功；被排除的证据是否确实不可访问。

执行时若返回禁止文档、快照之外的文档或已变更的 chunk，将立即失败，而不是继续显示看似正常的质量分数。这是条件/数据完整性检查，没有修改 Recall、MRR、nDCG 或阈值公式。

任何待审核案例都会阻止整套评测，不默认跳过。`--inspect` 仅允许查看待审核数据，不能绕过执行预检。

## 6. 现在可以安全执行的离线检查

在项目根目录：

```bash
python eval/dataset.py
python eval/dataset.py eval/datasets/boundaries-v1/manifest.json
python -m unittest discover -s tests -v
```

这些命令不会上传语料或访问实际检索服务。

数据生成工具 `datasets/build_v1.py` 可再次运行验证生成内容一致；遇到已有不同内容会失败，不覆盖旧版本。它不是日常编辑工具，后续变更应创建新的版本目录。

## 7. 真实环境验收流程（本次未执行）

先准备只装载对应评测语料的隔离测试服务和数据库。若确需装载新语料，使用新的受控路径：

```bash
# KNOWLEDGE_MANAGEMENT_KEY 通过安全环境配置提供，不使用历史兜底密钥。
python eval/load_corpus.py \
  --dataset eval/datasets/boundaries-v1/manifest.json \
  --base http://<隔离测试服务>:8000 \
  --confirm-test-environment
```

确认标志只能要求操作者明确选择，不能技术上证明该地址不是生产环境。不要向现有业务 NAS 上传虚构语料。

接着在显式配置 `KNOWLEDGE_EVAL_DATABASE_URL` 后导出：

```bash
python eval/export_snapshot.py \
  --dataset eval/datasets/boundaries-v1/manifest.json \
  --out eval/snapshots/boundaries-001.json

python eval/corpus_map.py \
  --dataset eval/datasets/boundaries-v1/manifest.json \
  --snapshot eval/snapshots/boundaries-001.json
```

真实检索执行还需要设置匹配的 `KNOWLEDGE_BASE`、`KNOWLEDGE_QUERY_KEY`、`KNOWLEDGE_EVAL_SNAPSHOT`。本项不要求现在运行网络评测或阈值标定。

第三项已移除所有评测网络入口的默认服务地址和密钥兜底，包括历史装载模式。执行参数、逐例产物与失败规则详见 [RUNNER_GUIDE.md](RUNNER_GUIDE.md)。语料装载仍需明确确认隔离环境；不要使用业务NAS。

## 8. 开发集与验收集边界

- 当前三套都是可反复调优的开发集；版本内容固定不等于已成为验收集。
- 不复制现有题目的近义变体到验收集，制造“独立验证”的假象。
- 后续验收集需独立案例来源、领域复核与冻结版本，并处理同文档派生案例的泄漏风险。
- 不能在验收集上反复调参后仍称其为未见测试集。

## 9. 本次验证边界

使用临时 Python 3.14 环境进行本地测试；没有访问真实 PostgreSQL/NAS、执行实际上传或外部Embedding请求。原文核对针对仓库中的HTML，不等同于真实解析器产生的chunk预检。

测试中的一文档一chunk与HTTP返回均为替身，只验证结构、数据范围和条件透传，不能当作检索质量或权限系统已通过真实环境验收的结论。

后续逐项计划见 `../EVALUATION_IMPLEMENTATION_PLAN.md`。完成本项后暂停，不自动进入CLI执行器任务。
