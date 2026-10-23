# nano-workbuddy

一个用 Python 从零实现的个人 Agent 助手：代码小、边界清楚、可以真实使用。

设计文档：[docs/design/2026-10-23-architecture-plan.md](docs/design/2026-10-23-architecture-plan.md)

已完成：

- 阶段 1 核心闭环：树形 JSONL 会话日志、可崩溃续跑的 Agent 循环、OpenAI Chat 协议适配、read/write/edit/bash、开发用 CLI。教程：[01-核心闭环](docs/tutorial/01-核心闭环.md)
- 阶段 2 Web 工作台：FastAPI + SSE、React 界面、审批卡片、插话/排队、分支切换、服务重启自动续跑。教程：[02-Web工作台](docs/tutorial/02-Web工作台.md)

**还没有沙箱（阶段 3）。** Web 界面默认“写入和命令需确认”；CLI 不询问，以你的用户权限运行。

## 快速开始

```bash
uv sync
export OPENDESIGN_API_KEY=...      # 变量名取决于 ~/.nwb/config.toml 里 api_key 的 env: 引用；也可用 keyring:NAME
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

交互模式下按 Ctrl-C 取消当前运行（会结束它启动的子进程），在空提示符下按 Ctrl-C 或 Ctrl-D 退出。输入 `/help` 查看 `/tree`、`/edit`、`/checkout`、`/fork` 等命令。

## 开发

```bash
uv run pytest
uv run ruff check
```
