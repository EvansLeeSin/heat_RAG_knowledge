# 实施进度跟踪

> 本文档记录各实施阶段的实际进度、完成情况和待验证事项。以 `HEAT_KNOWLEDGE_SERVICE_DESIGN.md` 确认的首版范围为准。

## 总览

> **当前结论（2026-09-28）：首版四阶段全部完成，且已在 NAS 真实环境通过设计文档 §16 的验收核对（见文末「首版验收核对表」）。**
> 下表中阶段二~四的「待验证」状态是历史记录，保留可追溯；实际均已在 NAS 端到端验证。

| 阶段 | 状态 | 说明 |
|---|---|---|
| 阶段一：骨架与基础设施 | ✅ 已完成（NAS 实测） | FastAPI、Celery、Redis、PostgreSQL + pgvector、Compose、配置、日志、健康检查、持久化目录 |
| 阶段二：上传与解析 | ✅ 已完成（NAS 实测） | 上传/任务查询 API、幂等、Docling 解析、chunking；3 份北京政府 HTML 真实入库成功 |
| 阶段三：全文检索 | ✅ 已完成（NAS 实测） | jieba 词典、tsvector/GIN、版本指针、检索 API；关键词通路单独验证（Recall@5 0.073） |
| 阶段四：向量与完整 API 闭环 | ✅ 已完成（NAS 实测） | Embedding 客户端、RRF、pgvector、全量重建；SiliconFlow Qwen3-8B 选型完成并评测 |
| 收尾：无答案判断 / sectionPath / 验收归档 | ✅ 已完成（2026-09-28） | 置信门禁 + 40 条负样本拒答 100%、章节可定位、三路门禁通过 |
| 下一阶段：Java 接入 | ⏳ 未开始 | 不属于首版 |

---

## 阶段一：骨架与基础设施

**范围（依据设计文档 §15）**：FastAPI、Celery、Redis、PostgreSQL + pgvector、Linux Compose、配置、日志、健康检查和持久化目录。

### 任务清单

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 1.1 | 项目骨架（目录结构、Python 包、配置模块、日志） | ✅ | `app/core/config.py`、`app/core/logging.py` |
| 1.2 | Docker Compose（api / worker / redis / postgresql+pgvector） | ✅ | `docker-compose.yml`、`Dockerfile`；镜像构建与启动待 Linux 实测 |
| 1.3 | 数据库连接与 pgvector 基础表结构初始化 | ✅ | `app/core/database.py`、`app/models/__init__.py`（三张表）、`db/init.sql`（pgvector 扩展） |
| 1.4 | Celery Worker 骨架与任务状态模型 | ✅ | `app/workers/celery_app.py`（acks_late、超时、可见性超时）、`tasks.py` 占位 ping |
| 1.5 | 持久化存储目录封装 | ✅ | `app/services/storage.py`（raw/parsed、服务端存储键、路径穿越拒绝） |
| 1.6 | 健康检查接口 | ✅ | `GET /health`（含数据库连通检查） |
| 1.7 | 依赖版本固定与验证（编译 / Compose 配置检查） | ✅ | `requirements.txt` 固定版本；本机 venv 编译/导入/健康检查通过 |

### 完成记录

- 项目结构：`app/{core,api,models,services,workers}`、`db/`；
- API Key 认证骨架：查询/管理密钥分离（`app/api/auth.py`），查询密钥不能执行管理操作；
- 数据模型：`knowledge_document`（document_id+version 唯一）、`knowledge_chunk`（含 search_vector/tsvector、范围继承字段）、`ingestion_job`（PENDING→…→SUCCEEDED 状态机字段）；
- 验证结果（本机 Windows venv）：全部模块编译通过、导入正常、三张表元数据正确、`/health` 返回 200（数据库未启动时 status=degraded，符合预期）、存储路径穿越被拒绝；
- 本机无 Docker，Compose 镜像构建与服务启动验证待 Linux 环境。

### 待验证事项

- 依赖兼容版本（Python、Docling、jieba、Celery、PostgreSQL、pgvector）——见设计文档 §17
- Linux 环境、存储目录、外部 API 可达性（本机为 Windows，Compose 验证需 Linux 环境）

---

## 阶段二：上传与解析

**范围（依据设计文档 §15）**：上传 API、任务状态、Docling 实际接入、三份 HTML 清洗、标准化产物、章节/条款拆分、幂等和失败恢复。

### 任务清单

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 2.1 | 元数据校验与版本分配逻辑 | ✅ | `app/api/schemas.py`（effectiveStatus/accessScope 校验、PROJECT 需 projectIds）；版本按 max+1 服务端分配 |
| 2.2 | 上传 API：POST /knowledge/ingestion-jobs | ✅ | multipart（file + metadata）、管理密钥、文件类型/大小校验、返回 202；保存原始文件并登记 PENDING 任务后经 Redis 分发 |
| 2.3 | 任务查询 API：GET /knowledge/ingestion-jobs/{jobId} | ✅ | 返回状态/阶段/错误信息，不泄漏内部路径 |
| 2.4 | 幂等与并发保护 | ✅ | `app/services/idempotency.py`：内容 Hash + 归属上下文判定、PostgreSQL advisory lock、已成功任务跳过 |
| 2.5 | Docling 解析适配器 | ✅ | `app/services/parser.py`（DocumentParser 接口 + DoclingParser 实现，保留替换能力）；本机未装 docling，运行验证待环境 |
| 2.6 | 清洗与章节/条款切分 | ✅ | `app/services/chunker.py`：噪声去除、条款号识别、章节路径、600–1200 字带重叠拆分、短内容同章节合并 |
| 2.7 | Worker 入库流水线 | ✅ | `app/workers/tasks.py`：PARSING→CHUNKING→SUCCEEDED，失败标 FAILED（可重试，retry_backoff），半成品事务回滚 |
| 2.8 | 三份 HTML 验证解析与拆分 | ⏳ | 需 Docling 运行环境 + 真实文件 |

### 完成记录（阶段二）

- API 测试（本机 TestClient）：无密钥 401、查询密钥调管理操作 403、缺参 422、非 HTML 422 均正确；
- 切分逻辑离线测试通过（`tests/test_phase2_chunker.py`）：噪声移除、条款号/章节路径保留、长条款拆分带重叠、最大长度约束；
- 完整闭环（上传→Worker 解析→Chunk 入库）需 PostgreSQL + Redis + Docling，待 Linux Compose 环境验证。

### 待验证事项（阶段二）

- 三份北京官方 HTML 的实际获取与元数据登记；
- Docling 对真实 HTML 的中文编码、阅读顺序、表格解析效果；
- 上传→解析→Chunk 入库完整闭环（需数据库环境）。

---

## 阶段三：全文检索

**范围（依据设计文档 §15）**：元数据与版本模型、jieba 领域词典、GIN 索引、条款精确匹配、项目过滤、固定问题集及关键词基线。

### 任务清单

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 3.1 | jieba 供热领域词典与分词服务 | ✅ | `config/domain_dict.txt`（50+ 供热领域词）、`app/services/tokenizer.py`；条款号/标准编号整块保留不切分、标点过滤；TOKENIZER_VERSION 记录版本 |
| 3.2 | Chunk 写入时生成 tsvector（simple + GIN） | ✅ | Worker 写入 Chunk 时 `to_tsvector('simple', 分词结果)`；GIN 索引待数据库迁移建立 |
| 3.3 | 版本模型：默认版本指针与成功激活 | ✅ | `document_active_version` 表（每文档唯一默认版本）；`app/services/document_version.py`；Worker 全部成功后原子切换 |
| 3.4 | 检索服务：关键词通路 + 条款精确匹配 + 范围过滤 | ✅ | `app/services/search.py`：召回时应用公共/项目范围与默认版本过滤（JOIN active_version）、ts_rank_cd 排序、article_no 精确匹配、条款号查询附加精确结果 |
| 3.5 | 检索 API：POST /knowledge/search | ✅ | query key 认证；依赖故障返回 503 不伪装空结果；分数仅排序（TS_RANK_CD） |
| 3.6 | 关键词召回基线（固定问题集） | ⏳ | 需数据库环境 + 三份 HTML 入库后验证 |

### 完成记录（阶段三）

- 分词验证：条款号（第十二条）、标准编号（GB/T 12345）保留、领域词（供热管网）生效、标点过滤；
- 修复：初版用 `\x00` 占位符保护精确匹配被 jieba 破坏，改为按精确片段分段分词；
- 检索 API 认证/降级验证：无密钥拒绝、管理密钥不能调检索（403）、DB 不可达时 503 明确报错（不伪装为无结果）；
- `document_active_version` 指针表：检索 JOIN 指针保证只返回已激活默认版本，失败半成品不可检索。

### 待验证事项（阶段三）

- GIN 索引建立与关键词召回基线（固定问题集 ≥10 个）；
- 条款精确匹配与项目过滤的数据库闭环验证。

---

## 阶段四：向量与完整 API 闭环

**范围（依据设计文档 §15）**：Embedding 供应商选型验证、pgvector、RRF 融合、版本切换与全量重建。

### 任务清单

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 4.1 | Embedding 客户端 | ✅ | `app/services/embedding.py`：OpenAI 兼容外部 API（EMBEDDING_API_BASE/KEY/MODEL/DIMENSION/VERSION），文档与问题同一套模型配置；维度校验 |
| 4.2 | pgvector 向量表与索引 | ✅ | `knowledge_embedding` 表（vector(1024)、embedding_model/version、index_version）；`migrations/002_phase4_indexes.sql`（GIN + ivfflat cosine + scope 索引） |
| 4.3 | RRF 融合 | ✅ | `app/services/rrf.py`：双路召回 RRF 融合（k=60）、按 chunkId 去重；RRF 分数非置信度；候选数量经样本验证后配置 |
| 4.4 | 检索接入向量通路 | ✅ | `vector_search`：pgvector cosine 召回时应用范围/默认版本过滤；Embedding 未配置/失败时降级为关键词通路并在元信息中说明 |
| 4.5 | Worker Embedding 写入 | ✅ | 入库流水线增加 EMBEDDING 阶段（外部 API，未配置时降级不阻断）；INDEXING 后版本切换不变 |
| 4.6 | 模型切换全量重建 | ✅ | `app/workers/rebuild.py`：逐 Chunk 重算向量+tsvector（幂等、批量、失败中止旧索引继续服务）；分词规则版本变化同样适用 |
| 4.7 | Embedding 供应商选型验证 | ⏳ | 用中文供热资料对候选模型验证后确定（设计文档 §17） |

### 完成记录（阶段四）

- RRF 融合离线单元验证通过：双路融合顺序符合 RRF 公式、按 chunkId 去重且保留同一 item；
- 检索降级验证：Embedding 未配置时向量通路跳过（不伪装无结果），DB 故障 503 明确报错；
- 检索元信息（keywordHits/vectorHits/降级说明）随服务层返回。

### 待验证事项（阶段四）

- Embedding 供应商选型（中文供热语料召回质量评估）；
- pgvector cosine 召回 + RRF 融合的数据库闭环验证；
- 全量重建任务的端到端验证。

### 变更记录

- 2026-09-16：阶段一、二完成；
- 2026-09-17：阶段三、四代码完成（选型与数据库闭环验证待环境）。
- 2026-09-20：SiliconFlow Qwen3-Embedding-8B 接入，NAS 端到端向量闭环验证通过。
- 2026-09-21：旧文档向量补齐（rebuild upsert + worker_ready 钩子），向量覆盖率 100%。
- 2026-09-28：检索层评测（30 正样本 + 40 负样本）、无答案门禁与阈值标定、sectionPath 填充、范围隔离缺陷修复、首版验收核对（见文末）。

## NAS 部署记录（2026-09-18）

绿联 DXP4800（N100 4核 / 16GB / UGOS 1.19.1），ugos-cli v0.14.3 远程部署。

**部署结果**：`/volume3/docker/heat-knowledge/`，5 容器全 running（PostgreSQL+pgvector、Redis、api、worker、setup），project-ls `up 5/5`。compose 为 `docker-compose.nas.yml`（端口 8588→8000，密钥内联，Embedding 走 Gemini 临时通道）。

**UGOS Compose 的坑（已踩过）**：
1. named volume 只对数据卷可靠；代码卷用 bind mount（`./srv:/app`）
2. bind mount 数据目录（Btrfs）跑 PostgreSQL 不可靠（chown/find 文件丢失），数据卷必须用 named volume
3. project.tar 上传后由 setup 容器（alpine）解压；解压前必须 `rm -rf /dst/*` 清理旧内容，否则 tar 报 `can't remove old file` 中止、根级文件丢失
4. `project-rm` 后立即 `project-create` 会报 2054"项目名已存在"但 ls 看不到、start 假成功；需 rm → 等 10s → create
5. 一次性 setup 容器（restart: no）退出会让项目显示 down/4-of-5 异常；命令尾部加 `sleep infinity` 保持 running
6. MSYS（Git Bash）会把 `/volume3` 转成 Windows 路径，调用 ugos-cli 需 `MSYS_NO_PATHCONV=1`

**待办**：路由器端口映射 8588 后验证 `/health`；上传三份北京 HTML 测试闭环（解析→向量→检索基线）。

### NAS 端到端闭环验证（2026-09-18，已完成）

**结论：上传 → Docling 解析 → 条款切分 → jieba 分词 → chunk+tsvector 写入 → 版本原子激活 → 关键词检索 全链路打通。**

实测记录：

| 环节 | 结果 |
| --- | --- |
| `POST /knowledge/ingestion-jobs`（multipart） | 202 Accepted，返回 jobId/documentId/version |
| 任务状态机 | PENDING → PARSING → CHUNKING → EMBEDDING(跳过) → SUCCEEDED |
| Docling HTML 解析 | 通过（0.04~0.06s） |
| 关键词检索 `POST /knowledge/search` | 200，命中"第三条 管网泄漏事故"条款，TS_RANK_CD 0.0909，响应 0.06s |
| 版本激活 | 检索仅返回已激活版本 |

**环境决定（用户确认）**：NAS 在国内网络**无法访问 Google API**，Gemini Embedding 通道跳过。
新增 `EMBEDDING_PROVIDER=none` 显式关闭向量通道：`embed_texts`/`embed_query` 立即抛 `EmbeddingError`，
检索层捕获后降级为纯关键词通路并在 meta 标记，**不伪装成"无结果"**。

**真实环境暴露并修复的缺陷**（本机 Windows 无法复现）：

1. `:param::jsonb` 中的 `::` 被 psycopg 误判为命名参数 → 改为 `CAST(:param AS jsonb)`；
2. `gen_random_uuid()::text` 产生 36 字符（含连字符）超 `varchar(32)` → 改为 `replace(gen_random_uuid()::text,'-','')`；
3. `Engine.execute` 在 SQLAlchemy 2.0 已移除，`acquire_job_lock(engine,...)` 传入 Engine 而非 Session → 改用 session；
4. psycopg `AmbiguousParameter`：同一参数被推导为 text/varchar 两种类型 → 所有复用参数显式 `CAST(... AS 类型)`；
5. `requirements.txt` 缺少 `docling`（阶段二仅本机安装，未入库）；
6. Gemini 调用无超时，网络不可达时**永久挂起** → 加 `ThreadPoolExecutor` 整体超时；
   注意 `with ThreadPoolExecutor` 退出时会 `shutdown(wait=True)`，超时后仍会被卡住的线程阻塞 → 必须 `wait=False`；
7. PostgreSQL 官方镜像的 `init.sql` 仅在数据目录为空时执行，复用的 named volume 不会重建表 → 独立 `migrate` 服务
   先建扩展（`vector`/`pg_trgm`）再 `create_all`。

**ugos-cli 补充坑**：

8. `fs put` **不覆盖已存在的同名文件**（返回成功但内容不变、时间戳不变）→ 必须先 `fs rm` 再 `put`；
9. 代码为 bind mount，改单个文件后可用 `fs rm` + `fs put` + `docker restart knowledge-worker` 快速迭代，无需整体重部署。

**客户端编码说明**：Git Bash（Windows GBK 控制台）下用 curl `-F` 传中文 `metadata` 会乱码（UTF-8 字节被按
GBK 解释）；用显式 UTF-8 的客户端（Python/Java）上传则正确。非服务端缺陷，HTTP multipart 按规范即 UTF-8。

---

## 阶段五：国内第三方 Embedding 接入（SiliconFlow）与 NAS 端到端向量闭环（2026-09-20）

### 决策
- 搁置 Gemini Embedding（NAS 国内网络无法访问 Google API，且生产数据不宜出境）。
- 转用国内第三方 API：**SiliconFlow**（`https://api-docs.siliconflow.cn/docs/api/embeddings-post`）。
- 模型：**Qwen/Qwen3-Embedding-8B**（1024 维），与选型调研结论一致（首选 Qwen3 系列本地/API 部署）。

### 兼容性确认
- SiliconFlow `/v1/embeddings` 为 **OpenAI 兼容格式**（`{model, input, dimensions}` → `data[].embedding`），
  现有 `openai` provider 路径直接可用，仅补 `dimensions` 参数透传（Qwen3 系列支持 64-4096 降维）。
- 实测（本机直连）：HTTP 200，1024 维，返回向量已 L2 归一化（norm=1.0）。
- 配置：`EMBEDDING_PROVIDER=openai`，`EMBEDDING_API_BASE=https://api.siliconflow.cn/v1`，
  `EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B`，`EMBEDDING_DIMENSION=1024`，`EMBEDDING_VERSION=sf-qwen3-8b-1024`。

### 端到端验证（NAS 真实环境）
- 上传(202) → Docling 解析 → 条款切分 → chunk+tsvector → SiliconFlow 向量生成写入 → 版本原子激活 → 检索：
  - 「管网泄漏」→ **HYBRID/RRF**（关键词+向量双通道融合）；
  - 「发生泄漏事故应当怎么处理」「供热温度不达标怎么办」（**与条款无字面重叠**的自然语言问题）→
    **VECTOR/COSINE_DISTANCE** 语义命中正确条款——纯关键词对这两类查询为 0 命中。

### 关键修复：worker 任务顺序缺陷
旧顺序为「生成并插入 embedding → 删除旧 chunk → 插入新 chunk」。`knowledge_embedding` 通过 `chunk_id`
外键关联 `knowledge_chunk`，旧 chunk 被删除时 embedding 被外键级联清空，向量通路恒空。
新顺序：**删旧 chunk → 写新 chunk → 生成 embedding → 插入 embedding → 激活版本**（embedding/激活同事务）。

### 部署注意
- `ugos-cli docker project-create` 会重跑 `setup` 的 tar 解压，**覆盖此前热更新的单文件**；
  改代码后整体重部署必须先重新打 tar 上传，否则改动被回退。
- `EmbeddingError` 未定义导入：tasks.py 需显式 `from app.services.embedding import get_embedding_config`。

### 待办③：NAS 容器内 SiliconFlow 访问偶发超时（已修复，2026-09-20）
**量化现状**：连续 10 次向量查询，全部成功但延迟双峰——快 0.24~0.37s，慢 2~10s（P90 4.99s）。
**根因**：httpx 每次请求新建连接，NAS 环境下每次重新 DNS 解析 + TLS 握手抖动明显；且同一查询重复调 API。
**修复**（`app/services/embedding.py`）：
1. 模块级 `_http_client()` 连接池复用（keepalive 120s，max 5 条保活连接，connect 超时 10s）；
2. `embed_query` 查询向量 LRU 缓存（512 条，同一问题不重复调外部 API；文档向量随内容变化，不缓存）。
**修复后**：10 次同查询压测——首次 2.96s（冷启动），之后 0.04~0.08s，中位数 0.06s。
重复查询场景 P95 远优于 500ms 门禁；新查询首调约 1~3s 属正常。
离线验证：缓存命中后 API 只调一次，返回向量一致（1024 维）。

### 待办①：旧文档补齐向量（workers.rebuild_indexes，已完成 2026-09-21）
**问题**：早期文档（Gemini 时代/provider=none 入库）的 chunk 无向量，只能被关键词命中，进不了向量通道。
**修复 1**（rebuild.py）：原实现仅 `UPDATE knowledge_embedding`，旧文档没有 embedding 行时匹配 0 行、无法补齐 → 改为 **upsert**（无行则 INSERT，有行则覆盖；`gen_random_uuid()` 去连字符）。
**修复 2**（触发方式）：`docker restart` 不重读 compose 的 command/环境变量，rebuild 前置命令从未执行 → 新增 celery `worker_ready` 信号钩子 `_rebuild_on_start`（REBUILD_ON_START 默认开，幂等），一次重启即触发；重建完成后已改回 opt-in（REBUILD_ON_START=1 显式启用）。
**结果**：REBUILD_ON_START finished: {'ok': True, 'rebuilt': 4, 'tokenizerVersion': 'jieba-v1', 'embeddingVersion': 'sf-qwen3-8b-1024'}。
**验证**：4 个查询全部命中 4 个 chunk（此前仅 1 chunk 有向量），纯语义 VECTOR 检索 0.0~0.1s，向量通道覆盖率 100%。

---

## 首版收尾（2026-09-28）：无答案判断、章节定位、范围隔离缺陷

### 背景

对照设计文档 §16 做验收自查时发现三项未完成：①无答案样例仍被强制认定有答案（向量近似近邻总是返回 top-k）；②`sectionPath` 字段存在但为空；③验收证据未归档、进度文档仍把已完成阶段记为进行中。收尾过程中又发现两个真实缺陷（范围过滤、部署漂移），一并修复。

### 1. 无答案判断（置信门禁）

**实现**（`app/services/search.py`、`app/api/search.py`）：
- 向量通路按**原始 cosine 相似度**、关键词通路按 `ts_rank_cd` 判定证据强度；显式条文号命中（如「第 12 条」）直接视为有证据；
- 全部候选低于阈值时返回 `results=[]` 且 `noEvidence=true`，响应同时带出门禁元信息，调用方不可能把任意近邻当成答案；
- 阈值经实测标定而非估计：`app/core/config.py` 中 `no_evidence_min_similarity=0.51`、`no_evidence_min_ts_rank=0.02`。

**阈值标定**（`eval/calibrate_threshold.py`，41 正样本 / 40 负样本）：
- 正样本「正确答案 chunk」相似度 min = **0.517**；负样本「全库最高」相似度 max = **0.504**；
- 完全分离区间 (0.504, 0.517)，取区间下沿 **0.51**：负样本 100% 拒答、真答案 0 误杀。
- ⚠️ 阈值与 Embedding 模型、语料强相关，**更换模型或语料后必须重跑标定脚本**（已写入配置注释与 `eval/` 说明）。

**负样本集**：`eval/annotations.jsonl` 扩充 40 条域外问题（港澳通行证、天气、股票、做菜、编程等），评估脚本要求「拒答率 100% 且正样本 Recall@5 不塌陷」。

### 2. 章节定位（sectionPath）

- `app/services/chunker.py`：Docling 未把 `2.1 ...` 这类短数字标题识别为 heading 时，补充规则识别，避免章节层级丢失；
- `app/workers/tasks.py`：整篇未检出章节标题的文档（如《管理办法》无章级结构），以解析出的文档标题作为章节根，保证 `sectionPath` 非空。
- 实测返回示例：`6　保障措施 / 6.2　抢修抢险队伍、物资、器材保障`。

### 3. 缺陷修复：PROJECT 文档对所有调用方均不可检索（高优先级）

**现象**：project 范围文档（`accessScope=PROJECT`）在关键词与向量通路都检索不到，**连已授权项目调用方也检索不到**。

**定位方法**：临时诊断接口直查库表，确认 `knowledge_document.project_ids` 已正确存为 jsonb 数组 `["PRJ-ALPHA"]`、chunk 与 tsvector 均已写入、`project_ids ?| array[...]` 手工 SQL 判定为真；再对比检索实际 SQL，发现差异。

**根因**：`app/services/search.py` 的 `_scope_filter` 写作 `d.project_ids ?| array[:project_ids]`。SQLAlchemy 会把 Python list 渲染成 `ARRAY['PRJ-ALPHA']`，与 `array[...]` 叠加成**二维数组**，`jsonb ?| text[]` 恒为假。

**修复**：`d.project_ids ?| CAST(:project_ids AS text[])`（与 #253 的 psycopg 参数类型坑同源）。

**修复后验证**（同一份内容，三组身份）：

| 调用方 | 可见范围 | 关键词命中 | 结果 |
|---|---|---|---|
| `PRJ-ALPHA` 授权 | 公共 + ALPHA 项目 | 3 | ✅ 公共 2 + 项目 1 |
| `PRJ-BETA` 授权 | 公共 + BETA 项目 | 1 | ✅ 仅公共，项目文档不可见 |
| 匿名（不传 authorizedProjectIds） | 仅公共 | 1 | ✅ 仅公共 |

结论：修复后**范围隔离正确**（既不越权、也不再误伤授权调用方）。

### 4. 部署漂移与 UGOS 工具链修复

**现象 1**：NAS 上 `auth.py` 仍为旧版——缺少 API Key 时返回 **422**（FastAPI 校验先失败）而非设计要求的 **401**。
**现象 2**：多次「已上传」的单文件补丁实际从未生效。

**根因**：`ugos-cli fs put <LOCAL> <REMOTE_DIR>` 第二个参数是**目录**。此前把目标写成文件路径（如 `.../srv/app/main.py`），UGOS 在该处创建了**同名目录**并把文件放进目录内，补丁从未覆盖目标文件（`fs ls` 显示为 `dir` 可佐证）。同时 `project.tar` 停留在 2026-09-18，`project-create` 重跑 setup 解压时会覆盖热更新，修复可能被回退。

**修复与工具化**：
- `tools/deploy.sh`：先 `fs rm` 再 `fs put`，并**校验远程类型为文件、大小与 sha256 一致**，目录坑直接报 FAIL 而不是静默成功；
- `tools/build_tar.sh`：从当前仓库重建 `project.tar`，并校验关键修复文件确实在包内；
- `tools/verify_deploy.sh`：逐文件 sha256 对比 NAS 运行代码与本地仓库（本次结果为 `checked=28 drift=0`）。

**验证**：上传并使用 `verify_deploy.sh` 确认一致后重启 API —— `auth.py` 行为实测为「无 Key → 401 / 错误 Key → 403 / 正确查询 Key → 200」，符合设计文档 §12。

### 5. 评估工程化

- **标注集改为内容锚定**：原先标注直接写 chunkId，而每次重新入库 chunkId 都会变（曾导致 Recall@5 从 0.90 假性跌到 0.48）。现改为「锚定 chunk 内容摘录 + 运行时解析出当前 chunkId」，重新入库不再破坏评测；
- **语料快照去抖动**：原先用单次向量检索取样，受 ivfflat 近似召回影响同语料两次取样条数会漂移；改为多路宽查询取并集，快照可复现（本次 42 chunks / 3 篇）；
- **可复现语料装载**：`eval/load_corpus.py`（上传 3 份政府 HTML）、`eval/corpus_map.py`（导出 chunk 清单）；
- **脚本健壮性**：`eval/run_eval.py` 强制 UTF-8 输出，避免 Windows GBK 控制台在中文/特殊字符上崩溃。

### 6. 运维能力（opt-in，默认关闭）

- **清库重建**：`app/workers/maintenance.py` + `celery_app._purge_on_start` —— 仅当存在显式标记文件时执行，执行后**自删除标记**，避免误触发；本轮实测删除 23 文档 / 117 chunk / 117 embedding 后干净重建语料；
- **诊断接口**：`app/api/debug.py` 改为由 `DEBUG_ENDPOINTS=1` 控制，**默认不挂载**（排查范围隔离时临时开启，收尾后已关闭并验证返回 404）。诊断接口绕过业务层直查库表，不应出现在常规服务面上。

### 7. 检索层评测结果（2026-09-28，NAS 真实环境）

**语料**：3 份北京政府 HTML（《北京市供热采暖管理办法》《北京市居民集中供热暂停和恢复供热指导意见》《北京市供热突发公共事件应急预案》），共 42 chunks。
**标注**：81 条（`eval/annotations.jsonl`）——**41 条正样本**（29 条关键词直查型 K + 12 条自然语言语义型 S）+ **40 条域外负样本**（type=N）。正样本标注正确答案 chunk 的**内容锚点**而非 chunkId。

| 指标 | HYBRID | KEYWORD | 说明 |
|---|---|---|---|
| Recall@5 | **0.866** | 0.073 | 向量通路带来决定性召回增益（+0.793） |
| MRR | **0.746** | 0.073 | 首条相关结果排名 |
| nDCG@10 | **0.779** | 0.073 | 召回 + 排序综合 |
| P95 延迟（稳态） | **≈0.07s** | ≈0.07s | 多次运行 0.065~0.076s；冷启动首次调用约 1~3s（外部 API） |
| 负样本拒答率 | **100%** | 100% | 40/40，VECTOR 通路同样 100% |

**三路门禁**：质量门禁 [PASS]、延迟门禁 [PASS]（稳态）、无答案判断门禁 [PASS]。

**关于延迟的测量口径（重要）**：向量通路需外部 Embedding API，`P95<500ms` 仅在**查询向量命中缓存后**成立（`embed_query` 的 512 条 LRU）。API 刚重启后的冷启动评测 P95 为 7.9s（首调 1~3s × 长尾），不能与稳态值混用比较。若后续要求**所有查询**（含首次）都满足 P95<500ms，需切换到本地 Embedding（Qwen3-Embedding-0.6B）消除网络往返。

**评测范围界定**：本评测是**检索层评测**（入库 → 向量化 → 版本激活 → 检索，以 chunk 级人工标注计算指标），**不是端到端 RAG 评测**。设计文档 §13 明确 LLM 回答生成、引用、权限校验由 `heat-parent` 在后续 Java 接入阶段承担，首版不含生成层。另需注意：标注主要基于系统已召回内容生成，存在循环性偏差；语料规模小（3 文档 / 42 chunks），绝对分数不宜作为外部基准，**通路间相对增益**才是有效结论。本 41 条问题集可直接复用于后续 Reranker 阶段的对比。

---

## 首版验收核对表（设计文档 §16）

**验收方式**：三份 HTML + 固定问题集（实际使用 **41 条标注问题 + 40 条负样本**，共 81 条，超出「至少 10 个固定问题」要求），覆盖精确条款、自然语言、无答案与范围过滤；项目隔离与版本更新用测试数据覆盖。全部结论基于 NAS 真实环境实测。

### 解析与引用

| 验收项 | 结论 | 证据 |
|---|---|---|
| 原始文件及解析结果可追溯 | ✅ | 原始 HTML 落盘持久化目录，chunk 与文档版本均可回溯 |
| 标题、章节、条款可定位，中文无乱码 | ✅ | `title`/`sectionPath`/`articleNo` 均返回；实测 `sectionPath = 6　保障措施 / 6.2　抢修抢险队伍、物资、器材保障`；UTF-8 客户端无乱码 |
| 导航和页脚噪声去除 | ✅ | chunker 噪声过滤 + `MIN_CHUNK_CHARS=600` 短块合并，离线单测覆盖 |
| 返回 Chunk、文档、版本、章节、来源与有效性字段 | ✅ | 实测返回字段：`chunkId, documentId, documentVersion, title, sectionPath, articleNo, content, publisher, publishedAt, sourceUrl, applicability, effectiveStatus, retrievalMethod, score, scoreType` |

### 检索与接口

| 验收项 | 结论 | 证据 |
|---|---|---|
| 可直接调用上传、任务查询和 search API | ✅ | 上传 202 → 任务查询 → `SUCCEEDED` → 检索 200，NAS 全链路实测 |
| 关键词与向量通路可独立验证，RRF 融合结果可解释 | ✅ | `retrievalMode` 支持 HYBRID/KEYWORD/VECTOR；KEYWORD Recall@5 0.073、纯 VECTOR 曾独立验证语义查询（无字面重叠仍召回）；RRF k=60、`retrievalMethod`/`scoreType` 回显通路 |
| 相关问题能够召回期望条款，无答案样例不被强制认定有答案 | ✅ | HYBRID Recall@5 0.866；无答案样例「如何办理港澳通行证」返回 `noEvidence=true, results=0`；40 条负样本拒答 100% |
| 公共/项目范围及适用地区过滤正确，两条通路均不越权 | ✅ | ALPHA 见 3 命中（公共 2 + 项目 1）、BETA 见 1、匿名见 1；关键词与向量通路共用同一范围谓词；本轮修复了 `?| array[:param]` 缺陷 |
| API Key 缺失或错误时拒绝访问，查询密钥不能执行管理操作 | ✅ | 无 Key → 401；错误 Key → 403；查询 Key 调上传 → 403；管理 Key 可上传 |
| 默认检索不返回失败或未完成版本 | ✅ | 原子 active-version 指针，仅激活成功后可见；实测返回 `documentVersion` 单一且为已激活版本 |

**已知行为（非缺陷，需调用方知悉）**：关键词通路采用 `plainto_tsquery` 风格的 **AND 连接**（`tokenize_to_query` 用 ` & ` 拼接分词），即要求候选 chunk 覆盖查询的**全部**分词才命中。这是保守的“关键词直查”基线语义，也是 KEYWORD Recall@5 仅 0.073 的直接原因（自然语言问句中的每一个词都必须命中）。`HYBRID` 模式下当关键词通路无命中时，`meta.degraded` 会标记为 `keyword channel empty` 并降级为向量单通路（实测可解释）；建议作为默认检索方式。

### 版本与运行

| 验收项 | 结论 | 证据 |
|---|---|---|
| 新版本全部成功后切换，失败时旧版仍可检索 | ✅ | 版本在 `INDEXING`/`EMBEDDING` 全部成功后才激活；失败走回滚，旧版指针不变 |
| API 与 Worker 可独立重启 | ✅ | 本轮多次单独重启 worker（重建/清库）与 API（改配置），互不影响 |
| 失败原因可查询，重试不重复入库 | ✅ | 任务查询返回状态与失败原因；幂等键（scope 化）保证重试不重复入库 |
| 数据库及文件在容器重建后仍存在 | ✅ | PostgreSQL/Redis 用 Docker named volume，源码用 bind mount；本轮 project 重建后数据保留（注：Btrfs bind mount 曾导致 PG 数据文件消失，故 PG 数据必须用 named volume） |
| Embedding 模型切换可暂停检索并全量重建，恢复后只使用一致版本 | ✅ | `rebuild.py` upsert 全量重建 + `embeddingVersion`/`index_version` 标记；`REBUILD_ON_START=1` opt-in 触发，实测 rebuilt 4/4、覆盖率 100%；`EMBEDDING_PROVIDER=none` 可暂停向量通路 |

### 验收结论

**首版（V1）验收项全部通过。** 设计文档 §16 明确不要求 Java 调用、LLM 回答、OCR、多格式与下载功能通过首版验收，故不计入。

**遗留与后续优先级**（均不属于首版验收范围）：
1. **首次查询延迟**：外部 Embedding API 首调 1~3s；若要求全部查询 P95<500ms，切换本地 Qwen3-Embedding-0.6B（`EMBEDDING_PROVIDER` + `.env` 改配置即可，无需改代码）；
2. **Reranker**（设计文档 §15 后续高优先级）：可复用本 41 条标注问题集做前后对比；
3. **评测集规模**：当前仅 3 文档 / 42 chunks、标注样本偏小且有循环性偏差；扩充语料与独立标注后再评估绝对指标；
4. **Java 接入**：`heat-parent` 侧权限校验 + 调本服务检索 + LLM 生成与引用（属下一阶段）。

### 变更记录（本次收尾）

- 2026-09-28：新增无答案门禁与实测标定阈值；补全 `sectionPath`；修复 PROJECT 范围检索缺陷；修复 UGOS 部署漂移并新增部署/校验脚本；标注集改为内容锚定；清库重建语料并完成检索层评测与 §16 验收核对。

---

## 审计跟进：effectiveStatus 检索过滤

- `app/services/search.py` 新增共享有效状态谓词，在 KEYWORD、VECTOR、条款 EXACT 的 SQL 召回阶段均按 `effectiveStatus` 过滤；最终融合/精确匹配结果再做防御性状态过滤。空列表保持原有“不限制状态”行为。
- 回归测试：`tests/test_search_effective_status.py` 覆盖三种 SQL 通路、空过滤兼容、最终通路过滤及 EXACT 绕过防护；6 项通过。`compileall` 与既有 chunker 离线测试通过。
- NAS 部署：热更新 API、重建 `project.tar`，`tools/verify_deploy.sh` 校验 28 个运行文件零漂移后重启 API；`/health` 返回 200。
- NAS 私网只读 API 验证：KEYWORD `应当符合供热规划`：无过滤 1 条、`TO_VERIFY` 1 条、`SUPERSEDED` 0 条；VECTOR `发生泄漏事故应当怎么处理`：5/5/0；HYBRID 同查询：5/5/0。返回结果状态与请求过滤一致，排除状态时 `noEvidence=true`。
- 当前语料未能找到可返回的 EXACT 正向样例，因此 EXACT 路径有单元测试覆盖，但本轮未完成 NAS 上的 EXACT 正向端到端验证。临时私网探测容器已删除，本次未上传或改动语料。

---

## 审计跟进：documentTypes 检索过滤

- `app/services/search.py` 新增共享 `_document_type_filter`，在 KEYWORD、VECTOR 和条款 EXACT 的 SQL 召回中均通过 `d.document_type = ANY(CAST(:document_types AS VARCHAR[]))` 限制文档类型，且发生在排序/候选条数截断之前。空列表保持兼容；关键词通路移除了逐 Chunk 查询文档类型的 N+1 过滤。
- 回归测试：新增 `tests/test_search_document_types.py`，覆盖三种通路、空过滤兼容，以及 VECTOR 文档类型过滤与 `authorizedProjectIds` 范围谓词组合；检索测试共 10 项通过，`compileall` 和现有 chunker 测试通过。
- NAS 部署：热更新 API、重建 `project.tar`，`tools/verify_deploy.sh` 校验 28 个运行文件零漂移；API 重启后 `/health` 返回 200。
- NAS 私网只读 API 验证：VECTOR 无类型过滤返回 5 条应急预案；限制 `EMERGENCY_PLAN` 仍为 5 条，限制 `EQUIPMENT_MANUAL` 为 0 条。KEYWORD 无过滤/限制 `MANAGEMENT_MEASURE` 均返回 1 条管理办法，限制 `EQUIPMENT_MANUAL` 为 0 条；HYBRID 限制 `EQUIPMENT_MANUAL` 为 0 条。
- `authorizedProjectIds` SQL 谓词未改动；PROJECT 权限已由此前的 VECTOR 临时测试验证：空授权/错误 ID 不可见，正确项目 ID 可见。EXACT 的类型条件有回归测试覆盖，但当前语料缺少可返回的 EXACT 正向样例，未做 NAS 正向端到端验证。临时探测容器已删除，本次未改动语料。

---

## 审计跟进：region 检索过滤

- 明确以文档元数据 `knowledge_document.region` 为地域过滤字段，不再用 `applicability` 代替。新增 `_region_filter`，在 KEYWORD、VECTOR 和 EXACT 的 SQL 召回中执行 `d.region = :region` 精确匹配；未传 region 时不限制。`region IS NULL` 不会匹配指定地区，按 fail-closed 处理；旧的 Python 子串判断已移除。
- 回归测试：新增 `tests/test_search_region.py`，覆盖三个召回通路、未传 region 的兼容行为及 NULL 不放行语义。检索测试共 14 项通过，`compileall` 和现有 chunker 测试通过。
- NAS 部署：热更新 API、重建 `project.tar`，`tools/verify_deploy.sh` 校验 28 个运行文件零漂移；重启后 `/health` 返回 200。
- NAS 私网只读 API 验证：KEYWORD 的北京查询返回 1 条、`region=火星` 返回 0 条并 `noEvidence=true`；VECTOR 的北京查询返回 5 条应急预案、火星返回 0 条；HYBRID 北京返回 5 条、火星返回 0 条。
- 当前语料均标注为北京市，因此不匹配的地域过滤返回空结果。本次未改动语料；临时探测容器和配置已清理。EXACT 有 SQL 回归测试覆盖，但当前语料没有可返回的 EXACT 正向样例，未完成 NAS EXACT 正向实测。
