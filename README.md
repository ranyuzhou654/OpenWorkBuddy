# nano-workbuddy

一个用 Python 从零实现的个人 Agent 助手：代码小、边界清楚、可以真实使用。

设计文档：[docs/design/2026-10-23-architecture-plan.md](docs/design/2026-10-23-architecture-plan.md)

已完成：

- 阶段 1 核心闭环：树形 JSONL 会话日志、可崩溃续跑的 Agent 循环、OpenAI Chat 协议适配、read/write/edit/bash、开发用 CLI。教程：[01-核心闭环](docs/tutorial/01-核心闭环.md)
- 阶段 2 Web 工作台：FastAPI + SSE、React 界面、审批卡片、插话/排队、分支切换、服务重启自动续跑。教程：[02-Web工作台](docs/tutorial/02-Web工作台.md)
- 阶段 3 完整地基：权限规则表 + 只读/工作区/完全信任三档、macOS Seatbelt 与 Linux bubblewrap 沙箱、清理旧工具输出与摘要压缩、Anthropic 协议。教程：[03-权限沙箱与上下文治理](docs/tutorial/03-权限沙箱与上下文治理.md)

- 模型与密钥管理：Web 设置页管理多个连接（地址 + 一把密钥）和模型，获取模型列表、一键测试、设默认，会话里按需切换。教程：[04-模型与密钥管理](docs/tutorial/04-模型与密钥管理.md)
- 阶段 4a MCP：官方 SDK 接入 stdio / Streamable HTTP 服务器，本机服务器默认进沙箱，工具与内置工具同一权限闸门，工具多时用 `tool_search` 延迟加载，设置页增删启停与连接测试。教程：[05-MCP接入](docs/tutorial/05-MCP接入.md)
- 阶段 4b Skill 与记忆：发现 `~/.nwb/skills`、`~/.agents/skills`、项目 `.agents/skills` 下的技能，`skill` 工具按需加载、压缩后自动放回，输入框 `/技能名`；`memory` 工具维护 `USER.md` 和项目 `AGENTS.md`，设置页可查看改正。教程：[06-Skill与记忆](docs/tutorial/06-Skill与记忆.md)
- 阶段 4c：Claude Code 格式的 shell 钩子（PreToolUse / PostToolUse / UserPromptSubmit / Stop）、OpenAI Responses 协议（加密推理回传）、切换分支时自动总结离开的分支。教程：[07-钩子Responses与分支摘要](docs/tutorial/07-钩子Responses与分支摘要.md)
- 补齐内置工具：`grep` / `glob`（有 ripgrep 用 ripgrep，否则 Python 实现）、`web_fetch`（正文抽取，按站点审批）、`todo`（计划面板，压缩不丢）、`ask_user`（运行中弹出问题卡片）；并用 OpenDesign 真实模型做了冒烟测试。教程：[08-补齐内置工具](docs/tutorial/08-补齐内置工具.md)
- 会话与项目管理：新对话页（输入框里选项目，也可不选项目直接聊）、系统对话框选文件夹并询问是否信任（不信任则默认只读、不加载项目技能）、会话重命名 / 归档 / 永久删除、项目改名 / 移除、运行中实时刷新上下文用量。教程：[09-会话与项目管理](docs/tutorial/09-会话与项目管理.md)
- 界面重做：Linear 式的密度和布局、Claude 式的暖色调色板（亮 / 暗）与衬线回答，工具调用合并成可折叠的步骤组并显示耗时，字体随前端打包、离线可用。教程：[10-界面重做](docs/tutorial/10-界面重做.md)
- 账户菜单与设置页：左下角账户按钮（设置 ⌘, / 用量 / 获取帮助），设置页按分区导航（通用、模型与密钥、用量、记忆、系统、开发者、帮助、技能、连接器），新增 token 用量统计、系统状态、钩子与规则查看、手动深浅色。教程：[11-账户菜单与设置页](docs/tutorial/11-账户菜单与设置页.md)

默认“工作区”档：项目内读写不询问，命令在沙箱里运行（只能写项目目录、不能联网）；写项目外的文件或在沙箱外运行命令时会请你确认。Web 界面弹审批卡片，CLI 在终端里问 y / a / n。

## 快速开始

```bash
uv sync
uv run nwb key opendesign          # 保存密钥到 ~/.nwb/secrets.json（不回显）；也可以在网页“⚙ 模型与密钥”里填
uv run nwb                         # 在当前目录作为项目，进入交互模式
uv run nwb "列出这个目录下最大的五个文件"   # 单次运行
uv run nwb -c                      # 继续最近的会话
```

Web 工作台：

```bash
cd web && npm install && npm run build && cd ..   # 构建一次前端（产物进入 Python 包的 static/）
uv run nwb serve                                  # 启动并在浏览器打开带登录令牌的链接
```

前端开发时可以另开 `cd web && npm run dev`（Vite 把 `/api` 代理到 `nwb serve` 的 8765 端口），登录链接改用 `http://localhost:5173/api/login?token=…`。

首次运行会生成 `~/.nwb/config.toml`，在其中添加其他 provider 和模型。`NWB_HOME` 环境变量可以改数据目录。

接入 Claude（Anthropic 协议）的示例：

```toml
[providers.anthropic]
protocol = "anthropic"
base_url = "https://api.anthropic.com"
api_key = "env:ANTHROPIC_API_KEY"

[models.opus]
provider = "anthropic"
id = "claude-opus-5-5"
context_window = 1_000_000
max_output = 64_000
params = { thinking = { type = "adaptive", display = "summarized" } }

# 自己的权限规则：排在预设之后，最后匹配的生效
[[rules]]
action = "bash"
resource = "git push*"
effect = "ask"

# 钩子：格式同 Claude Code，退出码 2 表示拦截（stderr 是原因）
[[hooks.PostToolUse]]
matcher = "Edit|Write"
command = '''f=$(jq -r .tool_input.file_path); [[ $f == *.py ]] && ruff format "$f" || true'''

# MCP 服务器（也可以在设置页添加）。本机服务器默认在沙箱里运行，可联网，只能写 writable 列出的路径
[mcp.fetch]
command = "uvx"
args = ["mcp-server-fetch"]
writable = ["~/.cache/uv", "~/.local/share/uv"]
```

交互模式下按 Ctrl-C 取消当前运行（会结束它启动的子进程），在空提示符下按 Ctrl-C 或 Ctrl-D 退出。输入 `/help` 查看 `/tree`、`/edit`、`/checkout`、`/fork` 等命令。

## 开发

```bash
uv run pytest
uv run ruff check
```
