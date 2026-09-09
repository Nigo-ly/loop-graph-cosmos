# 实现地图与责任

| 层 | 主要源码 | 负责什么 |
| --- | --- | --- |
| 原始输入 | `obsidian/src/runtime/main.js`、`backend/scripts/register_nigo_loops.py` | 保存链接与问题、明确进入 Loop、注册输入身份 |
| 接管与状态 | `backend/fragment_loop/continuation_bridge.py`、`intent_service.py` | 资格、目标绑定、幂等提案、路线、等待与恢复 |
| 研究执行 | `governed_research.py`、`subscription_research.py` | 来源采集、问题覆盖、模型综合、证据复核、受限重试 |
| Agent 适配 | `subscription_agent.py` | 使用已配置的 Kimi/Codex CLI，限制材料、输出、时间和工具范围 |
| 仓库试跑 | `repository_trial.py` | 固定边界的临时工作区与执行，不继承私人工作目录 |
| Graph | `backend/graph_runtime/` | 有依赖的节点执行、授权与结果状态；并非每条碎片都要升级 Graph |
| 持久状态 | `backend/common/checkpoint.py` | SQLite 追加 checkpoint 与乐观并发控制 |
| 知识 | `knowledge_library.py`、`knowledge_consolidation.py` | 验证后发布、不可变版本、稳定主题、派生目录、周期整理 |
| 本地接口 | `cognitive_server.py` | 回环 HTTP API、注册与现有后台扫描装配 |
| 人的入口 | `obsidian/src/standalone-view.js`、`src/console/homepage-cosmos.js` | 原生主页、核心判断、来源、阻塞、知识目录 |

## 三条路径

- **直接研究：** 公开链接 + 问题 → 注册 → 资格与范围核对 → verify → 采集/必要的受限试跑 → 综合与复核 → 当前结论与知识版本。新公开输入可在明确范围内自动推进；“发现链接”不等于完成问题。
- **已有结论更新：** 新证据/继续请求 → 重新判断 → 校验与 CAS 发布新版本 → 保留旧版本 → 通知。并非对互联网上任何变化都进行无期限监听。
- **周期整理与复用：** 已发布研究 → 周/月梳理 → 大类、小类、主题及缺口 → 后续归类或有范围的继续研究 → 人与 Agent 读取。汇总是派生视图，不能冒充独立研究。

## 公开整理时的取舍

公开包从当前源码建立新的 Git 历史。源码中的部分历史阶段命名、`requester=nigo` 和 `nigo-loop` 是兼容协议字段，不是 GitHub 身份认证。它仍是单用户系统。

为了离开个人环境运行，公开版将笔记前缀改为 `Notes`，从 PATH 发现 CLI，提供原生 Obsidian 视图，在既有扫描周期中注册碎片，并移除默认手机中继启动。没有搬运个人 LaunchAgent、生产配置或数据库。

`backend` 保留部分历史执行/投影模块，以保持现有运行时代码完整；快速开始只装配研究主服务。旧治理模块与额外服务入口不是已经验证的通用插件市场。公开测试集中在研究、知识和前端主路径；个人部署、历史阶段验收和真实账户操作测试留在原环境。
