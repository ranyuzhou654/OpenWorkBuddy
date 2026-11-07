import { test, expect, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (error) => { throw error; });
  await page.goto("/api/login?token=e2e-local-only");
});

test("optional plugins can be switched without clearing connection settings", async ({ page }) => {
  const before = await (await page.request.get("/api/settings")).json();
  const documents = before.plugins.find((p: any) => p.name === "documents");
  try {
    await page.goto("/#/settings/plugins");
    await expect(page.getByRole("heading", { name: "可选能力" })).toBeVisible();
    const card = page.locator(".card").filter({ has: page.locator("b", { hasText: /^documents$/ }) });
    const saved = page.waitForResponse((r) => r.url().endsWith("/api/settings/plugins/documents") &&
      r.request().method() === "PATCH");
    await card.getByRole("button", { name: documents.enabled ? "关闭" : "启用", exact: true }).click();
    const after = await (await saved).json();
    expect(after.plugins.find((p: any) => p.name === "documents").enabled).toBe(!documents.enabled);
    expect(after.providers).toEqual(before.providers);
    expect(after.search).toEqual(before.search);
    await page.reload();
    await expect(card.locator(".tag")).toHaveText(documents.enabled ? "未启用" : "已启用");
    await page.goto("/#/settings/memory");
    await expect(page.locator(".note").filter({ hasText: "已有会话的下一次请求" })).toBeVisible();
  } finally {
    await page.request.patch("/api/settings/plugins/documents", { data: { enabled: documents.enabled } });
  }
});

test("Tavily settings save locally, mask the key and stay separate from model credentials", async ({ page }) => {
  let probes = 0;
  await page.route("**/api/settings/search/test", async (route) => {
    probes++;
    await route.fulfill({ json: { ok: true, seconds: .01, results: 1 } });
  });
  await page.goto("/#/settings/search");
  await expect(page.getByRole("heading", { name: "联网搜索与密钥" })).toBeVisible();
  const key = page.getByRole("textbox", { name: "Tavily API Key" });
  const probe = page.getByRole("button", { name: "测试搜索（1 次请求）" });
  await expect(probe).toBeDisabled();
  await key.fill("tvly-browser-private-4321");
  const saved = page.waitForResponse((r) => r.url().endsWith("/api/settings/search") && r.request().method() === "PUT");
  await page.getByRole("button", { name: "保存密钥" }).click();
  const response = await saved;
  expect(await response.text()).not.toContain("tvly-browser-private-4321");
  await expect(key).toHaveValue("");
  await expect(page.getByRole("status")).toContainText("密钥已保存");
  expect(probes).toBe(0);
  await page.reload();
  await expect(page.locator(".card-head")).toContainText("…4321");
  await expect(key).toHaveValue("");
  await probe.click();
  await expect(page.getByTitle("搜索可用", { exact: true })).toContainText("搜索可用");
  expect(probes).toBe(1);
  await key.fill("tvly-unsaved-value");
  await expect(probe).toBeDisabled();
  await expect(page.getByTitle("搜索可用", { exact: true })).toHaveCount(0);
  await page.goto("/#/settings/models");
  await expect(page.getByRole("heading", { name: "连接", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "联网搜索与密钥" })).toHaveCount(0);
});

async function session(page: Page, readonly = false) {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const response = await page.request.post("/api/sessions", { data: { project_id } });
  const { id } = await response.json();
  if (readonly) await page.request.patch(`/api/sessions/${id}`, { data: { preset: "readonly" } });
  await page.goto(`/#/s/${id}`);
  await expect(page.locator(".conn")).toHaveAttribute("title", "已连接");
  return id;
}

async function send(page: Page, message: string) {
  await page.locator(".composer textarea").fill(message);
  await page.getByRole("button", { name: "发送", exact: true }).click();
}

test("attachment reaches the model and remains previewable after reload", async ({ page }) => {
  await session(page);
  const picker = page.locator('input[type="file"]');
  const file = { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("ATTACHMENT_MARKER\n本地合成附件") };
  await picker.setInputFiles(file);
  await picker.setInputFiles(file);
  await expect(page.locator(".composer .attach")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "通知" }).getByRole("status")).toContainText("已忽略 1 个重复附件");
  await send(page, "E2E附件：读取附加文件");
  await expect(page.locator(".msg.assistant")).toContainText("模型读到附件 ATTACHMENT_MARKER");
  await page.reload();
  await page.getByRole("button", { name: /notes.txt/ }).click();
  await expect(page.locator(".preview-body")).toContainText("本地合成附件");
});

test("approval survives reload and executes only after the browser approves", async ({ page }) => {
  const sid = await session(page, true);
  await send(page, "E2E审批：写入合成文件");
  await expect(page.locator(".approval")).toContainText("需要你确认");
  const before = await (await page.request.get(`/api/sessions/${sid}`)).json();
  expect(before.entries.filter((e: any) => e.type === "tool_started")).toHaveLength(0);
  await page.reload();
  await expect(page.locator(".approval")).toContainText("approved.txt");
  await page.getByRole("button", { name: "允许", exact: true }).click();
  await expect(page.locator(".msg.assistant")).toContainText("本地操作完成");
  await expect(page.locator(".approval")).toHaveCount(0);
  const after = await (await page.request.get(`/api/sessions/${sid}`)).json();
  expect(after.entries.filter((e: any) => e.type === "tool_started")).toHaveLength(1);
});

test("closed SSE stream reconnects from a fresh snapshot without duplicate messages", async ({ page }) => {
  const sid = await session(page);
  let snapshots = 0;
  page.on("request", (request) => { if (request.url().endsWith(`/api/sessions/${sid}`)) snapshots++; });
  await send(page, "E2E断线：恢复持久结果");
  await expect(page.locator(".streaming")).toContainText("重连测试已开始");
  await page.request.post(`/__test__/disconnect/${sid}`);
  await expect.poll(() => snapshots).toBeGreaterThan(0);
  await expect(page.locator(".conn")).toHaveAttribute("title", "已连接");
  await expect(page.locator(".msg.assistant")).toContainText("RECOVERY_MARKER");
  await expect(page.locator(".msg.assistant")).toHaveCount(1);
  await expect(page.locator(".msg.user")).toHaveCount(1);
});

test("download returns the delivered version and validation keeps four layers separate", async ({ page }, testInfo) => {
  await session(page);
  await send(page, "E2E下载：生成报告");
  await expect(page.locator(".artifact-name")).toContainText("report.md");
  await page.getByRole("button", { name: "查看校验层次" }).click();
  for (const label of ["文件与结构", "引用与来源", "数字与计算", "人工版式与事实验收"])
    await expect(page.locator(".check-list")).toContainText(label);
  await expect(page.locator(".check-list")).toContainText("not assessed");
  await page.screenshot({ path: testInfo.outputPath("validation-layers.png"), fullPage: true });
  const downloadReady = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载这一版" }).click();
  const download = await downloadReady;
  expect(download.suggestedFilename()).toBe("report.md");
  expect(await readFile((await download.path())!, "utf8")).toContain("DOWNLOAD_MARKER");
});

test("post-delivery failure survives reload and recovers bytes without rerunning the model", async ({ page }, info) => {
  const sid = await session(page);
  let submissions = 0;
  page.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith(`/sessions/${sid}/messages`)) submissions++; });
  await send(page, "E2E交付后失败");
  const region = page.getByRole("region", { name: "已交付版本恢复" });
  await expect(region).toContainText("TimeoutError");
  await page.reload();
  await expect(region).toContainText("原运行状态：error");
  await region.getByRole("button", { name: "检查已交付版本" }).click();
  await expect(region).toContainText("delivered.md · 第 1 版");
  const downloaded = page.waitForEvent("download");
  await region.getByRole("link", { name: "下载已交付版本" }).click();
  expect(await readFile((await (await downloaded).path())!, "utf8")).toContain("KEPT_BEFORE_TIMEOUT");
  await region.getByRole("button", { name: "检查已交付版本" }).click();
  await expect(region.getByRole("button", { name: "检查已交付版本" })).toBeEnabled();
  expect(submissions).toBe(1);
  const snapshot = await (await page.request.get(`/api/sessions/${sid}`)).json();
  expect(snapshot.entries.filter((e: any) => e.data.meta?.artifact)).toHaveLength(1);
  await page.screenshot({ path: info.outputPath("delivery-recovery.png"), fullPage: true });
});

test("scheduled task distinguishes approval wait from its verified artifact result", async ({ page }) => {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const response = await page.request.post("/api/schedules", { data: { name: "E2E任务", prompt: "生成报告",
    project_id, kind: "daily", at: "23:59", catch_up: "once" } });
  const task = await response.json();
  await page.request.patch(`/api/sessions/${task.session_id}`, { data: { preset: "readonly" } });
  await page.goto("/#/tasks");
  const card = page.locator(".task").filter({ hasText: "E2E任务" });
  await card.getByRole("button", { name: "立即运行" }).click();
  await expect.poll(async () => (await (await page.request.get("/api/schedules")).json())
    .find((s: any) => s.id === task.id).waiting).toBe(true);
  await page.reload();
  await expect(card).toContainText("等待处理");
  await page.goto(`/#/s/${task.session_id}`);
  await page.getByRole("button", { name: "允许", exact: true }).click();
  await expect(page.locator(".msg.assistant")).toContainText("本地操作完成");
  await expect.poll(async () => (await (await page.request.get("/api/schedules")).json())
    .find((s: any) => s.id === task.id).last_outcome, { timeout: 20000 }).toBe("changed");
  await page.goto("/#/tasks");
  await expect(page.locator(".task").filter({ hasText: "E2E任务" })).toContainText("有新结果");
});

test("drawer and preview keep keyboard focus predictable", async ({ page }) => {
  await session(page);
  const library = page.getByRole("button", { name: "资料", exact: true });
  await library.click();
  const drawer = page.getByRole("dialog", { name: "资料" });
  await expect(drawer).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭面板" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(library).toBeFocused();

  await page.locator('input[type="file"]').setInputFiles({ name: "focus-notes.txt", mimeType: "text/plain",
    buffer: Buffer.from("FOCUS_PREVIEW_MARKER") });
  await send(page, "E2E附件：读取附加文件");
  const chip = page.getByRole("button", { name: /focus-notes\.txt/ });
  await chip.click();
  const preview = page.getByRole("dialog", { name: "focus-notes.txt" });
  await expect(preview).toBeVisible();
  await expect(page.getByRole("button", { name: "关闭", exact: true })).toBeFocused();
  await expect(page.locator(".preview-body")).toContainText("FOCUS_PREVIEW_MARKER");
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(chip).toBeFocused();
});

test("wide sidebar collapses to a rail and remembers the choice", async ({ page }) => {
  await page.goto("/#/");
  const sidebar = page.locator("#sidebar");
  const collapse = page.getByRole("button", { name: "收起侧栏", exact: true });
  await expect(collapse).toBeVisible();
  const wide = await sidebar.evaluate((el) => el.getBoundingClientRect().width);
  expect(wide).toBeGreaterThan(200);

  await collapse.click();
  await expect(page.getByRole("button", { name: "展开侧栏", exact: true })).toBeVisible();
  await expect(sidebar).toHaveClass(/rail/);
  await expect.poll(async () => Math.round(await sidebar.evaluate((el) => el.getBoundingClientRect().width))).toBeLessThan(100);
  await expect(page.locator(".sidebar .brand-text")).toBeHidden();

  await page.getByRole("button", { name: /账户菜单/ }).click();
  const menu = page.locator(".account-pop");
  await expect(menu).toBeVisible();
  expect((await menu.boundingBox())!.width).toBeGreaterThan(180);
  await page.keyboard.press("Escape");

  await page.reload();
  await expect(sidebar).toHaveClass(/rail/);

  // a rail preference must not shrink the narrow-screen drawer
  await page.setViewportSize({ width: 820, height: 900 });
  await page.reload();
  await page.getByRole("button", { name: "打开导航" }).first().click();
  await expect(page.locator(".sidebar .brand-text")).toBeVisible();
  await expect(page.locator(".sidebar .nav-text").first()).toBeVisible();

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  const expand = page.getByRole("button", { name: "展开侧栏", exact: true });
  await expand.click();
  await expect(sidebar).not.toHaveClass(/rail/);
  await expect.poll(async () => Math.round(await sidebar.evaluate((el) => el.getBoundingClientRect().width))).toBeGreaterThan(200);
});

test("session dots appear only for working, unseen completion and failure", async ({ page }) => {
  const now = Date.now() / 1000;
  const session = (id: string, title: string, status: string, run: string | null) => ({
    id, project_id: "p1", title, model_ref: "fake", settings: { preset: "workspace" }, leaf_id: null,
    updated_at: now, archived_at: null, last_run: run ? { id: run, status, ended_at: now } : null,
  });
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({ json: [{
      id: "p1", name: "状态测试", root_path: "/tmp/status-test", trusted: true, scratch: false, no_index: false,
      sessions: [session("s-running", "正在运行", "running", "r-running"),
        session("s-done", "完成未读", "ok", "r-done"),
        session("s-viewed", "完成已读", "ok", "r-viewed"),
        session("s-error", "运行出错", "error", "r-error"),
        session("s-cancelled", "已取消", "cancelled", "r-cancelled")],
    }] });
  });
  await page.goto("/#/");
  await page.addInitScript(() => {
    if (sessionStorage.getItem("nwb.test-baseline")) return;
    sessionStorage.setItem("nwb.test-baseline", "1");
    localStorage.removeItem("nwb.viewed_runs");
  });
  await page.reload();
  // Existing history is a baseline; only runs completed after this point start unseen.
  await expect(page.locator(".session.done .dot")).toHaveCount(0);
  await expect(page.locator(".session .dot")).toHaveCount(2);
  await page.evaluate(() => {
    const viewed = JSON.parse(localStorage.getItem("nwb.viewed_runs")!);
    delete viewed["s-done"];
    localStorage.setItem("nwb.viewed_runs", JSON.stringify(viewed));
  });
  await page.reload();
  await expect(page.locator(".session .dot")).toHaveCount(3);
  await expect(page.locator(".session.running .dot")).toHaveAttribute("aria-label", "正在工作");
  await expect(page.locator(".session.done .dot")).toHaveAttribute("aria-label", "已完成，未查看");
  await expect(page.locator(".session.error .dot")).toHaveAttribute("aria-label", "运行出错");
  await expect(page.locator(".session").filter({ hasText: "完成已读" }).locator(".dot")).toHaveCount(0);
  await expect(page.locator(".session").filter({ hasText: "已取消" }).locator(".dot")).toHaveCount(0);
});

test("new chat options are labelled and easy to scan", async ({ page }) => {
  await page.goto("/#/");
  await expect(page.getByLabel("工作项目", { exact: true })).toBeVisible();
  await expect(page.getByLabel("模型", { exact: true })).toBeVisible();
  await expect(page.locator(".composer-options .composer-pill")).toHaveCount(2);
  const ambient = page.locator(".home-ambient");
  await expect(ambient).toHaveAttribute("aria-hidden", "true");
  await expect(ambient).toHaveCSS("pointer-events", "none");
  const stage = await page.locator(".home-stage").boundingBox();
  const canvas = await ambient.boundingBox();
  expect(canvas?.width).toBeCloseTo(stage?.width ?? 0, 0);
  expect(canvas?.height).toBeCloseTo(stage?.height ?? 0, 0);
});

test("custom select supports keyboard choice and dismisses from outside", async ({ page }) => {
  await page.goto("/#/");
  const model = page.getByRole("combobox", { name: "模型" });
  const menu = page.getByRole("listbox", { name: "模型" });
  await model.press("ArrowDown");
  await expect(menu).toBeVisible();
  await expect(model).toHaveAttribute("aria-expanded", "true");
  expect(await menu.getByRole("option").count()).toBeGreaterThanOrEqual(2);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect(model).toHaveAttribute("aria-expanded", "false");
  await expect(model).toContainText("deepseek-v4-pro");
  await model.click();
  await expect(menu).toBeVisible();
  await page.getByRole("heading", { name: "今天想完成什么？" }).click();
  await expect(menu).toHaveCount(0);
  await model.click();
  await expect(menu).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(model).toBeFocused();
});

test("drawer choice survives reload and an explicit close", async ({ page }) => {
  await session(page);
  const library = page.getByRole("button", { name: "资料", exact: true });
  const drawer = page.getByRole("dialog", { name: "资料" });
  await library.click();
  await expect(drawer).toBeVisible();
  await page.reload();
  await expect(drawer).toBeVisible();
  await page.getByRole("button", { name: "关闭面板" }).click();
  await expect(drawer).toHaveCount(0);
  await page.reload();
  await expect(drawer).toHaveCount(0);
});

test("markdown code copies and long tool output expands", async ({ page }) => {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const created = await (await page.request.post("/api/sessions", { data: { project_id } })).json();
  const base = await (await page.request.get(`/api/sessions/${created.id}`)).json();
  const now = Date.now() / 1000;
  const long = Array.from({ length: 45 }, (_, i) => `line ${i + 1}`).join("\n");
  const snap = {
    session: { ...base.session, title: "浏览器内代码与长输出" },
    seq: 10, leaf_id: "e4", live: null, context: null,
    entries: [
      { id: "sys", parent_id: null, seq: 1, ts: now - 60, type: "system_prompt", data: { text: "test" } },
      { id: "e1", parent_id: "sys", seq: 2, ts: now - 30, type: "user",
        data: { content: [{ type: "text", text: "给我一个例子" }], attachments: [] } },
      { id: "e2", parent_id: "e1", seq: 3, ts: now - 25, type: "assistant",
        data: { stop: "tool_use", content: [
          { type: "text", text: "```ts\nconst answer: number = 42;\n```" },
          { type: "tool_call", id: "call1", name: "read", args: { path: "build.log" } },
        ] } },
      { id: "e3", parent_id: "e2", seq: 4, ts: now - 24, type: "tool_started",
        data: { call_id: "call1", name: "read", args: { path: "build.log" } } },
      { id: "e4", parent_id: "e3", seq: 5, ts: now - 23, type: "tool_result",
        data: { call_id: "call1", name: "read", is_error: false, content: [{ type: "text", text: long }], meta: {} } },
    ],
  };
  await page.route(`**/api/sessions/${created.id}`, (route) => route.fulfill({ json: snap }));
  await page.route(`**/api/sessions/${created.id}/events*`, (route) => route.fulfill({ status: 200,
    contentType: "text/event-stream", body:
      'data: {"type":"live","live":null}\n\ndata: {"type":"leaf","leaf_id":"e4"}\n\n' }));

  await page.goto(`/#/s/${created.id}`);
  const code = page.locator(".md .code-wrap");
  await expect(code).toContainText("const answer: number = 42;");
  await code.hover();
  await page.getByRole("button", { name: "复制代码" }).click();
  await expect(page.getByRole("button", { name: "已复制代码" })).toBeVisible();

  const step = page.locator(".step").first();
  await step.locator(".step-row").click();
  const toggle = page.getByRole("button", { name: /展开全部 · 45 行/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(page.getByRole("button", { name: "收起输出" })).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".tool-output pre")).toContainText("line 45");
});

test("session status summarises tokens, cache and working state", async ({ page }) => {
  const sid = await session(page);
  await expect(page.locator(".session-status")).toBeVisible();
  await expect(page.locator(".session-status")).toContainText("缓存 —");
  await send(page, "E2E状态：简单回复");
  await expect(page.locator(".msg.assistant:not(.streaming)")).toContainText("本地模拟回复");
  await expect(page.locator(".session-status")).toContainText("30 tokens");
  await expect(page.locator(".session-status")).toContainText("1 轮 · 0 步");
  await expect(page.locator(".session-status")).toContainText("缓存 —");
  const detail = await (await page.request.get(`/api/sessions/${sid}`)).json();
  const reply = detail.entries.find((e: any) => e.type === "assistant");
  expect(reply.data.seconds).toBeGreaterThan(0);
});

test("running status ticks and the trace shows a live step", async ({ page }) => {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const created = await (await page.request.post("/api/sessions", { data: { project_id } })).json();
  const base = await (await page.request.get(`/api/sessions/${created.id}`)).json();
  const now = Date.now() / 1000;
  const live = { run_id: "live-run", text: "", thinking: "", outputs: {}, steer: [], queue: [], approvals: [], questions: [] };
  await page.route(`**/api/sessions/${created.id}`, (route) => route.fulfill({ json: {
    session: { ...base.session, title: "运行中" }, seq: 4, leaf_id: "u1", live,
    context: { tokens: 1200, budget: 100000 },
    entries: [
      { id: "sys", parent_id: null, seq: 1, ts: now - 20, type: "system_prompt", data: { text: "test" } },
      { id: "u1", parent_id: "sys", seq: 2, ts: now - 4, type: "user",
        data: { content: [{ type: "text", text: "请开始一次长任务" }], attachments: [] } },
    ],
  } }));
  await page.route(`**/api/sessions/${created.id}/events*`, (route) => route.fulfill({ status: 200,
    contentType: "text/event-stream", body:
      `data: {"type":"live","live":${JSON.stringify(live)}}\n\ndata: {"type":"leaf","leaf_id":"u1"}\n\n` }));
  await page.goto(`/#/s/${created.id}`);
  const status = page.locator(".session-status");
  await expect(status).toContainText("本轮 0 步");
  await expect(status).toContainText(/本轮 0 步 · \d+(?:\.\d+)?s/);
  await expect(status).toContainText("0 tokens");
  await expect(status.locator(".status-live")).toHaveCount(0);

  await page.getByRole("button", { name: "会话树", exact: true }).click();
  await page.getByRole("tab", { name: "轨迹" }).click();
  await expect(page.locator(".trace-step.live")).toContainText("正在生成");
  await expect(page.locator(".trace-step.live")).toContainText("进行中");
});

test("trace shows model and tool timing, follows the run and jumps back to chat", async ({ page }) => {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const created = await (await page.request.post("/api/sessions", { data: { project_id } })).json();
  const base = await (await page.request.get(`/api/sessions/${created.id}`)).json();
  const now = Date.now() / 1000;
  const snap = {
    session: { ...base.session, title: "轨迹测试" },
    seq: 20, leaf_id: "a3", live: null, context: { tokens: 8000, budget: 100000 },
    entries: [
      { id: "sys", parent_id: null, seq: 1, ts: now - 120, type: "system_prompt", data: { text: "test" } },
      { id: "u1", parent_id: "sys", seq: 2, ts: now - 100, type: "user",
        data: { content: [{ type: "text", text: "第一轮：查看文件" }], attachments: [] } },
      { id: "a1", parent_id: "u1", seq: 3, ts: now - 98, type: "assistant",
        data: { stop: "tool_use", seconds: 2, usage: { input: 100, output: 20, cache_read: 80 }, content: [
          { type: "text", text: "我先查看文件。" },
          { type: "tool_call", id: "c1", name: "read", args: { path: "notes.txt" } },
        ] } },
      { id: "t1", parent_id: "a1", seq: 4, ts: now - 97, type: "tool_started",
        data: { call_id: "c1", name: "read" } },
      { id: "r1", parent_id: "t1", seq: 5, ts: now - 96, type: "tool_result",
        data: { call_id: "c1", name: "read", is_error: false, content: [{ type: "text", text: "NOTES" }], meta: {} } },
      { id: "a2", parent_id: "r1", seq: 6, ts: now - 94, type: "assistant",
        data: { stop: "end", seconds: 1, usage: { input: 50, output: 10, cache_read: 40 },
          content: [{ type: "text", text: "文件里是 NOTES。" }] } },
      { id: "u2", parent_id: "a2", seq: 7, ts: now - 80, type: "user",
        data: { content: [{ type: "text", text: "第二轮：" }], attachments: [] } },
      { id: "a3", parent_id: "u2", seq: 8, ts: now - 78, type: "assistant",
        data: { stop: "end", seconds: 1.5, usage: { input: 30, output: 5 },
          content: [{ type: "text", text: "第二轮回复。" }] } },
    ],
  };
  await page.route(`**/api/sessions/${created.id}`, (route) => route.fulfill({ json: snap }));
  await page.route(`**/api/sessions/${created.id}/events*`, (route) => route.fulfill({ status: 200,
    contentType: "text/event-stream", body:
      'data: {"type":"live","live":null}\n\ndata: {"type":"leaf","leaf_id":"a3"}\n\n' }));

  await page.goto(`/#/s/${created.id}`);
  await expect(page.locator(".session-status")).toContainText("8%");
  await expect(page.locator(".session-status")).toContainText("215 tokens");
  await expect(page.locator(".session-status")).toContainText("缓存 80%");
  await expect(page.locator(".session-status")).toContainText("2 轮 · 1 步");

  await page.getByRole("button", { name: "会话树", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "会话树" });
  await expect(dialog).toBeVisible();
  const traceTab = dialog.getByRole("tab", { name: "轨迹" });
  const chatTab = dialog.getByRole("tab", { name: "对话" });
  await expect(page.getByRole("button", { name: "关闭面板" })).toBeFocused();
  await chatTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(traceTab).toBeFocused();
  await expect(traceTab).toHaveAttribute("aria-selected", "true");
  await expect(dialog.locator(".trace-head")).toContainText("2 轮 · 1 步 · 215 tokens");
  await expect(dialog.getByRole("button", { name: "自动跟随" })).toHaveAttribute("aria-pressed", "true");

  const first = dialog.locator(".trace-group").first();
  await expect(first).toContainText("第 1 轮");
  await first.locator(".trace-summary").click();
  await expect(first.locator(".trace-step.model")).toHaveCount(2);
  await expect(first.locator(".trace-step.tool")).toHaveCount(1);
  await expect(first).toContainText("10 tok/s");

  await first.getByRole("button", { name: "定位" }).click();
  await expect(page.locator(".chat-item.jump-hit")).toHaveCount(1);
  await expect(page.locator(".chat-item.jump-hit")).toContainText("第一轮：查看文件");

  await dialog.getByRole("button", { name: "自动跟随" }).click();
  await expect(dialog.getByRole("button", { name: "自动跟随" })).toHaveAttribute("aria-pressed", "false");
  await page.reload();
  await expect(page.getByRole("dialog", { name: "会话树" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "轨迹" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", { name: "自动跟随" })).toHaveAttribute("aria-pressed", "false");
});

test("long conversation mounts a window and reveals earlier turns", async ({ page }) => {
  const { project_id } = await (await page.request.get("/__test__/project")).json();
  const created = await (await page.request.post("/api/sessions", { data: { project_id } })).json();
  const base = await (await page.request.get(`/api/sessions/${created.id}`)).json();
  const now = Date.now() / 1000;
  const entries = [{ id: "sys", parent_id: null, seq: 1, ts: now - 100, type: "system_prompt", data: { text: "test" } }];
  for (let i = 1; i <= 70; i++) {
    entries.push({ id: `u${i}`, parent_id: i === 1 ? "sys" : `u${i - 1}`, seq: i + 1, ts: now - 100 + i,
      type: "user", data: { content: [{ type: "text", text: `第 ${i} 条消息` }], attachments: [] } });
  }
  await page.route(`**/api/sessions/${created.id}`, (route) => route.fulfill({ json: {
    session: { ...base.session, title: "长会话窗口" }, seq: 100, leaf_id: "u70", live: null, context: null, entries,
  } }));
  await page.route(`**/api/sessions/${created.id}/events*`, (route) => route.fulfill({ status: 200,
    contentType: "text/event-stream", body:
      'data: {"type":"live","live":null}\n\ndata: {"type":"leaf","leaf_id":"u70"}\n\n' }));

  await page.goto(`/#/s/${created.id}`);
  await expect(page.locator(".msg.user")).toHaveCount(40);
  await expect(page.getByRole("button", { name: /显示更早的 30 条/ })).toBeVisible();
  await page.getByRole("button", { name: /显示更早的 30 条/ }).click();
  await expect(page.locator(".msg.user")).toHaveCount(70);
  await expect(page.locator(".load-earlier")).toHaveCount(0);
  await expect(page.locator(".msg.user").first()).toContainText("第 1 条消息");

  await page.getByRole("button", { name: "会话树", exact: true }).click();
  await page.getByRole("tab", { name: "轨迹" }).click();
  await page.getByRole("button", { name: /显示更早的 30 轮/ }).click();
  await page.locator(".trace-group").first().getByRole("button", { name: "定位" }).click();
  await expect(page.locator(".msg.user").first()).toContainText("第 1 条消息");
  await expect(page.locator(".chat-item.jump-hit")).toHaveCount(1);
});
