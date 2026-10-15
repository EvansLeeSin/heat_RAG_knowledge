# 第五项：离线基线与 A/B 对比报告 V1

## 1. 定位

```text
固定题库与语料 → 正常检索接口 → run_eval.py 留档
                                   ↓
                              score_eval.py 评分
                                   ↓
                              report_eval.py 报告
```

`report_eval.py` 只读已有执行、评分及原始题库/快照，不调用 HTTP、数据库、Embedding 或 LLM，不改变 `app/` 的主流程。指标来自自己的评测工具，**不需要 Langfuse**。也不评最终 AI 回答或 Agent。

本轮交付报告工具，不自动建立真实效果基线。当前两套题库都是 development；自动化测试的接口和 chunk 为替身，不能把测试中的满分作为真实检索效果。真实基线需要另行确认隔离环境、上传/导出与运行授权。

## 2. 生成单次报告

在 `heat-knowledge-service` 根目录执行，先有第三项运行记录和第四项评分批次：

```bash
python eval/score_eval.py --run-dir "eval/runs/替换为真实运行编号" --score-id metrics-v1

python eval/report_eval.py \
  --score-dir "eval/runs/替换为真实运行编号/scores/metrics-v1" \
  --label "当前方案（操作者指定基线）" --report-id baseline-v1
```

明确选择具体评分批次，不自动找“最新”或“最好”的批次。单次报告默认分别显示批次内所有模式，不把 KEYWORD/VECTOR/HYBRID 混成一个平均分；可用 `--mode HYBRID` 只报告某模式。

没有自动冻结/验收按钮。将某次运行称为“基线”是操作者选择的参照角色，不证明配置已验收、等于生产配置或足够覆盖业务。可在 CI/实验记录中固定该运行、评分批次及哈希；不覆盖旧记录。

## 3. A/B 对比

先分别执行 A/B 并评分，然后：

```bash
python eval/report_eval.py \
  --baseline-score-dir "eval/runs/替换为A运行编号/scores/metrics-v1" \
  --baseline-mode HYBRID --baseline-label "方案A：指定基线" \
  --score-dir "eval/runs/替换为B运行编号/scores/metrics-v1" \
  --mode HYBRID --label "方案B：候选" --report-id compare-a-b
```

多模式批次必须明确选择两侧模式，避免挑最高分造成选择偏差。两侧均只有一个模式时可以省略模式参数；可以比较不同模式，但需核对共享缓存/执行顺序。不自动调整服务配置、算法或阈值。

评分与报告不需要密钥。原题库、配套原文、快照必须仍可读取。搬迁原文件时，用 `--dataset` / `--snapshot` 指定候选的新位置，用 `--baseline-dataset` / `--baseline-snapshot` 指定基线的新位置；文件哈希必须与原运行一致。不要拿新语料重解释旧响应。Windows 使用 `.venv/Scripts/python` 与对应 shell 的续行方式。

## 4. 产物与退出码

```text
eval/runs/<run_id>/                  # 原执行/评分：不修改
├── manifest.json
├── executions.jsonl
└── scores/<score_id>/
    ├── manifest.json
    ├── scores.jsonl
    └── metrics.json

eval/reports/<report_id>/            # 独立的单次或比较报告
├── manifest.json                    # 报告版本、角色、所选模式、来源/源码/产物哈希
├── summary.json                     # 完整分母、场景分组、逐题诊断/配对、可比性
└── report.md                        # 人可读配置、指标、差值、案例与限制
```

默认报告根目录为 `eval/reports/`，可用 `--output-dir` 改变；报告禁止输出到任一输入运行目录内部，避免修改输入。每次独立编号，已存在目录（包括空目录或不完整产物）拒绝覆盖。源码/输入生成期间变化、写入失败时，不发布完成清单；残留 `PREPARING` 不可用于验收。

退出码：

- `0`：报告已保存，所选模式适用的质量指标可完整计算，比较未被质量可比性规则阻断。**不是发布通过，不保证耗时可比或业务效果良好。**
- `1`：报告已保存，但有未知质量指标或质量比较受阻；须阅读原因。
- `2`：输入不可信/版本不支持/模式未明确/输出冲突/写入失败；本次未发布可信的完成报告。

`manifest.status=COMPLETED` 仅表示报告产物完成；同时检查 `quality_comparison_blocked` 和 summary 中覆盖/限制。`release_gate_evaluated=false`、`answer_quality_evaluated=false` 不会变成通过状态。

## 5. 指标与完整分母

沿用 [METRICS_GUIDE.md](./METRICS_GUIDE.md)：

- Recall@5、MRR@10、nDCG@10 越高越好；只适用于正样本。
- 正样本无证据率越低越好；负样本无证据率越高越好。
- 请求失败、未执行、未落盘尝试、评分异常均保留，不删除。
- 只要某指标有未知样本，正式均值为 `null`，其整体差值也为 `null`；不拿两侧不同成功子集均值做正式比较。
- 展示已评分数/完整分母和未知值上下界，**界限不是置信区间**。
- 请求 topK<5 或 <10，分别限制 @5 或 @10；topK 不同给出警告，只有两侧完整可算的指标才输出差值。
- 暖机排除在正式指标外，但暖机数/故障留在来源信息；标签分组可重叠，不能把分组样本数相加。
- 服务的 HTTP/协议成功不等于通路健康；声明降级不是确定故障证明。无法验证健康的向量空结果不算正确拒答，保留未知及原因。

逐题同时呈现：

- 质量：改善、退化、未变、多个指标方向相反、数据不完整、不可比或无配对；**不加权合成总分**。
- 工程：双方成功、恢复成功、候选未成功、双方未成功；恢复成功不冒充质量改善。
- 各项实际差值及未知情况，标准/返回 chunkId、首次正确证据原始排名、请求条件与状态；原始请求/响应回查原运行目录。

排名仍按返回顺序，重复 chunk 占位；不按返回 `score` 重排、不将其视为评测分。首个正确证据排名可超过10，此时 MRR@10 仍为0。报告不复制 chunk 全文或非 JSON 错误正文。

## 6. 可比性策略

先校验评分批次已完成、原运行哈希、题库与本地原文/快照、完整案例×模式网格、请求和金标准；复算逐题指标及汇总，拒绝篡改、重复、缺行和虚假分母。历史 `SCORING_ERROR` 保留未知，不用本次重算成功偷偷修复原批次。浮点复核容差为 `1e-12`，布尔不作为数字；不改变指标公式。

V1 只支持当前指标版本及同一评分源码哈希。旧实现不能自动当作同一评分口径：使用原版本的工具，或显式对两侧原执行重新评分。重评仍生成新批次，不覆盖历史。本报告工具本身的版本/哈希单独留档。

### 阻断质量差值

- 题库身份、版本、案例/清单哈希、语料清单等发生变化；
- 执行器源码不一致或评分实现口径不一致；
- 文档内容、版本、权限等证据身份发生变化；
- chunkId、文本、切分位置或标准 chunk 集合不同。

报告仍分别展示两侧原指标和全部案例；不强行对不同切分的 chunk 级 Recall 做“整体提升”结论。case_id 缺失时保留无配对项，而非只挑交集评分。

**索引方案变化本身可以是实验变量。** 文档/文本/chunk身份/标准证据相同，仅 embeddingModel、embeddingVersion、indexVersion、tokenizerVersion、hasEmbedding、hasSearchVector、tokenCount 改变时，不阻断质量比较，但显示快照/索引变化与覆盖声明。新导出仅时间/快照编号变化，而内容身份不变，也不直接阻断。

### 耗时单独限制

暖机开关不同、暖机故障、超时不同、多模式顺序/所选模式缓存位置不同、成功案例集合不同或不完整时，耗时差值标为不可比；两侧原 P50/P95 及样本数仍保留。即便条件匹配，差值也仅描述本次观测，不能宣称独立冷启动改善或统计显著。

### 无法自动证明的条件

目标 URL/配置声明变化显示警告；配置声明缺失显示警告；同一执行结果重评/自我比较提示“不是独立实验”。远端真实 commit、模型、服务端配置、硬件、负载、缓存和语料状态没有远程验证。记录字段一致**不等于**公平性证明，操作者需结合测试部署检查；建议一次只改变一个变量，在 `run_eval.py --target-config` 留下非敏感配置声明。

不根据分数断言原因、不提供 p 值或统计显著性、不自动发布。可以定位变化题目并回查配置/排名，但内部召回/过滤阶段未返回的细节仍不可见。

## 7. 安全与验证边界

报告包含问题、文档/chunkId、授权条件、目标声明及路径，仍可能敏感。默认 `eval/reports/` 已加入忽略规则；仅保存到受控目录，不默认上传 Langfuse 或提交仓库。已知环境凭据和敏感字段脱敏，不是通用 PII 检测器；不要在 label/target-config 写真实凭据。JSON 与 Markdown 均脱敏，Markdown 中的标签/问题等按数据转义，避免内容变成 HTML、图片或表格结构。若凭据字面值碰撞编号、路径、策略版本或哈希，导致脱敏会破坏清单，则中止而非发布错误身份/哈希。

新增40个单元/真实 CLI 产物集成测试；全量152个 unittest 通过，含原 chunker 断言。覆盖单次/多模式、逐题方向与多指标分歧、故障/未执行完整分母、健康歧义、暖机/缓存/超时可比性、版本/切分/题库变化、索引变化、搬迁、损坏/篡改产物、不可覆盖、脱敏及写入/输入变化失败等。

```bash
python -m unittest discover -s tests -v
python -m py_compile eval/report_eval.py tests/test_eval_reports.py
python eval/report_eval.py --help
```

验证在临时 Python 3.14.3 环境；检索和数据库为替身，无真实 NAS/PostgreSQL、语料上传、Embedding 调用或真实基线。报告工具仅用标准库；全量业务回归使用临时依赖（SQLAlchemy 2.0.36、Pydantic 2.14.1、pydantic-settings 2.15.0、httpx 0.28.1、jieba 0.42.1），**不等于项目锁定依赖或 Windows Python 3.12 已验收**，未修改 requirements.txt 或项目 .venv。

第五项代码闭环完成后暂停。后续真实基线运行、独立评测服务和 Langfuse 接入需分别授权。
