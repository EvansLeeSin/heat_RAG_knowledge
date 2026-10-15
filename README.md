# Heat RAG Knowledge

供热公共知识服务与离线检索评测工具。通过独立 Knowledge API 提供文档入库、关键词／向量／混合检索及预算上下文，供 `heat-parent` 的 Agent 调用。

## 目录

- `app/`：FastAPI、检索、文档处理及 Celery 任务。
- `config/`、`db/`、`migrations/`：词典、数据库初始化及迁移参考。
- `eval/`：版本化题库、公开语料、快照校验、检索执行、离线评分及报告工具。
- `tests/`：单元与组件测试；测试替身不代表真实服务质量验收。
- `tools/`：构建、初始化和显式部署工具。

## 开发与部署

依赖见 `requirements.txt`；WSL 构建约束见 `constraints.wsl.txt`。本仓库尚未提供完整依赖锁，源码版本相同不代表运行环境完全相同。

- [服务设计](HEAT_KNOWLEDGE_SERVICE_DESIGN.md)
- [WSL 部署说明](WSL_DEPLOYMENT_GUIDE.md)
- [上下文预算](CONTEXT_BUDGET_GUIDE.md)
- [评测实施计划](EVALUATION_IMPLEMENTATION_PLAN.md)

仅 `.env.example` 是可提交的占位配置。数据库、API、Embedding 和 NAS 凭据必须通过本地受保护配置或运行时环境提供。不得直接使用示例密码部署。

**Compose 必须显式使用 `-f docker-compose.wsl.yml` 或 `-f docker-compose.yml`，并遵循对应部署说明。** 本机可能残留不纳入版本控制的旧 NAS YAML；不要依赖自动选择 Compose 文件。

`tools/deploy.sh` 和 `tools/verify_deploy.sh` 要求显式提供 `UGOS_HOST`、`UGOS_USER`、`UGOS_PASSWORD`，没有默认账号或密码。执行这些工具会访问 NAS，源码提交不代表已获部署授权。

## 评测入口

1. [题库与请求条件](eval/DATASET_GUIDE.md)
2. [离线快照及证据预检](eval/SNAPSHOT_GUIDE.md)
3. [检索执行器](eval/RUNNER_GUIDE.md)
4. [离线评分口径](eval/METRICS_GUIDE.md)
5. [报告与比较](eval/REPORT_GUIDE.md)

`eval/datasets/public-seed-beijing-v1/` 是三题公开开发集，包含配套公开资料快照；它不是冻结验收集。通用导出快照、真实运行、报告、日志及临时响应默认不提交 Git。

评测服务、案例范围、凭据、语料装载及真实调用均须显式配置并获得授权。不要把一次提交、测试通过或历史验证记录视为已完成真实基线验收。

## 版本与比较

本次首次提交建立当前源码基线，不还原此前未由 Git 记录的修改历史，也不宣称已经实现统一 Agent 执行器或 FULL 采集。

后续评测应分别记录被测服务版本、部署证据、执行器／评分器版本、数据集、语料快照、配置及唯一运行 ID。同一源码版本可有多次运行；评分重做也应生成独立批次。Git commit 不能单独证明运行服务版本或实验条件相同。

## 本地检查

在已安装项目依赖的隔离环境中执行：

```bash
python -m unittest discover -s tests -v
```

真实数据库、NAS、外部 Embedding 或模型验收另行执行，不由提交或推送自动触发。
