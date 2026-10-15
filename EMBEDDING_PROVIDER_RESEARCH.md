# Embedding 供应商候选调研（阶段四任务 4.7）

> 调研日期：2026-09-17。结论供选型决策，最终需用中文供热语料 + 人工标注 query-document 对实测验证（见文末验证计划）。

## 一、候选模型对比

| 候选 | 类型 | 维度 | 上下文 | 中文检索（公开基准） | 部署方式 | 许可/合规 |
|---|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** | 稠密向量 | 支持 Matryoshka 自定义（可定 1024） | 32K | CMTEB-R **71.02**（官方 Qwen3 技术报告） | 本地 GPU / vLLM / Ollama；或 DashScope API | Apache 2.0，可本地化 |
| **BGE-M3**（BAAI） | 稠密+稀疏+多向量 | 1024 | 8K | C-MTEB 检索表现成熟，社区广泛用作中文 RAG 基线 | 本地 GPU（FlagEmbedding）；SiliconFlow 等 API | MIT，可本地化 |
| **阿里云 text-embedding-v4**（DashScope） | 稠密向量，OpenAI 兼容接口 | 默认 1024（最高 2048） | 8K tokens | 基于 Qwen 系列，官方口径较强 | 仅 API（约 0.0005 元/千 token） | 数据出域，需评估 |
| OpenAI text-embedding-3-large | 稠密向量 | 3072（可降维） | 8K | 多语言 MIRACL 强，但无可靠公开 C-MTEB-R 分数 | 仅 API | 数据出境（海外），供热政务数据不满足合规 |

> 注：Qwen3 官方表格中 71.02 为 Qwen3-Embedding-0.6B 在 CMTEB-R（MTEB 中文检索子集，nDCG@10）的自报分数；BGE-M3 各分数来自厂商/社区评测，测试条件不统一，横向对比仅供参考。

## 二、与本项目约束的对齐

1. **维度固定 1024**：`knowledge_embedding.vector(1024)` 与 BGE-M3（1024）、DashScope text-embedding-v4（默认 1024）直接匹配；Qwen3-Embedding 通过 instruct/dimension 参数同样可输出 1024。四个候选均兼容现有表结构。
2. **OpenAI 兼容客户端**：`app/services/embedding.py` 已按 OpenAI 兼容接口实现——本地模型经 vLLM/Ollama/TEI 起 OpenAI 兼容服务即可零改动接入；DashScope 原生兼容。
3. **数据合规（关键约束）**：供热应急预案、政府文件属敏感内部资料，**不宜出境**。OpenAI 海外 API 直接排除；阿里云等国内 API 需走数据出域评估；**本地部署是首选**。
4. **单机 Linux Compose 架构**：设计文档为单机部署。Qwen3-Embedding-0.6B（0.6B 参数，约 1.2GB 显存 fp16）最贴合资源受限的单机环境；BGE-M3（约 2.2GB 显存）也可行，且额外提供稀疏向量（可替代/补充 jieba 关键词通路，但首版暂不启用）。
5. **Reranker 生态（高优先级后续）**：Qwen3-Reranker 与 Qwen3-Embedding 同系列；BGE-reranker-v2-m3 配套 BGE-M3。两个体系均有后续 Reranker 升级路径。

## 三、推荐

- **首选：Qwen3-Embedding-0.6B 本地部署**（vLLM/Ollama 起 OpenAI 兼容服务）。理由：CMTEB-R 公开分数最高（71.02）、显存占用最小、Apache 2.0 可本地化、与现有 1024 维表结构和 OpenAI 兼容客户端零冲突、有同系列 Reranker 升级路径。
- **备选：BGE-M3 本地部署**。若 Qwen3 在实际语料上表现不佳，或后续想利用稀疏向量增强关键词通路时切换。
- **API 选项（需审批）：阿里云 DashScope text-embedding-v4**。仅当无法提供 GPU 时考虑，须先完成数据出域评估。
- **临时验证通道（2026-09-17 追加）：Gemini `gemini-embedding-001`（官方 google-genai SDK）**。仅用于开发/闭环验证（用公开的北京政府 HTML 测试数据跑通向量链路），**不用于真实生产数据**（数据出境不满足合规，且免费层速率限制不适合入库批量生成）。技术要点：官方 SDK 支持 `RETRIEVAL_QUERY/RETRIEVAL_DOCUMENT` task type 区分、输出维度 128-3072 可指定 1024、非 3072 维需手动 L2 归一化（已在 embedding.py 实现）。后续切 Qwen3 本地仅改 `.env`（EMBEDDING_PROVIDER/…）+ 跑 `workers.rebuild_indexes`。

配置示例（`.env`）：

```
EMBEDDING_API_BASE=http://localhost:11434/v1   # Ollama / vLLM OpenAI 兼容端点
EMBEDDING_API_KEY=ollama                        # 本地服务任意非空值
EMBEDDING_MODEL=qwen3-embedding:0.6b
EMBEDDING_DIMENSION=1024
EMBEDDING_VERSION=v1
```

## 四、验证计划（选型确认前必做）

用三份北京官方 HTML 入库后的真实语料：

1. 构造 ≥30 条人工标注 query→正确条款 对（覆盖：条款号查询、设备故障处置、职责分工、术语别名）；
2. 候选模型各跑一遍向量召回，统计 **Recall@5、MRR、nDCG@10**，与关键词通路基线对比；
3. 记录单条 embedding 延迟与批量吞吐，确认满足入库流水线与在线检索要求（在线查询 P95 < 500ms）；
4. 验证 RRF 融合（关键词+向量）相对单通路的提升幅度，据此决定 RRF 权重/候选数参数；
5. 择优后固定 `EMBEDDING_MODEL/DIMENSION/VERSION`，执行 `workers.rebuild_indexes` 完成切换演练。

## 五、参考来源

- Qwen3-Embedding 官方博客与技术报告：https://qwenlm.github.io/blog/qwen3-embedding/ 、https://arxiv.org/pdf/2506.05176 、https://github.com/QwenLM/Qwen3-Embedding
- 阿里云 DashScope OpenAI 兼容 Embedding 接口：https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-open-ai
- MTEB/C-MTEB 排行榜：https://huggingface.co/spaces/mteb/leaderboard
- BGE-M3 基准综述（含独立复核提醒）：https://axiomlogica.com/ai-ml/bge-m3-bge-reranker-benchmarks-dense-lexical-multi-vector-retrieval
