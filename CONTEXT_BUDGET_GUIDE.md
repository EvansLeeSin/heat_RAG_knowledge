# 检索上下文字符预算（第一版）

## 范围

本功能是 `POST /knowledge/search` 的可选输出处理：在检索完成后，按正文字符预算生成供回答使用的 `context`。原始 `results` 保持不变，便于复核检索与上下文处理两个阶段。

第一版只做按排名的前缀截断兜底，不做抽取式压缩、摘要、Tokenizer、rerank、分页续读或新增补检机制。不改变检索过滤、topK、权限、分数、证据门槛或 `noEvidence`。

## 请求

增加一个可选字段：

```json
{
  "query": "北京市暂停供热一般应在何时申请？",
  "contextCharBudget": 12000
}
```

- `contextCharBudget` 必须是正整数；0、负数、布尔值、浮点数和字符串等返回 HTTP 422。
- 不传或显式传 `null`：保持原始接口行为，不输出 `context` 字段，不执行预算处理。
- 服务端不自动采用12000；该值只是调用方的初始工程配置建议，不是校准结论。
- 预算只限制纳入 `context.snippets` 的所有 `content` 正文字符数之和。
- 字符单位统一为 Unicode 码点，即 Python `len(text)`。例如 `甲😀乙` 计为3，不是UTF-8字节数或Java UTF-16长度；Java调用方核算时应使用 `codePointCount`。
- 空格、换行和组合字符分别按码点计数，不做 `strip` 或文本归一化。
- 标题、来源、结构及JSON转义开销不计入正文预算。此预算不是token预算，也不保证适配模型窗口。

## 输出

原有 `query`、`results`、`noEvidence`、`meta` 保留原有语义。请求预算后，额外返回：

```json
{
  "context": {
    "snippets": [],
    "truncated": false,
    "omittedChunks": 0
  }
}
```

上述为空检索结果的上下文示例；非空时 `snippets` 的每项沿用 `results` 项的字段，另加该片段的 `truncated`。

整体仅增加两个处理标记：

| 字段 | 含义 |
|---|---|
| `context.truncated` | 因预算导致任一正文截短或整个chunk被省略时为true |
| `context.omittedChunks` | 完全未纳入上下文的chunk数量，按本次检索候选的身份去重后计算 |

- `context.snippets[i].truncated` 仅表示该项正文被截短，不代表其他chunk是否被省略。
- 纳入数量直接取 `len(context.snippets)`；本次有效去重候选总数为纳入数量加 `omittedChunks`，不是全库相关总数。
- 不额外输出预算回显、已用字符数、原始字符数或截断数量。
- 原始 `results` 不被去重、裁剪或改写；只在构建 `context` 时按 `(documentId, documentVersion, chunkId)` 去重，保留首次出现顺序。
- 相同正文但不同chunk身份不会在这一层合并；不新增语义去重策略。

## 兜底规则

1. 按原始检索排名处理去重后的候选，不重新排序。
2. 完整正文能装下则原样保留，不做旧的每片段1200字符限制。
3. 装不下时，保留该项正文在剩余预算内的前缀，并将该片段 `truncated` 设为true。
4. 预算耗尽后停止纳入，后续chunk计入 `omittedChunks`；不跳过长chunk去优先填入后面的短chunk。
5. 恰好在chunk边界耗尽预算时，该项 `truncated=false`；若后面仍有chunk，整体 `context.truncated=true`。
6. 部分保留的chunk算已纳入，不计入 `omittedChunks`。
7. 无检索证据时上下文为空，`truncated=false`、`omittedChunks=0`，原始 `noEvidence=true` 不变。
8. 检索或上下文构建故障返回HTTP 503，不伪装为空证据，也不静默退回未执行预算的原始上下文。

例如：三个chunk正文分别是 `甲乙丙`、`丁戊己庚`、`辛壬`，预算为5：

- 上下文包含 `甲乙丙`（未截短）和 `丁戊`（截短）。
- `context.truncated=true`，`omittedChunks=1`。
- `results` 仍含全部三个chunk的完整原文，`noEvidence` 仍由检索门槛决定。

后续如引入抽取式压缩，应放在最终预算兜底之前；第一版不引入该能力。截断只是资源兜底，不代表保留内容足以回答问题；回答模型仍需判断证据充分性，并承认省略内容不可推断。

## Agent适配边界

Knowledge字符预算版本已部署至本机WSL；部署记录见 `WSL_DEPLOYMENT_GUIDE.md`。heat-parent的Agent源码也已完成适配，但尚未确认运行实例或完成真实问答评测。

Agent显式传入预算（源码默认12000），且只把 `context` 对应的知识片段交给模型，不把原始 `results` 同时塞入上下文。旧的1200／3600正文二次裁剪已取消，整体与片段级的截断标记均传递给模型；对话原问检索适配已提交为 `b2dde991`。

以下工程边界独立存在，本功能不修改：

- Agent的Knowledge响应体128KiB限制；同时返回原始结果和上下文可能增大响应体。
- 对话工具结果 `data` 序列化后16000个Java字符限制；正文预算不包含标题和JSON开销，所以正文12000不能保证整个工具结果通过。
- 模型输入窗口、历史保存、超时及调用数量保护。

实际预算需要结合完整工具结果体积与模型配置验证，不能只依据正文长度推断。Agent启动、凭据绑定及真实回答评测仍需分步确认。

## 测试

预算单元测试与进程内API测试不访问数据库、Embedding或模型：

```bash
python -m unittest discover -s tests -p 'test_context_budget*.py' -v
python -m unittest discover -s tests -v
```

容器环境可用现有镜像、只读挂载源码并禁用网络执行；不需要重启正在运行的服务。测试覆盖未请求预算的旧响应兼容、完整保留、跨片段截断、整体省略、身份去重、Unicode码点、原始结果不变、认证、错误及检索元信息保持。

首次开发验证：修改前174个测试通过；新增35个测试通过；修改后全量209个测试通过。当时使用既有镜像的断网、只读源码临时容器，未访问真实数据库、Embedding或回答模型，未部署新代码。

2026-10-14部署验证：新构建镜像在断网、只读文件系统及只读源码挂载环境中再次通过全部209项测试，再仅重建WSL API/Worker。在线OpenAPI已包含 `contextCharBudget` 和 `context`；API健康、Worker pong、29个应用源码文件与工作区哈希一致，原3文档/10chunk/10向量及内容指纹不变。没有发送检索题或调用Embedding/回答模型。这些是工程回归与部署检查，不是检索质量或回答质量评分。
