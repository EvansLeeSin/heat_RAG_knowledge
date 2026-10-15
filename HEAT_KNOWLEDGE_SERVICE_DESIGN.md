# 供热知识服务设计文档

> 知识服务是独立于 `heat-parent` 的项目，不将文档解析、OCR、Embedding 和向量数据库实现并入 Java 业务项目。
>
> 当前阶段：首版范围和主要架构已确认，尚未开始编写知识服务代码。首版优先实现技术闭环，不展开业务审核、角色管理等流程；Java 接入属于下一阶段。

## 1. 建设目标与首版范围

长期目标：将供热应急预案、管理办法、运行规程、设备说明等多格式文档转换为可检索、可引用的知识，为供热对话 Agent 提供证据。

核心原则：

```text
知识服务负责“文档变成可检索知识”
heat-parent 负责“供热业务、用户权限、对话编排和最终回答”
```

### 1.1 首版交付

- 独立 Python 服务，API 与 Worker 分容器运行；
- 管理员文件上传 API，持久化保存原始文件与解析结果；
- 实际接入 Docling，以三份 HTML 验证解析、清洗和条款拆分；
- Celery + Redis 异步入库，PostgreSQL 保存任务状态；
- jieba 中文分词、供热领域词典、PostgreSQL GIN 全文索引；
- 外部 Embedding API、pgvector 向量检索、RRF 排名融合；
- 公共与项目级范围过滤、独立 API Key 认证；
- 文档版本管理、成功后切换默认版本、模型切换时全量重建；
- 返回文档、章节、条款、版本及来源，通过直接调用 API 验证；
- Linux 单机 Docker Compose 部署与持久化验证。

### 1.2 首版不做

- 不修改 `heat-parent`，不接入 Java Tool 或最终回答链路；
- 不验收 PDF、Word、Excel、图片、扫描件、OCR 或流程图理解；
- 不做独立管理后台、用户登录系统、业务角色管理、人工审核流；
- 不做设备级权限、复杂多租户、多机高可用或 Kubernetes；
- 不提供原始文件下载 API；
- 不接入 Reranker、多模型并行、无停机索引切换或独立评测平台。

上传 API 仅面向可信管理调用方，并不意味着首版开放所有终端用户上传。

## 2. 项目职责边界

本项目不负责实时遥测、泄漏确定性计算、业务告警写入、设备控制、会话保存、最终答案生成，也不自动判断文档法律效力或替代业务人员确认规程适用性。

`heat-parent` 保留用户登录与业务权限、项目/拓扑/测点、业务 Tools、对话编排和 LLM 调用。其后续通过受控 API 使用知识片段。

禁止的耦合：

```text
heat-parent 不引入 Docling/Python/OCR 运行时
heat-parent 不直接访问向量数据库
知识服务不直接访问 Heat 业务数据库
知识服务不接受任意 SQL
```

知识服务负责文件接收、解析、Chunk、Embedding、全文与向量索引、版本与来源、检索过滤及排序。多格式与 OCR 属于后续解析能力扩展。

## 3. 已确认的技术选型

| 项目 | 首版决策 |
|---|---|
| API | Python + FastAPI |
| 异步执行 | Celery Worker + Redis |
| 任务状态 | PostgreSQL 作为业务任务状态的权威来源 |
| 解析器 | 封装并实际接入 Docling，仅用 HTML 验证 |
| 中文全文检索 | jieba + 供热领域词典 + PostgreSQL `simple` / `tsvector` / GIN |
| 元数据与向量 | 独立 PostgreSQL 数据库 + pgvector |
| Embedding | 外部 API；具体供应商和模型通过选型验证确定 |
| 混合排序 | RRF；Reranker 为后续高优先级完善项 |
| 文件存储 | Linux 宿主机持久化目录，封装存储接口 |
| 认证 | 独立 API Key，查询与管理操作使用不同密钥 |
| 文档范围 | 公共或指定项目集合，Chunk 继承文档范围 |
| 部署 | Linux 单机 Docker Compose，无首版高可用要求 |

不部署 Qdrant、Elasticsearch、RabbitMQ 或 MinIO。具体依赖版本在实施预研中验证并固定。

## 4. 总体架构

### 4.1 首版检索

```text
可信接口调用方（测试脚本 / 管理工具）
    → knowledge-api：认证、参数校验、范围约束
    → PostgreSQL：全文检索 + pgvector 向量检索
    → RRF 融合、去重
    → 带文档 / 章节 / 条款 / 版本 / 来源的片段
```

查询向量通过外部 Embedding API 生成。关键词与向量通路均在召回候选时应用权限、默认版本及适用范围条件，不得仅在最终 topK 后过滤。

### 4.2 首版入库

```text
管理员上传 HTML 与元数据
    → API 校验并保存原始文件
    → PostgreSQL 创建任务与待处理版本
    → Celery 经 Redis 分发任务
    → Worker：Docling → 清洗 → 条款切分
              → jieba / 全文索引 → Embedding / pgvector
    → 全部成功后切换默认检索版本
```

API 可以接收文件，但不能在上传 HTTP 请求中同步执行 Docling 或向量化。文件保存、任务登记后返回任务 ID，通过任务接口查询进度。

## 5. 组件设计

### 5.1 knowledge-api

职责：上传校验、任务创建与查询、服务认证、检索、权限与适用范围过滤、标准化引用。管理操作与查询操作区分密钥，不向调用方暴露服务器路径或内部向量。

### 5.2 knowledge-worker

采用 Celery，Redis 用于任务分发，PostgreSQL 保存可查询的任务状态、处理阶段和错误。

必须支持：

- 失败重试、单任务超时、文件大小限制；
- 内容 Hash 幂等及并发保护；
- 解析器、分词词典、Embedding 和索引版本记录；
- 重启后任务恢复或可安全重试；
- 失败半成品不进入默认检索范围。

Celery 不保证任务只执行一次。需在应用和数据库层保证重复投递不重复入库；幂等判定必须包含文档归属和处理版本等上下文，不能仅按文件 Hash 将不同项目的文档合并。

实施时验证任务登记后投递失败、Worker 中断、重复消费等恢复路径。

### 5.3 Docling 解析适配器

```python
class DocumentParser:
    def parse(self, source_file) -> ParsedDocument:
        ...
```

首版实现并调用 `DoclingParser`，不是仅预留接口。以三份 HTML 验证中文编码、阅读顺序、标题、段落、表格和章节条款；可补充 HTML 清洗步骤去除导航、页脚等噪声。

保留解析器替换能力。PDF、Office、图片、OCR 和复杂表格后续用真实样本单独验证，不能因接入 Docling 就宣称全部格式已支持。

Docling 不负责 Embedding、数据库写入、权限、审核或最终回答。

### 5.4 Embedding

文档片段与检索问题均使用同一套外部 Embedding 模型配置，与聊天模型分离。记录：

```text
embeddingProvider
embeddingModel
embeddingDimension
embeddingVersion
indexVersion
```

供应商与模型尚未确定，以中文供热资料评估效果、成本、接口可用性和数据处理条款。文档片段与问题会发送至外部服务；实际接入内部资料前需确认其允许出网。

首版只启用一个模型。更换模型或维度时允许暂停检索，通过运维命令全量重建并验证完成后恢复，不混用不同模型或索引版本的向量，不做多模型并行或无停机切换。

### 5.5 PostgreSQL + pgvector

使用独立数据库保存文档元数据、Chunk、任务、全文索引和向量，不复用 Heat 业务表。向量必须关联具体 Chunk 与文档版本，原始文件和完整来源不能只存在向量记录中。

### 5.6 中文全文检索

```text
文档 Chunk → jieba 分词 → PostgreSQL simple 配置生成 tsvector → GIN 索引
用户问题   → 同一分词与归一化规则 → 构造安全的全文查询
```

- 首版保留真正的全文索引，不用 `LIKE` 文本包含查询替代；
- 维护供热领域词典，例如“补水定压”“一次网”“二次网”“换热站”；
- 保留分词器接口，记录词典/分词规则版本；规则变化时支持重建全文索引；
- 条款号、设备型号、标准编号保留精确匹配能力；
- 中文分词不等于同义词理解，语义差异由向量通路补充。

先验证关键词召回基线，再对比混合检索效果；向量检索仍属于首版交付范围。

### 5.7 RRF 融合

关键词和向量分别召回并排序，按 RRF 融合、按 Chunk 去重，再返回 topK。不直接相加两种通路的原始分数。

候选数量、RRF 参数和全文查询策略通过样本验证后配置。RRF 分数不是概率或置信度；无相关证据时不能仅因向量检索总能找到近邻就认定有答案，需用无答案样例验证相关性判断策略。

首版不接入 Reranker，但将其列为后续高优先级工作，以同一问题集对比排序收益和延迟成本。

## 6. 文件处理与 Chunk

### 6.1 持久化

通过配置的存储根目录保存：

```text
<storage-root>/raw/       原始文件
<storage-root>/parsed/    标准化 Markdown / JSON
```

API 和 Worker 挂载同一宿主机持久化目录，使用服务端生成的存储键，不接受任意服务器路径。文件不放在容器临时目录，也不提交 Git。首版保存文件但不提供下载 API。

记录原始文件名、媒体类型、大小、内容 Hash、官方来源 URL 或上传来源、采集/上传时间。保存 `documentId`、标题、章节、段落、表格、解析器版本等标准化结果。

### 6.2 切分

```text
文档 → 章节 → 条款 → 条款内自然段
```

完整条款尽量同 Chunk，过长条款按自然段拆分，避免切断义务、禁止和条件表达；保留章节路径、条款号，可包含上级标题以保证独立语义。

实验起点为普通段落约 600–1200 中文字符、重叠约 100–200 字符，并设置最大长度；最终结合真实问题及 Embedding 输入限制调整，不作为固定验收值。

## 7. 文档版本、业务字段和范围

### 7.1 版本生命周期

- 同一文档更新时新增版本，不覆盖历史内容；
- 新版本解析、全文索引、Embedding 和向量索引全部成功后，原子切换默认检索版本；
- 新版本失败或处理中，旧默认版本继续服务；首次入库失败的文档不可检索；
- 同一文档并发更新需保证默认版本切换一致，具体锁定策略实施时验证；
- 首版默认只检索已成功激活的版本，历史数据保留，历史查询接口后续扩展。

### 7.2 业务元数据

保留发布单位、来源、地区、适用范围、发布日期、生效区间、发布状态等字段，不建设人工审核和授权管理流程。

`effectiveStatus` 保留：

```text
CURRENT
TO_VERIFY
SUPERSEDED
REFERENCE_ONLY
```

文档是否法律有效与是否处理成功是不同概念。激活新技术版本不代表自动认定其法律效力；未知有效性使用 `TO_VERIFY`，不自动升级为 `CURRENT`。发布状态字段仅预留，不作为首版人工审核门槛。

### 7.3 文档访问范围

每份文档版本属于以下一种范围：

- `PUBLIC`：所有获准服务调用方可检索；
- `PROJECT`：关联一个或多个项目 ID。

Chunk 继承文档版本范围。检索匹配公共文档或与可信调用方授权项目集合存在交集的项目文档；空项目集合只能检索公共资料。首版不做设备或拓扑级授权。

## 8. 建议数据模型

以下为逻辑字段设计，物理表结构、约束与迁移在实现时固定。

### 8.1 knowledge_document（文档版本）

```text
document_id, version
 title, publisher, source_url, document_type, region, applicability
 effective_status, publication_status
 published_at, effective_from, effective_to, captured_at
 access_scope, project_ids
 content_hash, parser_version
 storage_path, parsed_storage_path
 created_at, updated_at
```

`document_id + version` 唯一。通过独立的文档默认版本指针或等价约束表达当前激活版本，保证每份文档只有一个默认版本，并支持事务切换。

### 8.2 knowledge_chunk

```text
chunk_id, document_id, version, chunk_index
section_path, article_no, heading, content, content_hash, token_count
access_scope, project_ids
search_vector, tokenizer_version
created_at
```

`search_vector` 为 `tsvector`，建立 GIN 索引；条款号等结构化字段按查询需要建立普通索引。范围字段由文档继承，不能独立随意填写。

### 8.3 knowledge_embedding

```text
chunk_id, embedding_provider, embedding_model, embedding_version
index_version, dimension, vector, created_at
```

`vector` 使用 pgvector 类型，维度需与当前模型匹配；具体向量索引方式按样本规模和过滤查询实测确定。

### 8.4 ingestion_job

```text
job_id, document_id, document_version, status
source_path, content_hash, parser_version, tokenizer_version
embedding_model, index_version, attempt_count
error_code, error_message
started_at, finished_at, created_at, updated_at
```

处理状态：

```text
PENDING → PARSING → CHUNKING → EMBEDDING → INDEXING → SUCCEEDED
任一处理阶段失败 → FAILED（可重试）
```

首版上传后处理，不需要 URL 下载阶段。`SUCCEEDED` 表示必需产物完整并完成默认版本切换，不等同于业务审核通过。

## 9. 首版 API

### 9.1 上传并创建入库任务

```http
POST /knowledge/ingestion-jobs
Content-Type: multipart/form-data
X-API-Key: <management-key>
```

包含 `file`（HTML）与 `metadata`（JSON）两个部分。元数据示例：

```json
{
  "title": "北京市供热突发事件应急预案（2024年版）",
  "publisher": "北京市应急管理局",
  "region": "北京市",
  "documentType": "EMERGENCY_PLAN",
  "effectiveStatus": "TO_VERIFY",
  "accessScope": "PUBLIC",
  "projectIds": []
}
```

更新已有文档时提交 `documentId`，版本由服务端分配；新建时由服务端生成 ID。官方来源 URL 作为元数据提供，不触发自动下载。首版不开放任意 URL 抓取或任意服务器文件路径导入。

保存文件、登记任务后返回 `202 Accepted`：

```json
{
  "jobId": "...",
  "documentId": "...",
  "documentVersion": "...",
  "status": "PENDING"
}
```

### 9.2 查询任务

```http
GET /knowledge/ingestion-jobs/{jobId}
X-API-Key: <management-key>
```

返回处理阶段、时间及经过清理的错误信息，不泄漏凭据或内部路径。

### 9.3 检索

```http
POST /knowledge/search
X-API-Key: <query-key>
```

```json
{
  "query": "发生供热管网泄漏后供热单位应先采取什么措施",
  "region": "北京市",
  "documentTypes": ["EMERGENCY_PLAN", "MANAGEMENT_MEASURE"],
  "effectiveStatus": ["CURRENT", "TO_VERIFY"],
  "authorizedProjectIds": [],
  "topK": 5
}
```

`authorizedProjectIds` 是可信服务调用方声明的授权上下文，不是终端用户可自行设置的查询条件。首版仅向受控测试/服务调用方发放密钥；Java 接入后由 `heat-parent` 根据用户权限计算并覆盖该字段。

响应结构示意（以下条款与内容均为占位，不代表真实文件内容）：

```json
{
  "query": "...",
  "results": [
    {
      "chunkId": "...",
      "documentId": "...",
      "documentVersion": "...",
      "title": "文档标题",
      "sectionPath": "章节路径",
      "articleNo": "条款编号",
      "content": "命中的原文片段",
      "sourceUrl": "...",
      "publisher": "发布单位",
      "publishedAt": null,
      "effectiveStatus": "TO_VERIFY",
      "applicability": "北京市",
      "retrievalMethod": "HYBRID",
      "scoreType": "RRF",
      "score": 0.032
    }
  ]
}
```

分数只用于排序，不代表置信度。无可用证据返回空结果；依赖故障应明确返回错误，不伪装为无结果。接口不返回向量、文件系统路径或可公开下载的文件链接。

## 10. heat-parent 后续对接

本节不属于首版实现或验收范围。

```text
用户问题
  → Java Agent 判断需要知识
  → heat-parent 校验权限并调用知识 API
  → 带来源的少量 Chunk
  → LLM 结合业务 Tools 结果生成答案
```

最终回答必须区分数据库观测事实、Java 计算结果、文档规定和模型推断。检索结果不替代实时设备状态，也不能转成自动控制动作。

## 11. 部署与配置

### 11.1 Linux 单机 Compose

```text
knowledge-api       FastAPI，示例端口 8000
knowledge-worker    Celery + Docling + jieba + Embedding 客户端
redis               任务分发
postgresql          元数据 + 全文索引 + pgvector
```

API 与 Worker 可来自同一仓库，使用独立容器。文件使用共享宿主机挂载目录，不部署 MinIO。只开放必要 API 入口，数据库与 Redis 不直接对外暴露。

### 11.2 持久化与恢复

- 原始文件、解析结果：宿主机持久化目录；
- 文档、Chunk、全文索引、向量、任务状态：PostgreSQL Volume；
- Redis：配置适当持久化，并验证队列故障恢复；不能仅依赖 Redis 保留业务状态；
- Docling 所需缓存：按实际依赖挂载持久化目录；首版没有本地 Embedding 模型；
- 数据和原始文件需有备份与恢复说明，索引需可重建。

首版不做多机高可用。对象存储、独立 Worker 扩容、OCR CPU/GPU 资源与 Kubernetes 均为后续按需求评估事项。

### 11.3 依赖与配置

固定 Python、Docling、jieba、Celery、Redis、PostgreSQL、pgvector 及 Embedding 客户端版本。OCR、Office 等额外运行依赖在扩展对应格式时再验证，不作为首版验收前提。

建议环境变量：

```text
KNOWLEDGE_QUERY_API_KEY
KNOWLEDGE_MANAGEMENT_API_KEY
DATABASE_URL
CELERY_BROKER_URL
STORAGE_ROOT
EMBEDDING_API_BASE
EMBEDDING_API_KEY
EMBEDDING_MODEL
EMBEDDING_DIMENSION
EMBEDDING_VERSION
INDEX_VERSION
```

凭据通过环境变量或密钥管理注入，禁止写入 Git、镜像、日志或示例 Compose。超时、文件大小、并发、候选数等参数可配置，初值经样本测试确定。

## 12. 安全边界

首版必须具备：

- 查询/管理 API Key 分离，非本机网络访问使用受保护通道；
- 文件类型、大小、文件名和路径安全校验；
- 不接受任意 URL 下载，不因 HTML 内外部资源引用绕过网络边界；
- Docling 外部资源访问策略需验证，禁止访问不受控资源；
- 单任务超时、资源约束及安全重试；
- 两条检索通路均执行公共/项目范围和默认版本过滤；
- 日志不记录凭据或不必要的文档敏感正文；
- 上传文档中的指令视为不可信内容，不能改变系统权限和执行策略。

业务用户认证、角色分配和授权界面不在首版内；不能因此省略服务认证与范围过滤。首版无下载接口，未来增加下载功能时必须同步校验权限。

## 13. 后续 RAG 回答约束

Java 接入后，LLM 应只根据检索证据陈述文档事实，重要结论附文档名、章节和版本，区分北京地方文件、项目内部资料与参考资料；`TO_VERIFY` 明确提示，无证据时明确说明。

引用链：

```text
回答 → chunkId → 章节/条款 → 文档版本 → 保存的原始文件或官方 URL
```

首版只验证到检索引用元数据和服务器端原始文件可追溯，不提供最终回答或原文下载。

## 14. 首批资料

使用三份北京官方 HTML：

1. 北京市供热突发事件应急预案（2024年版）；
2. 北京市供热采暖管理办法；
3. 北京市供热行业生产安全事故隐患排查治理暂行办法。

入库前核对官方 URL、发布单位、发布日期、适用地区、有效性、文档类型、采集时间和 Hash。不确定的信息保留待确认，不能从文件标题推断实际发布日期或效力。

正式原始文件存于知识服务持久化目录；`heat-parent` 中的临时文件不视为正式知识库。

## 15. 实施阶段与后续优先级

### 首版阶段一：骨架与基础设施

FastAPI、Celery、Redis、PostgreSQL + pgvector、Linux Compose、配置、日志、健康检查和持久化目录。

### 首版阶段二：上传与解析

上传 API、任务状态、Docling 实际接入、三份 HTML 清洗、标准化产物、章节/条款拆分、幂等和失败恢复。

### 首版阶段三：全文检索

元数据与版本模型、jieba 领域词典、GIN 索引、条款精确匹配、项目过滤、固定问题集及关键词基线。

### 首版阶段四：向量与完整 API 闭环

验证外部 Embedding 供应商和模型、生成向量、pgvector 检索、RRF、默认版本切换、暂停服务全量重建、接口与运行验收。

### 下一阶段：Java 接入

实现 `heat-parent` Knowledge Client / Tool，计算并传递可信用户范围，集成引用与最终回答约束。首版不修改 Java 项目。

### 后续高优先级：Reranker

评估并接入重排序模型，使用首版问题集验证相关性收益、延迟和成本。该项是明确的高优先级完善事项，不是首版依赖。

其他扩展按实际需求推进：多格式/OCR、原始文件下载、管理后台、审核流、对象存储、多机扩展和更完整的评测体系。

## 16. 首版验收标准

使用三份 HTML 和至少 10 个固定问题，无需独立评测平台。记录期望条款、实际召回及差异，覆盖精确条款、自然语言、无答案和范围过滤；额外用测试数据覆盖项目隔离与版本更新。

### 解析与引用

- 原始文件及解析结果可追溯；
- 标题、章节、条款可定位，中文无乱码；
- 导航和页脚噪声去除；
- 返回 Chunk、文档、版本、章节、来源与有效性字段。

### 检索与接口

- 可直接调用上传、任务查询和 search API；
- 关键词与向量通路可独立验证，RRF 融合结果可解释；
- 相关问题能够召回期望条款，无答案样例不被强制认定有答案；
- 公共/项目范围及适用地区过滤正确，两条通路均不越权；
- API Key 缺失或错误时拒绝访问，查询密钥不能执行管理操作；
- 默认检索不返回失败或未完成版本。

### 版本与运行

- 新版本全部成功后切换，失败时旧版仍可检索；
- API 与 Worker 可独立重启；
- 失败原因可查询，重试不重复入库；
- 数据库及文件在容器重建后仍存在；
- Embedding 模型切换可暂停检索并全量重建，恢复后只使用一致版本。

不承诺未经实测的准确率、吞吐量或延迟指标。不要求 Java 调用、LLM 回答、OCR、多格式或下载功能通过首版验收。

## 17. 实施前仍需验证的细节

以下不是重新开放已确认的架构选型，而是实施阶段的参数和验证工作：

1. 外部 Embedding 供应商、模型、维度、接口限制、数据条款及费用；
2. Python、Docling、jieba、Celery、PostgreSQL、pgvector 等兼容版本；
3. 三份 HTML 的实际文件、来源元数据和至少 10 个问题的期望条款；问题集长期维护人尚未指定；
4. Linux 环境资源、存储目录、备份位置、网络及外部 API 可达性；
5. 文件大小、处理超时、并发、重试、Chunk 和 RRF 参数；
6. 全文查询策略、无答案判断、向量索引方式和幂等/任务恢复的具体实现。

这些细节通过预研和样本测试收敛，不在缺乏证据时写死容量或效果承诺。

## 18. 后续实施启动建议

```text
1. 阅读本文件，以已确认首版范围为准
2. 验证依赖版本、Linux Compose 和外部 Embedding 候选接口
3. 建立 API / Celery Worker / Redis / PostgreSQL 骨架
4. 接入 Docling，跑通三份 HTML 上传与解析
5. 用 jieba 全文检索验证 Chunk 和条款质量
6. 加入 pgvector、RRF、版本切换及完整验收
7. 首版不修改 heat-parent；Java 与 Reranker 按后续路线推进
```
