import { useState } from "react";
import { api, type Info, type Project } from "./api";
import { Composer } from "./Composer";

// The new-chat page: pick where to work (a project, or no project) and a model, then the first message
// creates the session. Defaults to the project used most recently.
export function Home({ info, projects, initial, onAddProject, onError }: {
  info: Info | null; projects: Project[]; initial: string | null; onAddProject: () => Promise<Project | null>;
  onError: (m: string) => void;
}) {
  const recent = projects.flatMap((p) => p.sessions.filter((s) => !s.archived_at).map((s) => ({ p, at: s.updated_at })))
    .sort((a, b) => b.at - a.at)[0]?.p;
  const [choice, setChoice] = useState<string | null | undefined>(initial ?? undefined); // undefined: not chosen yet
  const [model, setModel] = useState("");
  const pid = choice === undefined ? recent?.id : choice;
  const project = projects.find((p) => p.id === pid && !p.scratch);
  const scratch = projects.find((p) => p.scratch);

  const submit = async (text: string) => {
    const s = await api.newSession(project?.id ?? null, model || undefined);
    await api.send(s.id, { text });
    location.hash = `#/s/${s.id}`;
  };

  return (
    <div className="home">
      <h1>要做什么？</h1>
      <Composer projectId={project?.id ?? scratch?.id ?? null} submit={submit} onError={onError}
        ctx={{ sid: null, session: null, info, busy: false, entries: {}, leaf: null, refresh: () => {}, toggleTree: () => {} }}
        placeholder="描述任务，回车开始；输入 / 查看命令和技能">
        <select value={project?.id ?? ""} title={project?.root_path ?? "不进项目，在对话文件夹里工作"}
          onChange={async (e) => {
            if (e.target.value !== "+") return setChoice(e.target.value || null);
            const added = await onAddProject();
            if (added) setChoice(added.id);
          }}>
          <option value="">无项目（对话）</option>
          {projects.filter((p) => !p.scratch).map((p) => (
            <option key={p.id} value={p.id}>{p.name}{p.trusted ? "" : "（未信任）"}</option>
          ))}
          <option value="+">＋ 添加项目…</option>
        </select>
        <select value={model || info?.default_model || ""} onChange={(e) => setModel(e.target.value)} title="模型">
          {info?.models.map((m) => <option key={m.name}>{m.name}</option>)}
        </select>
      </Composer>
      <p className="note">
        {project ? (project.trusted ? project.root_path : `${project.root_path} · 未信任：会话以只读档开始，不加载项目技能`)
          : "不进项目：在对话文件夹里工作，适合问答、写作和一次性的小任务。"}
      </p>
    </div>
  );
}
