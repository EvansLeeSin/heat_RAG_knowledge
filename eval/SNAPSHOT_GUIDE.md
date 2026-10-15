# 第一项：离线语料快照与证据预检

> 本文保留第一项的背景与历史用法。第二项已引入标准化评测集：当前默认输入、证据格式和推荐操作请看 [DATASET_GUIDE.md](DATASET_GUIDE.md)。侧车绑定仅用于历史标注兼容。第四项已新增离线评分，并停止不可靠的阈值推荐：见 [METRICS_GUIDE.md](METRICS_GUIDE.md)。

## 1. 本项解决什么问题

旧流程通过多个 VECTOR topK 结果并集推测全量 chunk：正确证据可能漏掉，且标注是否有效反过来依赖被测检索器。未匹配标注还可能被排除，令分数偏高。

新流程：

1. 操作者显式指定隔离测试数据库和文档白名单。
2. 在一个 PostgreSQL `REPEATABLE READ, READ ONLY` 事务里读取指定文档的活动版本及其全部 chunk；无 topK、LIMIT、HTTP 或 Embedding 请求。
3. 保存文档清单、版本、元数据、chunk 数量、内容哈希，以及解析/分词/Embedding/索引版本等现有数据库信息。
4. 从本地快照加载金标准，按文档 ID 和原文 anchor 映射证据。
5. 任一正样本的任一 anchor 缺失、未绑定或有跨版本歧义，整个预检失败，不执行检索、不缩小分母。

只修改评测工具和现有调用方的预检；没有修改生产检索算法、阈值、数据库结构或生产 API。

## 2. 文件职责

| 文件 | 职责 |
|---|---|
| `export_snapshot.py` | 直接读取受控评测库，完整导出指定文档的活动版本 |
| `corpus_map.py` | 校验快照、限定文档身份映射证据、严格预检所有案例 |
| `evidence_bindings.json` | 将现有 29 个唯一 anchor 绑定到 3 个固定文档 ID；临时侧车文件，不改变案例格式 |
| `run_eval.py` | 改为离线快照预检通过后才检索 |
| `calibrate_threshold.py` | 共用严格预检；本项未修正其阈值计算方法，也未执行阈值调优 |
| `../tests/test_eval_corpus_snapshot.py` | 快照、SQL 范围、只读事务、证据绑定和调用方预检测试 |

现有标注共 81 条：K 类 29 条、S 类 12 条、N 类 40 条。绑定完整性已测试，但实际原文能否命中仍需在真实导出快照上验证。

## 3. 安全及运行前提

- 只在隔离评测环境运行；不要直接使用生产管理员账号。
- 推荐数据库账号仅具备以下表的 SELECT 权限：`document_active_version`、`knowledge_document`、`knowledge_chunk`、`knowledge_embedding`。
- 导出命令只读取 `KNOWLEDGE_EVAL_DATABASE_URL`，不读取应用默认数据库地址、`.env` 或 NAS 配置；不把连接 URL 或错误详情写入产物。
- 文档范围必须显式提供，不能默认导出全部库。
- 不导出数据库凭据、服务 API Key、服务器存储路径或原始向量。
- 快照仍包含完整知识内容和授权元数据，需要限制目录访问；新文件在支持的文件系统上使用临时文件的受限权限。Windows/挂载盘上的 ACL 需操作者确认。
- `eval/snapshots/` 已加入 `.gitignore`，不要把敏感快照提交到仓库。
- 新文件完整写入后才创建最终路径；不覆盖已有快照。所在文件系统须支持硬链接，否则导出失败，需选择支持的本地目录。

## 4. 使用方式

命令均在 `heat-knowledge-service` 根目录执行。使用具有项目 SQLAlchemy / psycopg 依赖的 Python 环境。

### Linux / Bash

先通过安全渠道配置数据库连接：

```bash
export KNOWLEDGE_EVAL_DATABASE_URL='postgresql+psycopg://<只读用户>:<密码>@<测试库地址>:5432/<测试库名>'

python eval/export_snapshot.py \
  --bindings eval/evidence_bindings.json \
  --out eval/snapshots/baseline-001.json

# --annotations 显式选择旧标注；不传它时使用新的 beijing-v1 标准评测集。
python eval/corpus_map.py \
  --annotations eval/annotations.jsonl \
  --snapshot eval/snapshots/baseline-001.json
```

`--bindings` 使用侧车文件中的三份文档作为白名单。若需显式选择更多文档，可使用重复参数：

```bash
python eval/export_snapshot.py \
  --document-id '<文档ID1>' \
  --document-id '<文档ID2>' \
  --out eval/snapshots/another-001.json
```

已有评测脚本通过环境变量选择快照：

```bash
export KNOWLEDGE_EVAL_SNAPSHOT='eval/snapshots/baseline-001.json'
```

第三项执行器支持此环境变量及 `--snapshot`，但还必须显式配置服务地址和查询密钥；参见 [RUNNER_GUIDE.md](RUNNER_GUIDE.md)。**第一项本身只验证离线预检，不要求执行线上检索或阈值标定。**

### Windows / PowerShell

```powershell
$env:KNOWLEDGE_EVAL_DATABASE_URL = 'postgresql+psycopg://<只读用户>:<密码>@<测试库地址>:5432/<测试库名>'

.venv\Scripts\python.exe eval/export_snapshot.py --bindings eval/evidence_bindings.json --out eval/snapshots/baseline-001.json

.venv\Scripts\python.exe eval/corpus_map.py --annotations eval/annotations.jsonl --snapshot eval/snapshots/baseline-001.json

$env:KNOWLEDGE_EVAL_SNAPSHOT = 'eval/snapshots/baseline-001.json'
```

示例连接串是占位符；真实密码应由安全环境配置提供，避免进入 shell 历史或共享文档。

## 5. 快照格式与校验

快照保存：

- `schemaVersion`、UTC 导出时间、导出方式；
- `documentIds`、`documentCount`、`chunkCount`；
- `documents`：活动版本、标题、地域、有效状态、权限、原始内容哈希、解析版本及 chunk 数量等；
- `chunks`：完整文本、位置、章节、条款、内容 SHA-256、数据库原有哈希、分词及索引元数据等；
- `corpusHash`：文档和 chunk 内容及元数据的规范化哈希，独立于导出时间；
- `snapshotId`：包含导出时间在内的完整快照哈希。

加载时验证整体哈希、语料哈希、每个 chunk 的内容哈希、唯一标识、活动版本对应关系以及逐文档和总数量。

这些是完整性检查，不是数字签名，也不能证明输入来自可信操作者。文件权限和来源管理仍然必要。

`sourceContentHash` 是原始上传内容的数据库哈希；`contentHash` 是对实际 chunk 文本重新计算的 SHA-256，两者不能混用。

## 6. 证据映射规则

- 原文 anchor 必须绑定明确的 `documentId`，不通过相似度或标题猜测。
- 同一句出现在另一份文档时，不能作为当前证据命中。
- 同一活动文档版本内因重叠切分命中多个 chunk 时保留全部 ID，不任意选择第一条。
- 同一 anchor 在绑定文件中重复声明时报错。
- 同一个文档跨版本命中时报错；正式快照自身也不允许混入非活动版本 chunk。
- 任何一个 anchor 失败，包括一个多锚点案例仅部分解析成功，也会中止预检。

第二项现已标准化案例，证据直接包含文档、原文和定位说明，可选版本；详见 DATASET_GUIDE.md。本节的侧车规则仅说明历史标注兼容。

## 7. 边界与后续事项

- 快照是**指定文档的完整内容快照**，不是数据库备份，也不是向量索引备份。
- 它不能自动冻结正在运行的知识服务；导出后评测期间不能继续入库或切换活动版本。
- 建议测试环境只装载本次评测语料；若被测环境包含其他参与检索的文档，也应将它们纳入语料清单。
- 本项保存数据库已有的解析/分词/Embedding/索引信息，不声称已保存服务全部运行配置；阈值、融合、代码和环境清单在执行器任务补充。
- 第三项已移除 `run_eval.py` / `load_corpus.py` 的默认远程地址和密钥兜底，参数与产物见 RUNNER_GUIDE.md；本节的其余说明保留第一项范围。
- 第四项已审核阈值脚本的数据过滤和分母问题，停止旧推荐；确定性指标与性能统计定义见 METRICS_GUIDE.md。

## 8. 验证结果

本项新增 24 个测试，覆盖：

- 超过 topK 上限的 75 个 chunk 全部保留；
- 数据库查询参数绑定、活动版本限定、无 LIMIT / 向量近邻操作；
- 只读一致性事务设置及资源释放；
- 快照篡改、数量不一致、空文档、重复标识和版本异常；
- 文档限定、重复绑定、多锚点部分失败与跨版本歧义；
- 两个旧调用方预检失败时不发出任何检索请求；
- 不覆盖旧文件、无默认数据库连接、失败不泄露连接串。

执行新增测试及现有回归测试：

```bash
python -m unittest discover -s tests -v
```

本次在临时隔离 Python 3.14 环境执行，共 38 个 unittest 测试通过；现有 chunker 模块级断言也通过。测试环境不是项目 Windows Python 3.12 环境，且未连接实际 PostgreSQL / NAS，也未生成真实语料快照或进行效果基线实验。SQL 与事务通过模拟连接验证；真实数据库导出仍需在指定测试环境进行验收。
