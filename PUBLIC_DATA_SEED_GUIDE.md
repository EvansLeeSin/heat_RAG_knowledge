# 本机公开供热业务种子语料

## 用途与范围

按用户要求寻找少量真实、公开的供热业务数据，通过本机正常入库API持久化，供接口体验和后续评测集建设使用。本次不是正式评测，不新增标准答案，不调用eval评分器，不修改现有数据集、业务代码、阈值或索引。

三份资料都来自北京政府官方网站，仅适用于来源所述的北京市业务场景。官方发布不等于已审核现行有效性；文件中的历史机构名称、温度/天气条件、价格和适用范围应与原文及后续政策一并核对，不能直接作为生产业务或收费依据。

## 已入库资料

| 文件 | 官方来源 | documentId | 状态 | chunk/向量数 |
|---|---|---|---|---:|
| 北京市供热采暖管理办法 | https://www.beijing.gov.cn/gongkai/zfxxgk/zc/gz/202112/t20211216_2562678.html | seed-bj-heating-measures-216 | TO_VERIFY | 6/6 |
| 北京市居民集中供热暂停和恢复供热指导意见（京管发〔2022〕22号） | https://csglw.beijing.gov.cn/zwxx/2024zcwj/202405/t20240516_3685695.html | seed-bj-pause-restore-2022-22 | TO_VERIFY | 2/2 |
| 供热常见问题汇总（网页日期2024-03-11） | https://csglw.beijing.gov.cn/sy/sycxfw/xxcx/grxxcx/201912/t20191203_804452.html | seed-bj-heating-faq-20240311 | REFERENCE_ONLY | 2/2 |

合计3份文档、8436个正文字符、10个chunk、10条向量。3个入库任务均SUCCEEDED，文档活动版本均为1。真实检查每条向量为1024维、有限且非零，模型Qwen/Qwen3-Embedding-4B、版本sf-qwen3-4b-1024-v1；上传文件仍存在，没有清理本次业务数据。

地域均为北京市，权限PUBLIC、projectIds为空；管理办法类型为MANAGEMENT_MEASURE，其余两份归OTHER。实际数量来自当前切分器，不是按法条数量推算。

## 文本与来源保留

原始官网HTML、正文规范化HTML、来源链接、页面日期、哈希、入库任务与检索响应保存在Linux目录：

```text
/home/Evans/.local/share/heat-knowledge-wsl/public-seed/20261012T071450Z/
├── raw/                                  # 三份原始官网网页
├── normalized/                           # 实际上传的UTF-8正文HTML
├── fetch-records.json
├── manifest.json                         # 元数据、来源与规范化哈希
├── ingestion-records.jsonl                # 接受/处理状态
├── database-verification.json            # 版本、chunk、向量验证
├── parsed-chunks.json                     # 本次三个documentId的实际解析片段
├── text-preservation-verification.json    # 正文段落保留检查
└── query-verification.json                # 九次主题查询的原始请求/响应，无密钥
```

只提取官网`.view.TRS_UEDITOR`正文，移除导航、脚本、样式和页脚；管理办法的公布/施行说明逐字取自官网元数据。没有生成、摘要或改写条款，保留天气、适用条件和收费说明。原正文提取阶段验证没有漏掉非段落文本；解析入库后，对有序chunk去重叠并进行忽略空白的段落存在性检查，96/24/19个段落全部存在（包括管理办法公布/施行说明）。这不是版式或法律有效性审查。

网页日期单独记录在manifest，不擅自视作原始发文日期。当前正常上传API虽然schema接受publishedAt/effectiveFrom/effectiveTo，其INSERT并未写入这些字段，本次没有假称日期已落库；日期证据仍在原网页/manifest及管理办法正文说明中。captured_at不能当成发文日期。

## 检索验证：有改善空间，不隐瞒失败

使用正确请求字段retrievalMode，避开条款号EXACT匹配，topK=10；查询地域北京市，显式允许TO_VERIFY/REFERENCE_ONLY。后两组还使用documentTypes=[OTHER]。

| 主题查询 | 指定演示文档 | KEYWORD | VECTOR | HYBRID |
|---|---|---|---|---|
| 采暖期 | 管理办法 | 找到 | 未找到指定文档；仅返回暂停/恢复指导意见 | 找到 |
| 暂停供热申请 | 暂停/恢复指导意见 | 找到 | 找到 | 找到 |
| 卧室起居室温度 | 常见问题汇总 | 找到 | noEvidence=true，最高相似度0.362，低于0.51 | 找到 |

9次HTTP请求均200。此处只验证指定主题文档是否出现，没有逐chunk金标准，不能把7/9当作正式质量分数。VECTOR实际召回候选存在；未找到目标不是入库失败，不能由此确定唯一原因。短查询、长chunk、模型/查询形式和现有阈值需通过后续正式评测区分。本次保持0.51/0.02不变，原始响应和未通过项都已保存，不以筛选查询或调阈值掩盖问题。

例如调用正常接口：

```json
{
  "query": "暂停供热申请",
  "retrievalMode": "HYBRID",
  "region": "北京市",
  "effectiveStatus": ["TO_VERIFY", "REFERENCE_ONLY"],
  "topK": 10
}
```

接口为http://127.0.0.1:18000/knowledge/search，X-API-Key使用本机查询密钥。不在文档展示密钥。若只过滤CURRENT，本次三份资料不会出现，这是预期的状态过滤。

## 与原评测集的关系

这些文档用seed-bj-*编号，不占用eval/datasets/beijing-v1中的doc000...编号，也没有改动该题库及哈希。

**不要直接在这个已有业务种子库里混装原评测集并宣称可跑原81题基线。** 原评测器会校验文档身份/白名单，重复内容或额外文档可能导致快照外命中和中止。后续应选择独立评测数据库/部署，或另行审核并创建绑定这些种子文档的新开发数据集。没有授权前不要清空用户当前业务数据。

目前是向量精确扫描的小库，没有新增/训练IVFFlat，适合本次少量数据；性能优化另行评估。
