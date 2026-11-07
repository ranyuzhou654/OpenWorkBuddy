import { Folder, Sparkles } from "lucide-react";
import { useState } from "react";
import { api, type Info, type Project } from "./api";
import { Composer } from "./Composer";
import { HomeAmbient } from "./HomeAmbient";
import { Select } from "./Select";

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

  const submit = async (text: string, _delivery: unknown, files: File[],
    onProgress?: (loaded: number, total: number) => void) => {
    const s = await api.newSession(project?.id ?? null, model || undefined);
    await api.sendWithFiles(s.id, text, files, "auto", onProgress);
    location.hash = `#/s/${s.id}`;
  };

  return (
    <div className="home-stage">
      <HomeAmbient />
      <div className="home">
        <div className="home-intro">
          <h1>今天想完成什么？</h1>
        </div>
        <Composer projectId={project?.id ?? scratch?.id ?? null} submit={submit} onError={onError}
          ctx={{ sid: null, session: null, info, busy: false, entries: {}, leaf: null, refresh: () => {}, toggleTree: () => {} }}
          placeholder="描述任务，回车开始；输入 / 查看命令和技能">
          <label className="composer-pill" title={project?.root_path ?? "不进项目，在对话文件夹里工作"}>
            <Folder size={14} aria-hidden />
            <span className="pill-label">项目</span>
            <Select className="composer-select" label="工作项目" value={project?.id ?? ""}
              options={[
                { value: "", label: "无项目（对话）" },
                ...projects.filter((p) => !p.scratch).map((p) => ({
                  value: p.id, label: `${p.name}${p.trusted ? "" : "（未信任）"}`,
                })),
                { value: "+", label: "＋ 添加项目…" },
              ]}
              onChange={async (next) => {
                if (next !== "+") return setChoice(next || null);
                const added = await onAddProject();
                if (added) setChoice(added.id);
              }} />
          </label>
          <label className="composer-pill" title="模型">
            <Sparkles size={14} aria-hidden />
            <span className="pill-label">模型</span>
            <Select className="composer-select" label="模型" placeholder="选择模型"
              value={model || info?.default_model || ""}
              options={(info?.models ?? []).map((m) => ({ value: m.name, label: m.name }))}
              onChange={setModel} />
          </label>
        </Composer>
      </div>
    </div>
  );
}
