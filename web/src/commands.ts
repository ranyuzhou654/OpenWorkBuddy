// Slash commands typed into the composer. They act on the page (switch the model, compact, rewind ...)
// and never reach the model as text, except /init, which sends a prepared request. Skills share the
// "/" menu; a command wins when a skill has the same name.

import { api, type Entry, type Info, type SessionInfo } from "./api";
import { pathTo, textOf } from "./session";

export interface Ctx {
  sid: string | null; // null on the new-chat page: only the commands without `session` apply
  session: SessionInfo | null;
  info: Info | null;
  busy: boolean;
  entries: Record<string, Entry>;
  leaf: string | null;
  toggleTree: () => void;
  refresh: () => void;
}

export interface Option {
  value: string;
  label?: string;
}

export interface Command {
  name: string;
  args?: string; // shown after the name; "<...>" is required, "[...]" optional
  description: string;
  session?: boolean; // needs an open session
  idle?: boolean; // not while a run is active
  options?: (ctx: Ctx) => Option[]; // completions for the argument
  run: (ctx: Ctx, arg: string) => unknown; // a returned string is put back into the composer
}

const go = (hash: string) => (location.hash = hash);

const INIT = "分析这个项目，在项目根目录写一份 AGENTS.md，给以后在这里工作的助手看：项目是做什么的、目录结构、" +
  "怎么安装依赖、构建、运行和测试、代码约定和容易踩的坑。已经有 AGENTS.md 就在它的基础上补充和修正。" +
  "只写从代码里能确认的内容，保持简短。";

export const COMMANDS: Command[] = [
  { name: "model", args: "<模型>", description: "切换这个会话的模型（下一次运行生效）", session: true, idle: true,
    options: (c) => (c.info?.models ?? []).map((m) => ({ value: m.name, label: `${m.name}（${m.provider}）` })),
    run: (c, arg) => api.patch(c.sid!, { model: arg }) },
  { name: "permissions", args: "<档位>", description: "切换权限档：只读、工作区、完全信任", session: true,
    options: (c) => Object.entries(c.info?.presets ?? {}).map(([value, label]) => ({ value, label: `${label}（${value}）` })),
    run: (c, arg) => api.patch(c.sid!, { preset: arg }) },
  { name: "compact", args: "[要保留的重点]", description: "把较早的对话压缩成摘要，释放上下文", session: true, idle: true,
    run: (c, arg) => api.compact(c.sid!, arg || undefined) },
  { name: "rewind", description: "撤回上一轮：回到上一条消息之前，消息放回输入框", session: true, idle: true,
    run: async (c) => {
      const last = [...pathTo(c.entries, c.leaf)].reverse().find((e) => e.type === "user" && !e.data.synthetic);
      if (!last?.parent_id) return;
      await api.checkout(c.sid!, last.parent_id, false, false); // the turn stays in the tree as a branch
      return textOf(last.data.content);
    } },
  { name: "fork", description: "把到目前为止的对话复制成新会话", session: true, idle: true,
    run: async (c) => {
      const s = await api.fork(c.sid!, c.leaf!);
      c.refresh();
      go(`#/s/${s.id}`);
    } },
  { name: "rename", args: "<标题>", description: "给这个会话改名；不带标题则恢复自动标题", session: true,
    run: async (c, arg) => {
      await api.patch(c.sid!, { title: arg });
      c.refresh();
    } },
  { name: "init", description: "让助手分析项目，写一份 AGENTS.md", session: true, idle: true,
    run: (c) => api.send(c.sid!, { text: INIT }) },
  { name: "new", description: "在同一个项目里开新对话", session: true,
    run: (c) => go(c.session ? `#/new/${c.session.project_id}` : "#/") },
  { name: "tree", description: "显示或隐藏会话树", session: true, run: (c) => c.toggleTree() },
  { name: "stop", description: "停止当前运行", session: true, run: (c) => api.cancel(c.sid!) },
  { name: "archive", description: "归档这个会话", session: true, idle: true,
    run: async (c) => {
      await api.patch(c.sid!, { archived: true });
      c.refresh();
      go("#/");
    } },
  { name: "skills", description: "管理技能", run: () => go("#/settings/skills") },
  { name: "mcp", description: "管理连接器（MCP 服务器）", run: () => go("#/settings/connectors") },
  { name: "memory", description: "查看和改正记忆（USER.md、AGENTS.md）", run: () => go("#/settings/memory") },
  { name: "usage", description: "查看 token 用量", run: () => go("#/settings/usage") },
  { name: "settings", description: "打开设置", run: () => go("#/settings") },
  { name: "help", description: "快捷键、命令和文档", run: () => go("#/settings/help") },
];

// The commands that make sense right now.
export function available(ctx: Ctx): Command[] {
  return COMMANDS.filter((c) => (!c.session || ctx.sid) && (!c.idle || !ctx.busy) && (c.name !== "stop" || ctx.busy));
}
