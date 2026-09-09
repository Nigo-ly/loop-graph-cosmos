# 本地启动

## 前提

- macOS、Python 3.11+、Node.js 22+、npm、Obsidian 桌面版。
- 推荐使用新建测试 Vault。首版默认中文目录 `Notes/散记/碎片想法`。
- 同一工作副本只运行一个后端，默认 API 端口 5684。UI 的回环端口暂不可配置。
- 不需要 Dataview；公开版使用原生 Obsidian 视图。旧个人部署脚本、手机云中继和自动开机启动不在发行包里。

## 1. 安装与离线演示

```sh
git clone https://github.com/Nigo-ly/loop-graph-cosmos.git
cd loop-graph-cosmos/backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/demo.py --vault "$PWD/demo-vault"
```

输出包含 `synthetic: true`、`current_revision: 2`、`history_length: 2` 和笔记路径。演示是知识写入/修订演示，不是自动研究演示。用 Obsidian 打开 `backend/demo-vault` 可直接读 Markdown。

## 2. 启动本地 API（模型与采集均关闭）

在 `backend` 下运行：

```sh
mkdir -p data
python -m fragment_loop.cognitive_server \
  --db "$PWD/data/loop.sqlite3" \
  --graph-db "$PWD/data/graph.sqlite3" \
  --vault-root "$PWD/demo-vault" \
  --product-candidates-dir "$PWD/demo-vault/candidates" \
  --product-assets-dir "$PWD/demo-vault/assets"
```

另开终端：

```sh
curl --fail http://127.0.0.1:5684/fragment/v1/knowledge
```

应返回包含演示知识的 `data`。`Ctrl+C` 停止服务。API 只监听回环地址，**不要映射到公网或多用户网络**。若 5684 已被使用，不要结束不认识的服务；可以用 `--port 15684` 单独测试 API，但现有 UI 仍连接 5684。

路径必须为真实目录，不能通过符号链接进入；例如 macOS 的 `/tmp` 应先解析为 `/private/tmp`。示例使用 `$PWD` 下的真实目录。

## 3. 安装工作台

```sh
cd ../obsidian
npm ci --ignore-scripts
npm run build
```

关闭该测试 Vault 中的同名插件（如果存在）。在测试 Vault 下新建 `.obsidian/plugins/my-life-homepage/`，仅复制 `dist/main.js`、`dist/styles.css`、`dist/manifest.json` 三个文件进去。在 Obsidian 设置中允许并启用 **Loop Graph Cosmos**，使用命令 **打开 My Life 主页**，或点击主页图标。

`my-life-homepage` 是保留的兼容插件 ID，不要与旧个人版本同时安装在同一 Vault。不要复制任何其他用户的 `data.json`。无需个人主页笔记；工作台直接读取该 Vault 与本地 API。请确保 API 的 Vault 和 Obsidian 打开的 Vault 是同一个。

## 4. 开启真实公开研究（明确选择后）

离线模式不会形成新的模型结论。真实研究需要用户自己安装并登录支持的 **Kimi Code 或 Codex CLI**，且当前方案允许这种调用方式。项目不提供账户、订阅或免费模型额度。

先停止后端。在 `backend` 目录生成一次固定起点，保留这个文件，不要每次启动都重新生成：

```sh
python -c 'from datetime import datetime, UTC; from pathlib import Path; p=Path("data/public-after.txt"); p.open("x").write(datetime.now(UTC).isoformat())'
```

重新启动：

```sh
python -m fragment_loop.cognitive_server \
  --db "$PWD/data/loop.sqlite3" \
  --graph-db "$PWD/data/graph.sqlite3" \
  --vault-root "$PWD/demo-vault" \
  --product-candidates-dir "$PWD/demo-vault/candidates" \
  --product-assets-dir "$PWD/demo-vault/assets" \
  --research-collect-live \
  --research-search-provider bing-cn \
  --subscription-provider kimi \
  --subscription-new-public-after "$(cat data/public-after.txt)"
```

可将 `kimi` 换为 `codex`。可执行文件从 PATH 发现，再尝试各自默认用户安装目录。凭据留在已登录 CLI 的本机配置中，不要写进此仓库。不要同时传 `--research-live`、`--research-model-live` 或 `--pilot-live`：这些是另外的付费 API 路径，当前教程不启用。

这条命令允许固定起点之后、明确勾选“进入 Loop”的合格公开输入使用选定订阅 Agent，并开启已有周期梳理。公开来源材料与用户写下的问题会提供给该 Agent；不要将私密材料误填为公开研究输入。`sensitive` / `restricted` 被排除。

在工作台“记录碎片”中输入一个 HTTPS 公开链接以及明确问题，并勾选进入 Loop。例如：

> https://github.com/pallets/markupsafe
> 这个项目适合解决 Python 输出 HTML 时的转义问题吗？说明能解决什么、不能解决什么；必要时做小范围隔离试跑，最后给出采用建议。

这是建议试验输入，不是保证答案的固定样例。后台会先注册新输入，再执行现有承接与研究链。轮转和正在运行的任务会影响延迟，**不承诺单条 60 秒完成**。观察核心结论、证据与限制；出现阻塞应查看具体原因，不能把“来源已收集”当成“问题已解决”。

## 检查和排障

```sh
# backend，先激活其 venv
python -m pytest -q
# obsidian
npm test
npm run build
```

- 主页连接失败：确认 5684 存活、Vault 一致。
- 无模型结论：确认启用了订阅范围、CLI 已登录、输入符合时间与公开资格。
- 抓取失败：站点可能拒绝请求或需要登录；不会绕过登录。
- 旧碎片不承接：固定起点是明确的前瞻范围，不应随意回拨以扩大授权。
- 试跑不可用：隔离试跑主要支持 macOS `sandbox-exec`；不是任意仓库的一键安装器。
- 旧深度控制面可能提示其他本地服务离线。这些历史调试入口不属于首版快速开始的支持路径。
