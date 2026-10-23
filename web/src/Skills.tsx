import { useEffect, useState } from "react";
import { api, type MemoryFile, type SettingsView } from "./api";

// Skills: SKILL.md directories found in ~/.nwb/skills, ~/.agents/skills, extra dirs, and each project's
// .agents/skills. Switching one off (by name) removes it from the next run's list and skill tool.
export function SkillsSection({ view, save }: { view: SettingsView; save: (p: Promise<SettingsView>) => Promise<boolean> }) {
  const [dirs, setDirs] = useState(view.skill_dirs.join("\n"));
  const groups = new Map<string, typeof view.skills>();
  for (const s of view.skills) {
    const g = s.project ? `项目 ${s.project}` : "个人";
    groups.set(g, [...(groups.get(g) ?? []), s]);
  }
  return (
    <>
      <h2>技能（Skills）</h2>
      <p className="note">
        技能是一个带 SKILL.md 的目录，放在 ~/.nwb/skills/、~/.agents/skills/ 或项目的 .agents/skills/ 下。模型只看到名字和说明，需要时用 skill 工具加载全文；
        你也可以在输入框里输入 /技能名 直接加载。关掉的技能从下一次运行起不再出现。
      </p>
      {view.skills.length === 0 && <div className="note">还没有找到技能。</div>}
      {[...groups].map(([group, list]) => (
        <div key={group} className="card">
          <div className="card-head"><b>{group}</b><span className="note">{list.length} 个</span></div>
          {list.map((s) => (
            <label key={s.path} className="skill" title={s.path}>
              <input type="checkbox" checked={s.enabled} onChange={(e) => save(api.enableSkill(s.name, e.target.checked))} />
              <b>{s.name}</b> <span className="note">{s.description}</span>
            </label>
          ))}
        </div>
      ))}
      <div className="card form">
        <label>额外的技能目录（每行一个）
          <textarea rows={2} value={dirs} onChange={(e) => setDirs(e.target.value)} placeholder="如 ~/.claude/skills（Claude Code 的技能）" />
        </label>
        <div className="row">
          <button disabled={dirs === view.skill_dirs.join("\n")} onClick={() => save(api.setSkillDirs(dirs.split("\n")))}>保存目录</button>
        </div>
      </div>
    </>
  );
}

// Memory: USER.md and each project's AGENTS.md. The model updates them with the memory tool; here the
// user can read and correct everything it saved. Sessions read them once, at their start.
export function MemorySection({ onError }: { onError: (m: string) => void }) {
  const [files, setFiles] = useState<MemoryFile[]>([]);
  const [key, setKey] = useState("user");
  const [draft, setDraft] = useState<string | null>(null);

  useEffect(() => {
    api.memory().then(setFiles).catch((e) => onError(String(e)));
  }, [onError]);

  const file = files.find((f) => f.key === key);
  const text = draft ?? file?.text ?? "";
  const save = async () => {
    try {
      setFiles(await api.putMemory(key, text));
      setDraft(null);
    } catch (e) {
      onError(String(e));
    }
  };

  return (
    <>
      <h2>记忆</h2>
      <p className="note">
        模型用 memory 工具把你的偏好记在 USER.md（所有项目通用），把项目约定记在项目的 AGENTS.md。每个会话开始时读入一次，所以修改从下一个会话起生效。
        这里可以查看和改正它记下的任何内容。
      </p>
      <div className="card form memory">
        <label>文件
          <select value={key} onChange={(e) => { setKey(e.target.value); setDraft(null); }}>
            {files.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
        </label>
        {file && <code className="cmd">{file.path}</code>}
        <textarea rows={10} value={text} onChange={(e) => setDraft(e.target.value)} placeholder="（空）" />
        <div className="row">
          <button disabled={draft === null} onClick={() => setDraft(null)}>还原</button>
          <button className="primary" disabled={draft === null} onClick={save}>保存</button>
        </div>
      </div>
    </>
  );
}
