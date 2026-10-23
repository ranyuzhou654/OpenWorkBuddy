# nano-workbuddy

一个用 Python 从零实现的个人 Agent 助手：代码小、边界清楚、可以真实使用。

设计文档：[docs/design/2026-10-23-architecture-plan.md](docs/design/2026-10-23-architecture-plan.md)

当前处于阶段 1（核心闭环）：树形 JSONL 会话日志、可崩溃续跑的 Agent 循环、OpenAI Chat 协议适配、read/write/edit/bash 四个工具、开发用 CLI。**此阶段没有沙箱，工具以你的用户权限运行。**

## 快速开始

```bash
uv sync
export DEEPSEEK_API_KEY=...        # 或在 ~/.nwb/config.toml 中改用 keyring:NAME
uv run nwb                         # 在当前目录作为项目，进入交互模式
uv run nwb "列出这个目录下最大的五个文件"   # 单次运行
uv run nwb -c                      # 继续最近的会话
```

首次运行会生成 `~/.nwb/config.toml`，在其中添加其他 provider 和模型。`NWB_HOME` 环境变量可以改数据目录。

交互模式下按 Ctrl-C 取消当前运行（会结束它启动的子进程），在空提示符下按 Ctrl-C 或 Ctrl-D 退出。输入 `/help` 查看 `/tree`、`/edit`、`/checkout`、`/fork` 等命令。

## 开发

```bash
uv run pytest
uv run ruff check
```
