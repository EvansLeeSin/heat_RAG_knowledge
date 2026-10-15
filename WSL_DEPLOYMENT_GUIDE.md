# WSL 本机 Docker 部署

本部署不连接 NAS、不安装 Portainer，数据库/查询/管理凭据独立生成，也不装载正式评测集。云端Embedding使用用户提供并授权的SiliconFlow密钥，只存本机受保护配置。随后已按用户要求装载三份真实公开业务种子资料，非带标准答案的评测集，详见PUBLIC_DATA_SEED_GUIDE.md。

## 部署结构

- 主机：Fedora 44 / WSL2，原生 Docker Engine，由 systemd 管理。
- 显式配置：`docker-compose.wsl.yml`，项目名 `heat-knowledge-wsl`。
- 服务：API、Celery Worker、PostgreSQL 16 + pgvector、Redis 7，以及运行一次的数据库初始化容器。
- API：`http://127.0.0.1:18000`；文档 `/docs`，健康检查 `/health`。
- API 仅绑定回环地址。Windows 通常可通过 WSL localhost 转发访问，但没有验证其他电脑访问，也没有开放局域网端口。
- PostgreSQL / Redis 没有发布宿主机端口；初始化任务成功退出是正常状态。

## 配置与数据

配置放在 Linux 文件系统，不提交到项目，不在文档中展示密钥：

```text
/home/Evans/.config/heat-knowledge-wsl/runtime.env        # 权限600，目录700
/home/Evans/.local/share/heat-knowledge-wsl/storage/      # 入库文件
/home/Evans/.local/state/heat-knowledge-wsl/              # 构建日志及部署验证记录
```

PostgreSQL / Redis 使用独立 Docker named volumes，位于 WSL Docker 存储中。不要用 `down -v` 删除数据卷。备份时数据库、存储文件和配置必须一起考虑；本轮没有迁移 NAS 数据。

`runtime.env` 包含新生成的数据库密码、查询密钥和管理密钥。HTTP 请求通过 `X-API-Key` 提供对应密钥。不要把密钥写进聊天、共享命令或日志；Docker 管理权限仍可读取容器环境，视为高权限访问。

Compose未配置模型时默认 `EMBEDDING_PROVIDER=none`。当前已按用户授权启用以下云端配置：

```text
EMBEDDING_PROVIDER=openai
EMBEDDING_API_BASE=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIMENSION=1024
EMBEDDING_VERSION=sf-qwen3-4b-1024-v1
```

客户端会在base后追加 `/embeddings`，不要把完整 `/v1/embeddings` 当成base。密钥仅在 `runtime.env`，不写进本文或Compose。1024维已通过真实API及数据库验证；CPU PyTorch供文档解析使用，Embedding实际在云端执行。文档和查询文本会发到SiliconFlow并产生用量，接入非公开资料前另行确认。

API/Worker已重建容器以加载新配置。原 `minSimilarity=0.51`、`minTsRank=0.02` 保持不变，但它们未经当前4B模型重新验证，不据本次连通性探针宣称阈值合适或质量达标。

## 管理命令

以下是基础部署命令。**当前已启用文末所述的受限代理覆盖配置，日常重建API/Worker须使用该节的本地包装脚本，否则会丢失代理环境。**

在 `heat-knowledge-service` 根目录执行：

```bash
# 构建：CPU PyTorch、兼容原项目固定依赖的 Docling/Google SDK 约束。
# 仅构建过程使用 host 网络访问本机代理；业务容器仍在私有 bridge 网络。
bash tools/wsl-build.sh

# 启动或应用修改（请先完成镜像构建）
bash tools/wsl-compose.sh up -d --wait --wait-timeout 180

# 状态及日志
bash tools/wsl-compose.sh ps --all
bash tools/wsl-compose.sh logs --tail 100 knowledge-api knowledge-worker

# 暂停，不删数据
bash tools/wsl-compose.sh stop

# 修改源码并更新容器
bash tools/wsl-build.sh
bash tools/wsl-compose.sh up -d --force-recreate --wait --wait-timeout 180
```

脚本明确选择 WSL Compose 与 Linux 配置文件，绝不依赖 `docker compose` 默认的文件选择。仓库中有多个历史 NAS YAML，**不要直接运行不带 `-f` 的 `docker compose up`**。

当前账号未加入 docker 组，脚本使用 `sudo -n docker`（本主机已有免密 sudo）。不要使用 `sudo bash tools/wsl-compose.sh`，否则默认配置路径会变成 root 的 HOME；需要覆盖路径时设置 `KNOWLEDGE_WSL_ENV_FILE`。

更改 API 密钥或 Embedding 配置后需要重建容器环境（`up -d --force-recreate`），只执行 `restart` 不会更新环境变量。更改配置中的 PostgreSQL 密码不会自动修改已初始化数据库的角色密码，须同步处理，不能只改文件。

## Docker 与出网

安装了 Fedora 软件源的 `moby-engine`、Docker CLI、Compose 和 Buildx，并启用了 `docker.service`。WSL 启动后由 systemd 启动 Docker；WSL 关闭后服务也停止，这不是 Windows 独立后台服务。

本机出网代理为 `http://127.0.0.1:7890`。Docker 镜像拉取配置在 `/etc/systemd/system/docker.service.d/proxy.conf`；构建脚本读取当前 `HTTP_PROXY` / `HTTPS_PROXY`。代理关闭或端口改变后，拉取/构建可能失败，需同步调整并重启 Docker。业务容器没有继承此本机回环代理。

`.dockerignore` 只放行镜像构建所需的源码与配置，排除 NAS 凭据、日志、归档、虚拟环境和评测资料。原 `requirements.txt` 不变；CPU部署的兼容约束在 `constraints.wsl.txt`，实际安装版本留在部署验证目录。

初始化只创建扩展、ORM表及关键词/范围索引，不修改业务语料。不在空库训练 IVFFlat；真实向量入库后再规划向量索引创建/重建。

## 验证记录

本机已完成真实部署与验证：

- Docker Engine 29.8.2、Compose 5.5.1；Python 3.12.15 CPU镜像。
- API、Worker、PostgreSQL、Redis运行，初始化容器退出0；宿主机 `/health` 返回 `ok` 且数据库连接成功，Worker返回 `pong`。
- 容器内154个 unittest全部通过（原152个加2个WSL初始化测试），`pip check`通过。
- 首次部署探针完成真实上传202、Worker异步入库SUCCEEDED及条款匹配；临时文档/原文件已清理，文档数从0恢复为0。**更正：首次探针误用 `mode`（接口忽略并采用默认HYBRID），查询含条款号会触发EXACT，原先将它描述为KEYWORD专项验收不准确。原记录保留，专项验证见下节。**
- 无查询密钥返回401，管理密钥用于查询返回403。
- 镜像内28个应用Python文件与工作区逐文件哈希一致；未修改 `app/` 业务代码。
- 初次部署没有连接NAS、调用外部Embedding、迁移旧数据或装载正式评测集；随后按用户授权完成下述云端验证，本机仍无正式知识文档。

验证详情和镜像/源码身份留在 `/home/Evans/.local/state/heat-knowledge-wsl/deployment.json`、`verification.json`、`tests.log`、`installed-requirements.txt`。

### 云端Embedding专项验证（已完成）

- 用两条虚构文本调用用户指定模型，返回两条1024维、有限且非零的向量。
- 一份临时虚构HTML经真实API上传、Worker解析/云端向量化、数据库写入，1个chunk对应1条1024维向量；模型/embedding版本正确。
- 使用无条款号的文本以及正确请求字段 `retrievalMode` 验证三模式：KEYWORD=关键词1/向量0，VECTOR=关键词0/向量1，HYBRID=关键词1/向量1。返回方法分别为KEYWORD/VECTOR/HYBRID，分数类型分别为TS_RANK_CD/COSINE_DISTANCE/RRF；均非EXACT，HYBRID无声明降级。
- VECTOR/HYBRID的探针最高相似度0.7491，只是连通性记录，不是质量基线或阈值标定。
- 临时文档、原文件及向量已清理，文档数和向量数都恢复为0。认证与健康检查通过，没有改检索代码或阈值。

新增专项记录：`/home/Evans/.local/state/heat-knowledge-wsl/embedding-verification.json`、`embedding-deployment.json`；初次部署记录保持不变。旧配置已以600权限备份在配置目录，注意备份同样包含凭据。密钥在聊天明文提供过，建议后续轮换并同步本机配置，不要继续回显。

### 公开业务数据装载（随后授权，已完成）

已通过正常入库API写入北京政府官网的供热采暖管理办法、暂停/恢复供热指导意见、供热常见问答，合计3份文档、10个chunk、10条1024维向量，当前不再是空库。源文件与数据均保留；网页/入库/查询证据在本机public-seed目录。两个文件TO_VERIFY，历史问答REFERENCE_ONLY，没有将公开发布等同于现行有效。

关键词/HYBRID主题查询均找到指定来源；纯向量三组中两组未找到指定文档（一组被阈值过滤为空），原始响应保留，未修改算法或阈值。见PUBLIC_DATA_SEED_GUIDE.md。

健康检查、临时探针及主题查询不等价于真实检索质量基线；正式数据集/快照/评分实验需后续明确授权。已有seed-bj-*资料与原评测集doc000...身份不同，不得默默混装或清空当前业务数据。

### RRF返回分数修复（用户另行授权，已部署）

修复app/services/search.py：两路均有候选时，将rrf_fuse返回的entry.score写入结果score，与scoreType=RRF一致；单路仍返回TS_RANK_CD原始分数或COSINE_DISTANCE距离，EXACT仍为EXACT_MATCH/1.0。原始RankedItem.score不变，门槛继续使用原关键词分数/向量相似度，不用RRF值判无证据。没有增加channelScores字段、修改sparse全词AND、词典、RRF参数或阈值。

新增tests/test_search_rrf_scores.py的12个回归测试，全量166个测试在Python3.12镜像依赖环境通过。覆盖RRF公式、混合通路成员、去重和顺序、API序列化、单路降级、原始信号门槛、EXACT及topK。构建新镜像后仅重建API/Worker，未重建数据库/Redis或运行初始化；API健康、Worker pong、容器search.py与工作区哈希相同。

真实诊断查询“采暖期”：关键词7候选、向量10候选，最终7条均按原通路排名核算出正确RRF。第一条原score=0.375，修复后=1/61+1/63=0.032266458495966696。修复前后原通路候选/分数/排名、最终chunk顺序、gate meta及语料/向量哈希相同，保留3文档/10chunk/10向量。纯KEYWORD/VECTOR接口也验证返回原分数/距离。

另复查最初自然问句：仍是关键词0/向量10，返回相同5chunk且顺序、非score字段、门槛阈值及通过/过滤数不变。但距离与历史值不完全相同（最大差约0.000867，最高相似度0.6697变为0.6701），原先要求与历史响应完全相等的附加检查失败。该变化已记录，原因未在本次定位，不能声称历史浮点值完全可复现；没有为通过检查改阈值或掩盖差异。

记录位于/home/Evans/.local/state/heat-knowledge-wsl/rrf-fix/：tests.log、build.log、before.json、after.json、single-channel-verification.json。属于修复验收，不是正式质量评测；旧walkthrough响应不覆盖，也没有推进用户分步评测的下一项。

### 字符预算版本部署（2026-10-14，用户另行授权）

已部署 `CONTEXT_BUDGET_GUIDE.md` 所述可选 `contextCharBudget` / `context` 契约；仅应用源码 `app/api/search.py` 和新增 `app/services/context.py` 相对原运行镜像变化，不修改检索算法、语料或凭据。

- 使用 `tools/wsl-build.sh` 构建，新镜像在断网、只读文件系统/源码挂载的临时容器中通过全部209项测试。
- 通过 `tools/wsl-compose.sh up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 knowledge-api knowledge-worker` 仅更新API/Worker，未重建PostgreSQL、Redis或重跑初始化。
- 在线OpenAPI包含请求 `contextCharBudget` 与响应 `context`；API健康，Worker ping返回pong，两容器29个应用Python文件均与工作区一致。
- 部署前后环境及 `runtime.env` 哈希一致，数据库/Redis容器ID不变；3文档/10chunk/10向量，以及文档、chunk、向量和活动版本指针的完整行指纹均保持不变。
- 保留旧镜像标签 `heat-knowledge-service:wsl-pre-context-20261014t070707z` 作为回退点；无需删除数据卷或恢复数据库。
- 记录位于 `/home/Evans/.local/state/heat-knowledge-wsl/context-budget-20261014T070707Z/`：`build.log`、`tests.log`、`before.json`、`after.json`、`fingerprint.sql`。目录700，验证JSON仅含脱敏信息/哈希，不保存密钥值。

本轮没有发送检索题、调用Embedding或回答模型，也未启动Agent；不代表端到端问答评测已通过。Agent的服务地址、模型凭据和实际生效配置须随后单独确认。

### 受限SiliconFlow运行时代理（2026-10-14，随后授权）

首次真实Agent问答的Knowledge调用失败；独立诊断中，直连Embedding等待60.41秒超时，相同参数经现有代理取得1024维向量。后一次仅为单次路径诊断，不是性能基线，也不能单独证明故障唯一原因。

随后仅在部署层新增受限转发器并重建API/Worker，不修改应用源码、模型、维度、版本、语料、向量或Agent超时：

- API/Worker的 `HTTPS_PROXY` 指向 `http://172.18.0.1:17891`，`NO_PROXY` 覆盖本机及内部服务名。HTTPX客户端采用代理，内网HTTP/HTTPS仍直接连接；PostgreSQL和Redis不经HTTP代理。
- 新转发器只绑定Knowledge网桥网关，仅接受该网段来源，并仅允许 `CONNECT api.siliconflow.cn:443`；上游为宿主机 `127.0.0.1:7890`。不记录请求头、正文或凭据，TLS仍端到端验证。原DeepSeek转发器未修改。
- 其他域名及普通HTTP代理请求被拒绝。因此未来新增外部HTTPS依赖（例如模型下载）需单独评审，不能默认放开全部出网。

本机覆盖文件、转发器和操作说明位于 `/home/Evans/.config/heat-knowledge-wsl/`。后续操作使用：

```bash
bash /home/Evans/.config/heat-knowledge-wsl/compose-with-proxy.sh ps --all
bash /home/Evans/.config/heat-knowledge-wsl/compose-with-proxy.sh up -d --no-build --no-deps --wait embedding-proxy
bash /home/Evans/.config/heat-knowledge-wsl/compose-with-proxy.sh up -d --no-build --no-deps --force-recreate --wait knowledge-api knowledge-worker
```

包装脚本显式组合原WSL Compose与仓库外覆盖文件，不含密钥。不要直接打印解析后的完整Compose配置；验证语法使用 `config --quiet`。网关/网段或宿主机代理端口变更时须同步核对配置。

部署后API健康、字符预算契约存在、Worker pong；两个服务的实际客户端均通过代理访问模型清单成功。非白名单目标、普通HTTP转发及另一Docker网段的实际连接均被403拒绝。最初健康脚本的转义错误已在重建API/Worker之前修正；强制回环源地址的网络检查超时，无结论，旧记录保留，已用真实另一网段的检查替代并通过。

数据库、Redis、初始化容器及Agent未重建，凭据文件和非代理环境不变；3文档/10chunk/10向量及活动版本完整行指纹一致。记录位于 `/home/Evans/.local/state/heat-knowledge-wsl/embedding-proxy-20261014T093316Z/`。

本次部署验收仅做健康/配置/模型清单检查，没有生成向量、调用回答模型或重跑第1题。端到端恢复仍需经授权用新会话验证，不能用健康状态代替质量结果。
