const $ = (id) => document.getElementById(id);
const pageInfo = {
  overview: ["运行总览", "掌握服务状态与数据生产进度"],
  data: ["数据管理", "管理数据生产机上的终端与采集任务"],
  sources: ["数据源盘点", "核对实际存储、来源与已登记的采集能力"],
  research: ["研报知识", "逐页处理、审核并发布产业链与金工内容"],
  tasks: ["任务队列", "追踪执行状态、检查结果与失败原因"],
  logs: ["运行日志", "查看后台输出与定位运行问题"],
  diagnostics: ["环境诊断", "供开发与测试人员核对运行环境"],
  settings: ["设置", "管理台外观与本地服务运行策略"],
};
const statuses = { QUEUED: "排队中", RUNNING: "运行中", PASS: "完成", PARTIAL_FAILURE: "部分完成", FAILED: "失败", CANCELLED: "已取消", NO_CHANGE: "无需增量" };
const terminalReasons = {
  LAUNCHED: "终端已启动；盘后任务将检查登录与下载窗口", ALREADY_RUNNING: "终端已在运行；盘后任务按检查点去重",
  UI_DONE_UNVERIFIED: "界面下载已完成，仍需核验本地文件", COMPLETE_WITH_SKIPS: "部分批次按原文件端点跳过，其余界面下载完成",
  NO_GUI_RAW_RECENT: "原文件分组多数端点日期已覆盖，逐标的入库仍待核", WAITING_FOR_LOGIN: "已保存登录尚未就绪，稍后自动重试",
  WAITING_FOR_LOGIN_OR_MAIN_WINDOW: "登录主窗口尚未就绪，稍后自动重试", CLIENT_NOT_RUNNING: "终端尚未启动",
  OPEN_DOWNLOAD_DIALOG_MANUALLY: "当前终端菜单未识别，请检查软件版本与窗口状态",
  EXISTING_DOWNLOAD_BUSY: "终端已有下载任务，当前任务未接管它", DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN: "等待超时，终端可能仍在下载，请先核对实际结果",
  EXTENDED_MARKET_HOST_DISCONNECTED: "扩展市场行情主站尚未连接，已按空闲状态尝试重连；请核对终端连接",
  EXTENDED_HOST_RECONNECT_UNVERIFIED: "点击扩展市场选项卡后无法确认重连，保留窗口供检查",
  FUTURES_MARKET_HOST_DISCONNECTED: "期货市场行情主站尚未连接，当前下载未完成",
  FUTURES_HOST_RECONNECT_NOT_IDLE: "期货通当前不在空闲状态，暂缓重连",
  FUTURES_HOST_TABS_UNVERIFIED: "期货通下载选项卡布局待核验，保留窗口供检查",
  FUTURES_HOST_RECONNECT_UNVERIFIED: "点击期货市场选项卡后无法确认重连，保留窗口供检查",
  TERMINAL_DOWNLOAD_CANCELLED: "终端显示下载被取消；本次未认定为下载完成",
  MARKET_CONNECTION_DROPPED: "终端显示行情连接断开；本次未认定为下载完成",
  MARKET_HOST_INITIALIZING: "主站正在初始化当日数据；终端将自动重连，稍后再试",
  PREVIOUS_RUN_NEEDS_RECONCILIATION: "上次下载结果未知，请先核对终端与文件", RUNNING: "正在执行，可在通达信日志查看进度",
};
let state = null;
let page = "overview";
let csrf = "";
let taskItems = [];
let selectedTaskId = "";
let logLines = [];
let logPaused = false;
let polling = false;
let taskLoading = false;
let taskLastReadAt = 0;
let taskFastUntil = 0;
let taskRenderFingerprint = "";

function taskDisplayState(item) {
  if (!item) return null;
  return [item.operation_id, item.kind, item.status, item.created_at, item.started_at,
    item.completed_at, item.detail, item.phase, item.completed_units, item.total_units,
    item.checkpoint, item.cancel_requested, item.cancel_supported];
}
let settingsSaving = false;
let closing = false;
let noticeTimer;
let tdxLoading = false;
let tdxStatus = null;
let tdxLastReadAt = 0;
let tdxLoadSerial = 0;
let manualGapLoading = false;
let manualGapLoaded = false;
let manualGapCatalog = [];
let sessionReady;
const initialPageHash = location.hash.slice(1);
let pendingBootstrap = new URLSearchParams(initialPageHash).get("ml-bootstrap");
let selectedResearchPage = null;
let researchLastReadAt = 0;

function sessionUnavailable(message) {
  const error = new Error(message);
  error.loginRequired = true;
  return error;
}

function initializeSession() {
  if (sessionReady) return sessionReady;
  sessionReady = (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
      const bootstrap = pendingBootstrap;
      if (bootstrap !== null) {
        history.replaceState(null, "", location.pathname + location.search);
        await fetch("/api/local-session/bootstrap", {
          method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bootstrap }),
        });
        pendingBootstrap = null;
      }
      let response = await fetch("/api/local-session", {
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
      });
      if (response.status === 401) {
        response = await fetch("/api/local-session/open", {
          method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
          headers: { "Content-Type": "application/json" }, body: "{}",
        });
      }
      if (!response.ok) throw sessionUnavailable(`本机会话暂不可用（${response.status}），正在重试。`);
      const value = await response.json();
      if (value.audience !== "admin" || !value.csrfToken) throw new Error("本机会话权限域无效，请刷新页面。");
      csrf = value.csrfToken;
      tdxLoadSerial += 1;
      tdxLoading = false;
      tdxStatus = null;
      tdxLastReadAt = 0;
      $("tdx-status-note").textContent = "正在读取日期核验结果…";
      showPage(page);
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("本机会话请求超时，管理台会继续重试");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  })().catch((error) => {
    sessionReady = undefined;
    csrf = "";
    throw error;
  });
  return sessionReady;
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").className = `notice${error ? " error" : ""}`;
  $("notice").setAttribute("role", error ? "alert" : "status");
  clearTimeout(noticeTimer);
  if (!error) noticeTimer = setTimeout(() => $("notice").classList.add("hidden"), 6000);
}

async function api(path, method = "GET", value, timeoutMs = 30000) {
  await initializeSession();
  const requestSession = sessionReady;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      method, cache: "no-store", credentials: "same-origin", signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(method !== "GET" ? { "X-MarketListener-Console-Token": csrf } : {}) },
      body: value === undefined ? undefined : JSON.stringify(value),
    });
    const result = await response.json();
    if (response.status === 401 && sessionReady === requestSession) {
      sessionReady = undefined;
      csrf = "";
      throw sessionUnavailable("本机会话已过期，正在自动恢复。");
    }
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求失败（${response.status}）`);
    return result;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("管理接口响应超时；正在重新核对服务状态");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function applyTheme(theme) {
  const effective = theme === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : theme;
  document.documentElement.dataset.theme = effective;
  document.querySelectorAll("[data-theme-choice]").forEach((button) => button.classList.toggle("selected", button.dataset.themeChoice === theme));
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const value = Math.max(0, seconds);
  return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map((n) => String(n).padStart(2, "0")).join(":");
}

function beijingTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function fullBeijingTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function fileDate(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : "—";
}

function dateRange(start, end) {
  if (!start && !end) return "尚未核验";
  return `${fileDate(start)} → ${fileDate(end)}`;
}

function count(value) {
  return Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
}

function textElement(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderTdxStatus(value) {
  tdxStatus = value;
  const hasRoots = Array.isArray(value.roots) && value.roots.length > 0;
  $("tdx-status-loading").classList.toggle("hidden", hasRoots || !value.scanning);
  $("tdx-status-loading").setAttribute("aria-busy", String(Boolean(value.scanning)));
  $("tdx-status-note").textContent = value.scanning
    ? "正在核对本地文件，已读结果保持显示"
    : `${value.stale ? "显示上次核验结果 · " : ""}${value.checkedAt ? `核验时间 ${fullBeijingTime(value.checkedAt)}` : "尚无文件核验记录"}`;
  const startup = value.startupImport || {};
  const startupLabel = startup.enabled === true ? "启动增量导入已开启" : startup.enabled === false ? "启动增量导入未开启" : "启动增量导入状态尚未核验";
  $("tdx-auto-import").textContent = `${startupLabel}${startup.status ? ` · ${statuses[startup.status] || startup.status}` : ""}${startup.detail ? ` · ${startup.detail}` : ""}`;
  const target = $("tdx-status-roots");
  const scrollPositions = new Map([...target.querySelectorAll("[data-terminal-status]")].map((card) => {
    const wrap = card.querySelector(".tdx-table-wrap");
    return [card.dataset.terminalStatus, { top: wrap?.scrollTop || 0, left: wrap?.scrollLeft || 0 }];
  }));
  target.replaceChildren();
  for (const root of value.roots || []) {
    const terminalName = root.name || ({ financial: "通达信金融终端", futures: "通达信期货通" }[root.terminal]) || "本地终端";
    const card = textElement("section", "", "tdx-terminal");
    card.dataset.terminalStatus = root.terminal;
    const heading = textElement("div", "", "tdx-terminal-heading");
    heading.append(textElement("h3", terminalName));
    const job = root.lastImport || {};
    const status = job.status || "UNVERIFIED";
    heading.append(textElement("span", job.status ? `入库${statuses[status] || status}` : "尚无入库记录", `badge ${status}`));
    card.append(heading, textElement("p", root.path || "未配置终端目录", "tdx-root-path"));
    const metadata = textElement("dl", "", "tdx-terminal-metadata");
    const recordedDownload = root.lastDownload?.finishedAt;
    const schedule = root.downloadSchedule || {};
    const scheduleText = schedule.currentSlotComplete
      ? `本时段已完成 · 下次最早 ${fullBeijingTime(schedule.nextCheckSlot)} 后`
      : schedule.needsReconciliation
        ? `下载结果未确认，需先核对终端与原始文件${schedule.skippedBatches?.length ? "；" + schedule.skippedBatches.join("、") + "的原文件分组多数端点已覆盖" : ""}`
        : schedule.skipReason && root.terminal === "futures"
          ? `周末无新国内会话，原文件分组多数端点日期已覆盖；逐合约入库待核 · 本时段不启动期货通`
        : schedule.skipReason && root.terminal === "financial"
          ? `沪深京A/B股原文件分组多数端点日期已覆盖；扩展市场仍独立检查`
        : schedule.currentSlot
          ? `本时段未完成 · ${fullBeijingTime(schedule.currentSlot)} 后按失败退避自动检查`
          : "尚无自动下载时段记录";
    const lastImportTime = job.completedAt || job.startedAt;
    for (const [label, content] of [
      ["上次记录下载", recordedDownload ? `${fullBeijingTime(recordedDownload)}${root.lastDownload.status ? ` · ${terminalReasons[root.lastDownload.status] || statuses[root.lastDownload.status] || root.lastDownload.status}` : ""}` : "尚无界面完成时间"],
      ["自动下载时段", scheduleText],
      ["原文件最近改动", fullBeijingTime(root.lastFileModifiedAt)],
      ["最近入库任务", lastImportTime ? fullBeijingTime(lastImportTime) : "尚无记录"],
    ]) metadata.append(textElement("dt", label), textElement("dd", content));
    card.append(metadata);
    if (root.available === false) card.append(textElement("p", "未找到终端文件目录，保留已记录的导入结果。", "tdx-warning"));
    if (job.status) {
      const result = textElement("div", "", "tdx-import-result");
      result.append(textElement("span", `本次提交 ${count(job.rows)} 根 K 线${Number.isFinite(job.importedFiles) ? ` · ${count(job.importedFiles)} 个文件` : ""}`));
      if (Number.isFinite(job.pendingFiles)) {
        const label = job.status === "RUNNING" ? "待处理" : "当前文件版本待入库";
        const rejected = job.rejectedFiles ?? job.report?.["拒绝文件"];
        const quarantined = job.quarantinedFiles ?? job.report?.["隔离文件"];
        const evidence = Number.isFinite(rejected) && rejected > 0
          ? ` · 本次质量拒绝 ${count(rejected)} 个${Number.isFinite(quarantined) ? `（隔离总数 ${count(quarantined)}，含拒绝）` : ""}`
          : "";
        result.append(textElement("span", `${label} ${count(job.pendingFiles)} 个${evidence}`, job.pendingFiles > 0 ? "tdx-warning" : "muted"));
      }
      if (job.detail) {
        const detail = textElement("details", "", "tdx-import-detail");
        detail.append(textElement("summary", "查看导入结果"), textElement("p", job.detail)); result.append(detail);
      }
      card.append(result);
    }
    const wrap = textElement("div", "", "table-wrap tdx-table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", `${terminalName}本地原件与入库日期`);
    const table = textElement("table", "", "tdx-table");
    const thead = document.createElement("thead"); const header = document.createElement("tr");
    for (const label of ["数据 / 来源", "本地原件起止", "常见截至日", "已入库起止", "文件数"]) header.append(textElement("th", label));
    thead.append(header); const tbody = document.createElement("tbody");
    for (const group of root.groups || []) {
      const row = document.createElement("tr");
      const name = textElement("td", "");
      name.append(textElement("strong", `${group.label || group.key || "未分类"} · ${{ "1d": "日线", "5m": "5分钟线" }[group.period] || group.period || "未知周期"}`), textElement("small", group.source || group.sourceCode || "来源未核验"));
      const raw = textElement("td", dateRange(group.earliestDate, group.latestDate), "tdx-date-range");
      const common = textElement("td", fileDate(group.commonLatestDate));
      if (group.commonLatestDate) common.append(textElement("small", `${count(group.commonLatestCount)} / ${count(group.fileCount)} 个文件`));
      const imported = textElement("td", dateRange(group.importedEarliestDate, group.importedLatestDate), "tdx-date-range");
      const files = textElement("td", count(group.fileCount));
      if (group.invalidFileCount > 0) files.append(textElement("small", `${count(group.invalidFileCount)} 个无法解析`, "tdx-warning"));
      row.append(name, raw, common, imported, files); tbody.append(row);
    }
    if (!root.groups?.length) {
      const row = document.createElement("tr"); const empty = textElement("td", value.scanning ? "" : "未找到可核验的日线或5分钟文件", value.scanning ? "tdx-skeleton-row" : "empty");
      empty.colSpan = 5; row.append(empty); tbody.append(row);
    }
    table.append(thead, tbody); wrap.append(table); card.append(wrap); target.append(card);
    const position = scrollPositions.get(root.terminal);
    if (position) { wrap.scrollTop = position.top; wrap.scrollLeft = position.left; }
  }
  const financial = value.financialCoverage;
  if (financial?.sourcePolicyVersion) {
    const card = textElement("section", "", "tdx-terminal");
    card.append(textElement("h3", "期货行情与官网补充字段"));
    const fields = textElement("dl", "", "tdx-terminal-metadata");
    for (const [label, content] of [
      ["通达信月份日线", fileDate(financial.tdxLatestDay)],
      ["官网成交额", fileDate(financial.officialTurnoverLatestDay)],
      ["官网投机保证金", fileDate(financial.officialMarginLatestDay)],
      ["派生指标日期", fileDate(financial.derivedLatestDay)],
      ["有效市值 / 合约", `${count(financial.notionalCount)} / ${count(financial.contractCount)}`],
      ["有效成交额 / 合约", `${count(financial.turnoverCount)} / ${count(financial.contractCount)}`],
      ["有效沉淀资金 / 合约", `${count(financial.capitalCount)} / ${count(financial.contractCount)}`],
      ["量价仓待核验", `${count(financial.mismatchCount)} 个合约`],
      ["缺少通达信月份日线", `${count(Math.max(0, (financial.contractCount || 0) - (financial.tdxValidCount || 0)))} 个合约`],
    ]) fields.append(textElement("dt", label), textElement("dd", content));
    card.append(fields);
    const dce = value.dceApi?.accounts;
    if (dce && Object.keys(dce).length) {
      const labels = { PASS: "正常", RUNNING: "运行中", COOLDOWN_WAIT: "冷却中", NETWORK_RETRY_WAIT: "网络重试", FAILED: "失败" };
      card.append(textElement("p", `大商所授权API：${Object.entries(dce).map(([label, account]) =>
        `账号${label} ${labels[account.status] || account.status}（每${account.windowSeconds || 1}秒最多${account.requestsPerWindow || 3}次）`).join(" · ")}`, "tdx-date-note"));
    }
    card.append(textElement("p", `最近核验 ${fullBeijingTime(financial.checkedAt)} · 期货数据页的历史统计仍使用官网日行情。`, "tdx-date-note"));
    target.append(card);
  }
}

async function loadTdxStatus(force = false) {
  if (tdxLoading || closing) return;
  const serial = ++tdxLoadSerial;
  tdxLoading = true;
  $("refresh-tdx-status").disabled = true;
  tdxLastReadAt = Date.now();
  try {
    const value = await api(`/api/admin/tdx-status${force ? "?refresh=1" : ""}`);
    if (serial === tdxLoadSerial) renderTdxStatus(value);
  }
  catch (error) {
    if (serial === tdxLoadSerial) {
      $("tdx-status-loading").classList.add("hidden");
      $("tdx-status-note").textContent = `日期核验读取失败：${error.message}${tdxStatus ? "；保留上次结果" : ""}`;
    }
  }
  finally {
    if (serial === tdxLoadSerial) {
      tdxLoading = false;
      $("refresh-tdx-status").disabled = false;
    }
  }
}

function renderState(next) {
  state = next;
  csrf = next.csrfToken;
  const running = next.ready;
  const unresponsive = !next.action && !running && next.owned;
  const failed = !next.action && !running && Boolean(next.error);
  const phaseLabels = { STOPPING: "正在停止旧进程", STARTING: "正在启动进程", WAITING_READY: "等待服务就绪", CLEANING_UP: "正在清理未就绪的进程" };
  const startupLabels = {
    APP_CONSTRUCTION: "创建服务", RESTORE_RECOVERY: "检查上次恢复", CATALOG_MIGRATION: "准备数据目录",
    QUERY_INDEX_SETUP: "准备行情索引", READMODEL_CHECK: "核对行情快照", BACKGROUND_WATCHER: "启动发布监测",
    BACKUP_QUEUE: "安排备份", UPDATE_QUEUE: "安排更新", READY: "启动完成", FAILED: "启动失败",
  };
  const elapsed = Number.isFinite(next.actionElapsedSeconds) ? ` · 已等待 ${Math.max(0, Math.floor(next.actionElapsedSeconds))} 秒` : "";
  const phase = next.action === "start" || next.action === "restart"
    ? startupLabels[next.startupStage?.phase] || phaseLabels[next.actionPhase] || "正在处理服务请求"
    : phaseLabels[next.actionPhase] || "正在处理服务请求";
  $("service-title").textContent = next.action ? ({ start: "正在启动客户服务", stop: "正在停止客户服务", restart: "正在重启客户服务" }[next.action]) : failed ? "客户服务操作未完成" : unresponsive ? "客户服务进程暂未响应" : running ? "客户服务已就绪" : "客户服务已停止";
  $("service-description").textContent = next.action ? `${phase}${elapsed}。管理台会自动刷新状态，请勿重复提交。` : failed ? `${next.error}。请查看启动日志；当前进程状态会继续自动检查。` : unresponsive ? "进程仍在运行，但健康检查暂未响应；可能正在加载或处理更新。请查看日志并等待状态恢复，持续无响应时可重启服务。" : running ? next.owned ? "服务由当前管理台维护，客户产品已可访问。" : "已发现其他启动程序管理的客户服务；停止与重启需在原程序中操作。" : "启动服务后，客户可使用行情、策略与研究功能。管理台保持在线。";
  $("service-badge").textContent = next.action ? "处理中" : failed ? "操作失败" : unresponsive ? "未响应" : running ? "在线" : "离线";
  $("service-badge").className = `badge ${failed ? "FAILED" : unresponsive ? "unresponsive" : next.state}`;
  $("service-card").classList.toggle("has-error", failed);
  $("service-card").setAttribute("aria-busy", String(Boolean(next.action)));
  $("service-log-shortcut").classList.toggle("hidden", !next.action && !failed && !unresponsive);
  $("service-uptime").textContent = duration(next.uptimeSeconds);
  $("console-uptime").textContent = duration(next.consoleUptimeSeconds);
  $("service-pid").textContent = next.pid || "—";
  const cpu = Number.isFinite(next.resources?.cpuPercent) ? `${next.resources.cpuPercent}%` : "—";
  const memory = Number.isFinite(next.resources?.memoryBytes) ? `${(next.resources.memoryBytes / 1024 ** 2).toFixed(1)} MiB` : "—";
  $("service-resources").textContent = `客户服务进程：CPU ${cpu} · 内存 ${memory}（不包含采集子进程）`;
  $("service-owner").textContent = next.owned ? "由本管理台启动" : running ? "现有外部服务" : "尚未启动客户服务";
  $("priority-mode").textContent = next.settings.background_priority ? "后台低优先级" : "标准优先级";
  $("client-address").textContent = next.backendUrl;
  $("open-client").href = next.backendUrl;
  $("open-client").classList.toggle("disabled", !running);
  $("open-client").setAttribute("aria-disabled", String(!running));
  document.querySelectorAll("[data-service]").forEach((button) => {
    button.disabled = Boolean(next.action) || (button.dataset.service === "start" ? running || next.owned : !next.owned);
    if (button.dataset.service === "start") button.textContent = failed && !next.owned ? "重新启动客户服务" : "启动客户服务";
  });
  document.querySelectorAll("[data-operation]").forEach((button) => { button.disabled = !running || Boolean(next.action); });
  document.querySelectorAll("[data-terminal]").forEach((button) => {
    button.disabled = !next.startupSupported || next.terminalBusy || (button.dataset.action === "download" && !running);
  });
  for (const terminal of ["financial", "futures"]) {
    const job = next.terminalJobs[terminal];
    if (job) $("terminal-" + terminal).textContent = (terminalReasons[job.detail] || `${statuses[job.status] || job.status}：${job.detail}`) + (job.followUpError ? `；后续处理：${job.followUpError}` : "");
  }
  if (!settingsSaving) {
    $("setting-auto-start").checked = next.settings.auto_start;
    $("setting-auto-update").checked = next.settings.auto_update;
    $("setting-background").checked = next.settings.background_priority;
    $("setting-startup").checked = next.startupEnabled;
    $("setting-startup").disabled = !next.startupSupported;
    if (!next.startupSupported) $("startup-description").textContent = "当前系统请使用其服务管理器配置";
    applyTheme(next.settings.theme);
  }
  $("project-path").textContent = next.projectPath;
  $("data-path").textContent = next.dataPath;
  $("connection-note").textContent = `管理服务在线 · ${next.scope}`;
}

const taskGuidance = {
  QUEUED: "任务在队列中等待；可在操作列取消尚未开始的任务。",
  RUNNING: "任务正在执行；进度以当前阶段报告为准，页面会自动刷新。",
  PASS: "任务已结束。可核对结果与对应数据覆盖。",
  NO_CHANGE: "检查已结束，本次没有需要导入的增量。",
  PARTIAL_FAILURE: "部分步骤未完成。请核对下方结果与运行日志中的缺口。",
  FAILED: "任务未完成。请查看下方原因和运行日志，排除问题后重新提交。",
  CANCELLED: "任务已取消；已经提交的数据以任务结果为准。",
};

function showTaskDetails(item) {
  selectedTaskId = item.operation_id || "";
  const title = state?.operationLabels?.[item.kind] || item.kind;
  $("task-detail-title").textContent = title;
  const badge = $("task-detail-state");
  badge.className = `badge ${item.status || ""}`;
  badge.textContent = statuses[item.status] || item.status || "状态未知";
  $("task-detail-hint").textContent = taskGuidance[item.status] || "请核对当前结果和运行日志。";
  const lines = [
    `状态：${statuses[item.status] || item.status || "未知"}`,
    item.phase && item.phase !== item.status ? `阶段：${item.phase}` : null,
    Number.isInteger(item.total_units) && item.total_units > 0
      ? `进度：${count(item.completed_units || 0)} / ${count(item.total_units)}` : null,
    item.checkpoint ? `检查点：${item.checkpoint}` : null,
    item.detail ? `结果：${item.detail}` : null,
  ].filter(Boolean);
  $("task-detail").textContent = lines.join("\n") || "尚无任务结果";
  document.querySelectorAll("#all-tasks tr[data-task-id]").forEach((row) => {
    row.classList.toggle("is-selected", row.dataset.taskId === selectedTaskId);
  });
}

function openTask(item) {
  showPage("tasks");
  showTaskDetails(item);
}

function taskRows(target, items, actions = false) {
  const active = document.activeElement;
  const focusedId = target.contains(active) ? active.closest("tr[data-task-id]")?.dataset.taskId : "";
  const focusedClass = active?.classList.contains("task-cancel") ? "task-cancel" : "task-open";
  const scrollHost = target.closest(".table-wrap");
  const left = scrollHost?.scrollLeft || 0;
  const top = scrollHost?.scrollTop || 0;
  target.replaceChildren();
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = actions ? 4 : 3; td.className = "empty";
    td.textContent = actions && taskItems.length ? "当前筛选条件下没有任务" : "暂无任务记录";
    tr.append(td); target.append(tr); return;
  }
  for (const item of items) {
    const tr = document.createElement("tr"); tr.className = "selectable";
    tr.dataset.taskId = item.operation_id || "";
    tr.classList.toggle("is-selected", Boolean(selectedTaskId) && selectedTaskId === item.operation_id);
    const name = document.createElement("td");
    const title = state?.operationLabels?.[item.kind] || item.kind;
    const open = document.createElement("button"); open.className = "task-open";
    open.type = "button"; open.textContent = title; open.setAttribute("aria-label", `查看${title}任务详情`);
    open.addEventListener("click", (event) => { event.stopPropagation(); openTask(item); });
    name.append(open);
    const status = document.createElement("td"); const badge = document.createElement("span"); badge.className = `badge ${item.status}`; badge.textContent = statuses[item.status] || item.status; status.append(badge);
    if ((item.status === "FAILED" || item.status === "PARTIAL_FAILURE") && item.detail) {
      const reason = document.createElement("small"); reason.className = "task-reason";
      reason.textContent = String(item.detail).replace(/\s+/g, " ").slice(0, 180);
      status.append(reason);
    }
    if (item.status === "RUNNING") {
      const progress = document.createElement("small"); progress.className = "task-progress";
      const counts = Number.isInteger(item.total_units) && item.total_units > 0
        ? ` ${item.completed_units || 0}/${item.total_units}` : "";
      progress.textContent = `${item.phase && item.phase !== "RUNNING" ? item.phase : "处理中"}${counts}${item.cancel_requested ? " · 等待安全检查点取消" : ""}`;
      status.append(progress);
      if (Number.isInteger(item.total_units) && item.total_units > 0) {
        const meter = document.createElement("progress"); meter.className = "task-meter";
        meter.max = item.total_units;
        meter.value = Math.max(0, Math.min(item.completed_units || 0, item.total_units));
        meter.setAttribute("aria-label", `${title}当前阶段进度`);
        status.append(meter);
      }
    }
    const time = document.createElement("td"); time.className = "task-time"; time.textContent = beijingTime(item.completed_at || item.started_at || item.created_at);
    tr.append(name, status, time);
    tr.addEventListener("click", () => openTask(item));
    if (actions) {
      const action = document.createElement("td");
      if (item.status === "QUEUED") {
        const button = document.createElement("button"); button.className = "task-cancel"; button.textContent = "取消排队";
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          try { await api(`/api/admin/tasks/${encodeURIComponent(item.operation_id)}/cancel`, "POST"); notice("任务已取消排队"); await loadTasks(); } catch (error) { notice(error.message, true); }
        }); action.append(button);
      } else if (item.status === "RUNNING" && item.cancel_supported) {
        const button = document.createElement("button"); button.className = "task-cancel"; button.textContent = item.cancel_requested ? "取消已请求" : "请求取消";
        button.disabled = Boolean(item.cancel_requested);
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          try { await api(`/api/admin/tasks/${encodeURIComponent(item.operation_id)}/cancel`, "POST"); notice("将在下一安全检查点取消"); await loadTasks(); }
          catch (error) { notice(error.message, true); }
        }); action.append(button);
      }
      tr.append(action);
    }
    target.append(tr);
  }
  if (scrollHost) { scrollHost.scrollLeft = left; scrollHost.scrollTop = top; }
  if (focusedId) {
    const row = [...target.querySelectorAll("tr[data-task-id]")].find((entry) => entry.dataset.taskId === focusedId);
    const control = row?.querySelector(`.${focusedClass}:not(:disabled)`) || row?.querySelector(".task-open");
    control?.focus({ preventScroll: true });
  }
}

function renderTaskList() {
  const filter = $("task-filter").value;
  const visible = taskItems.filter((item) => filter === "active" ? ["RUNNING", "QUEUED"].includes(item.status) :
    filter === "attention" ? ["FAILED", "PARTIAL_FAILURE"].includes(item.status) :
    filter === "finished" ? !["RUNNING", "QUEUED"].includes(item.status) : true);
  const running = taskItems.filter((item) => item.status === "RUNNING").length;
  const queued = taskItems.filter((item) => item.status === "QUEUED").length;
  const attention = taskItems.filter((item) => ["FAILED", "PARTIAL_FAILURE"].includes(item.status)).length;
  const summary = `${count(taskItems.length)} 条记录 · ${count(running)} 运行 · ${count(queued)} 排队 · ${count(attention)} 需要关注`;
  if ($("task-summary").textContent !== summary) $("task-summary").textContent = summary;
  taskRows($("all-tasks"), visible.slice(0, 30), true);
  if (selectedTaskId) {
    const current = taskItems.find((item) => item.operation_id === selectedTaskId);
    if (current) showTaskDetails(current);
  }
}

let selectedBackup = "";
let restorePlanDigest = "";

function openBackupDialog(name) {
  selectedBackup = name;
  restorePlanDigest = "";
  $("backup-dialog-name").textContent = name;
  $("backup-passphrase").value = "";
  $("backup-plan").textContent = "尚未预览";
  $("backup-dialog-restore").disabled = true;
  $("backup-restore-dialog").showModal();
  $("backup-passphrase").focus();
}

async function loadBackups() {
  const status = $("backup-status");
  const list = $("backup-list");
  try {
    const payload = await api("/api/admin/backups/profile");
    const items = Array.isArray(payload.items) ? payload.items : [];
    status.textContent = items.length ? `已保存 ${items.length} 份加密备份；最近一次 ${beijingTime(items[0].createdAt)}` : "尚无加密备份；启动服务后会按日创建。";
    list.replaceChildren();
    for (const item of items.slice(0, 12)) {
      const row = document.createElement("div"); row.className = "backup-row";
      const name = document.createElement("span"); name.textContent = `${beijingTime(item.createdAt)} · ${(item.bytes / 1048576).toFixed(2)} MiB`;
      const inspect = document.createElement("button"); inspect.textContent = "校验／恢复";
      inspect.addEventListener("click", () => openBackupDialog(item.name));
      row.append(name, inspect); list.append(row);
    }
  } catch (error) { status.textContent = `备份状态读取失败：${error.message}`; }
}

let publicationLoading = false;

function diagnosticTone(id, tone) {
  $(id).dataset.tone = tone;
}

function indexBuildLabel(index) {
  const build = index?.build;
  if (index?.ready) return "当前版本已就绪";
  if (!build || typeof build !== "object") return "待核验或尚未开始";
  const stages = {
    CHECKING: "检查来源修订", ADOPTING: "迁移旧索引", DELTA_CHECK: "核对分区差集",
    AUDIT_SHA: "核验原件 SHA", AUDIT_METADATA: "核验原件行数",
    INDEX_FILES: "构建查询目录", INDEX_METADATA: "写入目录元数据",
    PUBLISHING: "发布查询索引",
  };
  if (build.state === "RUNNING") {
    const stage = stages[build.stage] || "准备查询索引";
    const done = build.completed;
    const total = build.total;
    const unit = build.unit === "files" ? "个文件" : build.unit === "passes" ? "遍" : "";
    const progress = Number.isInteger(done) && Number.isInteger(total) && total > 0 && done >= 0 && done <= total
      ? ` · ${count(done)}/${count(total)} ${unit}` : "";
    return `${stage}${progress}`;
  }
  if (build.state === "FAILED") {
    const reasons = {
      AUDIT_FAILED: "原件核验未通过", INDEX_FAILED: "查询目录构建失败",
      PUBLICATION_FAILED: "索引发布失败", SOURCE_NOT_READY: "来源暂未就绪",
    };
    const delay = Number.isInteger(build.retryAfterSeconds) && build.retryAfterSeconds > 0
      ? ` · 约 ${count(build.retryAfterSeconds)} 秒后重试` : "";
    return `${reasons[build.failureCategory] || "索引恢复失败"}${delay}`;
  }
  if (build.state === "DEFERRED") return "来源任务仍在运行，等待恢复";
  return "待核验或尚未开始";
}

async function loadMarketPublication() {
  if (publicationLoading) return;
  publicationLoading = true;
  try {
  if (!state?.ready) {
    $("market-publication-status").textContent = "客户服务未启动";
    $("index-build-status").textContent = "客户服务未启动";
    $("silver-writes-status").textContent = "客户服务未启动";
    $("silver-recovery-status").textContent = "客户服务未启动";
    for (const id of ["market-publication-status", "index-build-status", "silver-writes-status", "silver-recovery-status"]) diagnosticTone(id, "muted");
    return;
  }
  try {
    const value = await api("/api/admin/market-publication");
    const models = value.models || {};
    const main = models["category:cn-future-main"] || {};
    const whole = models.all || {};
    const label = status => status === "CURRENT" ? "已核对当前来源" :
      status === "STALE" ? "显示上次已发布版本" : "尚无可用快照";
    const writes = value.silverWrites || {};
    const writeLabel = writes.status === "CLEAR" && writes.count === 0 ? "已结清，无待完成写入" :
      writes.status === "PENDING" ? `待核对 ${Number.isInteger(writes.count) && writes.count >= 0 ? count(writes.count) : "若干"} 笔写入` :
      writes.status === "INVALID" ? "修订记录无效，需核查" : "状态未提供";
    const recovery = value.silverRecovery || {};
    const recoveryLabels = {
      NOT_NEEDED: "无需恢复", CLEAR: "已核对，无需恢复", RECOVERED: "已自动恢复",
      REVIEW_REQUIRED: "需人工核查", OWNER_RUNNING: "写入进程仍在运行",
      RETRY_LATER: "暂时无法核验，将稍后重试", NOT_RUN: "尚未执行恢复检查",
    };
    const recoveryReasons = {
      INVALID_REVISION_STATE: "修订记录无效", INVALID_PENDING_LEDGER: "待完成记录无效",
      MULTIPLE_PENDING_WRITES: "存在多笔待完成写入", INVALID_REVISION_JOURNAL: "修订流水无效",
      UNPROVEN_WRITE_INTENT: "写入意图无法证明", UNKNOWN_WRITER: "写入进程未知",
      UNKNOWN_LATER_WRITE: "后续写入无法核验", DUPLICATE_CATALOG_PATH: "目录存在重复分区",
      INVALID_CATALOG_PARTITION: "目录分区无效", CATALOG_FILE_MISMATCH: "目录与文件不一致",
      LATER_WRITE_MISMATCH: "后续写入与目录不一致", UNREGISTERED_FILE: "文件未登记",
      EVIDENCE_UNAVAILABLE: "核验证据暂不可用", CATALOG_BUSY: "目录暂时繁忙",
      REVISION_CHANGED: "核验期间修订已变化",
    };
    const recoveryModes = {
      COMMITTED_OR_SUPERSEDED: "已提交或被后续写入覆盖", ABORTED: "未提交写入已撤销",
      ABORTED_NEW_PARTITION: "未提交的新分区已撤销",
    };
    const recoveryDetail = recovery.status === "RECOVERED" ? recoveryModes[recovery.mode] : recoveryReasons[recovery.reason];
    const recoveryLabel = `${recoveryLabels[recovery.status] || "状态未提供"}${recoveryDetail ? ` · ${recoveryDetail}` : ""}`;
    $("market-publication-status").textContent =
      `K线索引：${value.queryIndex?.ready ? "就绪" : "重建中或未就绪"} · 默认期货：${label(main.snapshotStatus)} · 全市场：${label(whole.snapshotStatus)}`;
    $("index-build-status").textContent = indexBuildLabel(value.queryIndex);
    $("silver-writes-status").textContent = writeLabel;
    $("silver-recovery-status").textContent = recoveryLabel;
    diagnosticTone("market-publication-status", value.queryIndex?.ready && main.snapshotStatus === "CURRENT" && whole.snapshotStatus === "CURRENT" ? "success" : "warning");
    diagnosticTone("index-build-status", value.queryIndex?.ready ? "success" : value.queryIndex?.build?.state === "FAILED" ? "danger" : "warning");
    diagnosticTone("silver-writes-status", writes.status === "CLEAR" && writes.count === 0 ? "success" : writes.status === "INVALID" ? "danger" : "warning");
    diagnosticTone("silver-recovery-status", ["CLEAR", "NOT_NEEDED", "RECOVERED"].includes(recovery.status) ? "success" : recovery.status === "REVIEW_REQUIRED" ? "danger" : "warning");
    const queryIndex = value.queryIndex || {};
    const indexBuild = queryIndex.build || {};
    $("market-publication-detail").textContent = JSON.stringify({
      当前来源版本: value.currentSourceVersion,
      默认期货: { 状态: main.snapshotStatus, 行数: main.rowCount ?? null, 查询版本: main.queryVersion ?? null },
      全市场: { 状态: whole.snapshotStatus, 行数: whole.rowCount ?? null, 查询版本: whole.queryVersion ?? null },
      K线索引: {
        就绪: queryIndex.ready === true, 版本: queryIndex.revision ?? null,
        行数: queryIndex.rows ?? null, 构建时间: queryIndex.builtAt ?? null,
        构建任务: {
          摘要: indexBuildLabel(queryIndex),
          运行秒数: Number.isInteger(indexBuild.elapsedSeconds) && indexBuild.elapsedSeconds >= 0
            ? indexBuild.elapsedSeconds : null,
        },
      },
    }, null, 2);
  } catch (error) {
    $("market-publication-status").textContent = `行情快照状态读取失败：${error.message}`;
    $("index-build-status").textContent = "读取失败；保留上次结果";
    $("silver-writes-status").textContent = "读取失败；保留上次结果";
    $("silver-recovery-status").textContent = "读取失败；保留上次结果";
    for (const id of ["market-publication-status", "index-build-status", "silver-writes-status", "silver-recovery-status"]) diagnosticTone(id, "danger");
  }
  } finally {
    publicationLoading = false;
  }
}

function backupRequest() {
  return { name: selectedBackup, passphrase: $("backup-passphrase").value || null };
}

$("backup-passphrase").addEventListener("input", () => {
  restorePlanDigest = "";
  $("backup-dialog-restore").disabled = true;
  $("backup-plan").textContent = "口令已改变，请重新预览";
});
$("backup-dialog-verify").addEventListener("click", async () => {
  const button = $("backup-dialog-verify"); button.disabled = true;
  try {
    const result = await api("/api/admin/backups/profile/verify", "POST", backupRequest());
    $("backup-plan").textContent = `文件完整性已核验：${result.fileCount} 项。`;
  } catch (error) { $("backup-plan").textContent = `校验未通过：${error.message}`; }
  finally { button.disabled = false; }
});
$("backup-dialog-preview").addEventListener("click", async () => {
  const button = $("backup-dialog-preview"); button.disabled = true;
  restorePlanDigest = "";
  $("backup-dialog-restore").disabled = true;
  try {
    const plan = await api("/api/admin/backups/profile/restore-preview", "POST", backupRequest());
    restorePlanDigest = plan.planDigest;
    const examples = (plan.paths || []).slice(0, 20).map(item => `${item.action} ${item.path}`).join("\n");
    $("backup-plan").textContent = `将替换 ${plan.replaceCount}、创建 ${plan.createCount}、移除 ${plan.removeCount} 项文件。\n${examples}${plan.paths?.length > 20 ? "\n…" : ""}`;
    $("backup-dialog-restore").disabled = false;
  } catch (error) { $("backup-plan").textContent = `恢复预览未完成：${error.message}`; }
  finally { button.disabled = false; }
});
$("backup-dialog-restore").addEventListener("click", async () => {
  if (!restorePlanDigest) return;
  const button = $("backup-dialog-restore"); button.disabled = true;
  try {
    const started = await api("/api/admin/backups/profile/restore", "POST",
      { ...backupRequest(), planDigest: restorePlanDigest });
    $("backup-plan").textContent = "恢复作业已启动，正在核验与替换文件。";
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const job = await api(`/api/admin/backups/profile/restore/${encodeURIComponent(started.id)}`);
      if (job.status === "PASS") { $("backup-plan").textContent = "个人资料恢复完成；可重新启动客户服务。"; return; }
      if (job.status === "FAILED") { $("backup-plan").textContent = `恢复未完成：${job.detail || "检查恢复日志"}`; return; }
    }
    $("backup-plan").textContent = "恢复仍在进行；稍后刷新任务状态。";
  } catch (error) { $("backup-plan").textContent = `恢复未启动：${error.message}`; }
});
$("backup-dialog-close").addEventListener("click", () => $("backup-restore-dialog").close());
$("backup-restore-dialog").addEventListener("close", () => {
  $("backup-passphrase").value = ""; restorePlanDigest = ""; selectedBackup = "";
});

async function loadTasks() {
  if (!state?.ready) {
    $("task-error").textContent = "客户服务未启动，暂时无法读取任务。";
    $("overview-task-error").textContent = "客户服务未启动，暂时无法读取任务。";
    $("queue-summary").textContent = "暂不可读";
    $("task-summary").textContent = "客户服务未启动，任务暂不可读";
    return;
  }
  if (taskLoading) return;
  taskLoading = true;
  taskLastReadAt = Date.now();
  try {
    const response = await api("/api/admin/tasks");
    if (!state?.ready) return;
    taskItems = response.items || [];
    const running = taskItems.filter((item) => item.status === "RUNNING").length;
    const queued = taskItems.filter((item) => item.status === "QUEUED").length;
    $("queue-summary").textContent = running || queued ? `${running} 运行 · ${queued} 排队` : "空闲";
    const filter = $("task-filter").value;
    const visible = taskItems.filter((item) => filter === "active" ? ["RUNNING", "QUEUED"].includes(item.status) :
      filter === "attention" ? ["FAILED", "PARTIAL_FAILURE"].includes(item.status) :
      filter === "finished" ? !["RUNNING", "QUEUED"].includes(item.status) : true);
    const fingerprint = JSON.stringify({
      length: taskItems.length,
      running, queued,
      attention: taskItems.filter((item) => ["FAILED", "PARTIAL_FAILURE"].includes(item.status)).length,
      recent: taskItems.slice(0, 5).map(taskDisplayState),
      visible: visible.slice(0, 30).map(taskDisplayState),
      selected: taskDisplayState(taskItems.find((item) => item.operation_id === selectedTaskId)),
    });
    if (fingerprint !== taskRenderFingerprint) {
      taskRows($("recent-tasks"), taskItems.slice(0, 5));
      renderTaskList();
      taskRenderFingerprint = fingerprint;
    }
    $("task-error").textContent = ""; $("overview-task-error").textContent = "";
  } catch (error) {
    if (!state?.ready) return;
    $("queue-summary").textContent = "读取失败";
    $("task-summary").textContent = "任务状态读取失败；可在运行日志核对服务连接";
    $("task-error").textContent = error.message; $("overview-task-error").textContent = error.message;
  } finally { taskLoading = false; }
}

function renderLogs() {
  const query = $("log-search").value.trim().toLowerCase();
  const errorsOnly = $("log-level").value === "error";
  const compact = $("log-compact").checked;
  const lines = logLines.filter((line) => {
    if (query && !line.toLowerCase().includes(query)) return false;
    if (errorsOnly && !/warning|error|failed|failure|失败|错误|警告|attention_required/i.test(line)) return false;
    if (compact && (/^\s*\d+%\|/.test(line) || /"(?:GET|HEAD) .+ HTTP.+" (?:2\d\d|304)/.test(line) || !line.trim())) return false;
    return true;
  });
  $("log-output").textContent = lines.length ? lines.join("\n") : "当前筛选下没有日志记录。";
  if ($("log-scroll").checked) $("log-output").scrollTop = $("log-output").scrollHeight;
}

async function loadLogs() {
  if (logPaused) return;
  try { const result = await api(`/api/admin/logs?source=${encodeURIComponent($("log-source").value)}`); logLines = result.lines; renderLogs(); $("log-status").textContent = `最近 ${logLines.length} 行`; }
  catch (error) { $("log-status").textContent = error.message; }
}

async function diagnostics() {
  try {
    const result = await api("/api/admin/diagnostics");
    const items = [["Python", result.python], ["运行系统", result.platform], ["客户前端构建", result.clientBuilt ? "已就绪" : "尚未构建"], ["数据目录", result.dataRootExists ? "存在" : "尚未创建"], ["员工身份", result.employeeAccounts], ["核对时间", beijingTime(result.checkedAt)]];
    $("diagnostic-result").replaceChildren();
    for (const [key, value] of items) { const dt = document.createElement("dt"); dt.textContent = key; const dd = document.createElement("dd"); dd.textContent = value; $("diagnostic-result").append(dt, dd); }
  } catch (error) { notice(error.message, true); }
}

async function loadInventory() {
  if ($("load-inventory").disabled) return;
  $("load-inventory").disabled = true;
  try {
    const value = await api("/api/admin/inventory");
    if (!value.storage?.available && !(value.inventory || []).length) {
      $("inventory-status").textContent = "本地行情清单尚未就绪，无法据此判断数据为空；保留上次已读取内容。";
      return;
    }
    const storageState = value.storage?.snapshotStatus;
    const catalogState = value.metadata?.catalogStatus;
    $("inventory-status").textContent = catalogState === "TEMPORARILY_UNAVAILABLE"
      ? "行情清单可读；Catalog 暂时被更新任务占用，表目录待重试。"
      : storageState === "BUILDING"
        ? "行情清单已读取；存储占用正在后台盘点，稍后刷新可查看。"
        : storageState === "STALE"
          ? "行情清单已读取；存储占用显示上次完整盘点，正在后台更新。"
          : "已读取实际本地清单";
    $("inventory-rows").textContent = Number.isFinite(value.summary?.rows) ? value.summary.rows.toLocaleString("zh-CN") : "—";
    $("inventory-instruments").textContent = Number.isFinite(value.summary?.instruments) ? value.summary.instruments.toLocaleString("zh-CN") : "—";
    const bytes = value.storage.bytes;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    const scale = Number.isFinite(bytes) && bytes > 0 ? Math.max(0, Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)))) : 0;
    $("inventory-storage").textContent = value.storage?.available && Number.isFinite(bytes) && bytes >= 0 ? `${(bytes / 1024 ** scale).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${units[scale]}` : "—";
    $("inventory-table").replaceChildren();
    for (const item of value.inventory || []) {
      const row = document.createElement("tr");
      for (const text of [`${item.market} / ${item.assetType} / ${item.period}`, Number.isFinite(item.instruments) ? item.instruments.toLocaleString("zh-CN") : "—", Number.isFinite(item.rows) ? item.rows.toLocaleString("zh-CN") : "—", (item.sourceDetails || []).map((source) => Number.isFinite(item.sourceSampleCounts?.[source.providerId]) ? `${source.name}（样本 ${item.sourceSampleCounts[source.providerId]}）` : source.name).join("、") || "该周期暂无来源样本", fullBeijingTime(item.earliestBarAt), fullBeijingTime(item.latestBarAt)]) {
        const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
      }
      $("inventory-table").append(row);
    }
  } catch (error) { $("inventory-status").textContent = `盘点读取失败：${error.message}。不能据此判断数据为空；保留上次已读取内容。`; }
  finally { $("load-inventory").disabled = false; }
}

window.setInterval(() => {
  if (!document.hidden && !$('page-sources').classList.contains('hidden')) void loadInventory();
}, 120_000);

const manualGapStatuses = {
  UNKNOWN: "尚未核验", AUDIT_STALE: "核验已过期", SOURCE_AHEAD: "原文件待入库",
  CANDIDATE_GAPS: "候选缺口", NO_GAPS_OBSERVED: "观测范围未见缺口",
  VERIFIED_MISSING: "已核验缺失", CURRENT_WITHIN_OBSERVED_RANGE: "观测范围已覆盖",
  CALENDAR_STALE: "交易日历待更新", EVENT_DRIVEN: "按事件发布",
};
const manualGapSemantics = {
  TRADING_DAY_CANDIDATE: "交易日候选", TRADING_DAY_VERIFIED: "已核验交易日", EVENT_DRIVEN: "按事件发布",
  SOURCE_DEPENDENT: "按来源分别判断",
};
const manualGapUpdateModes = {
  MANUAL_DOWNLOAD_AUTO_IMPORT: "手动下载 · 自动导入",
  AUTO_START_AND_MANUAL: "启动增量 · 可手动更新",
  AUTO_DAILY_19_AND_MANUAL: "每日19:00检查 · 晚启动补查 · 可手动更新",
  AUTO_WEEKLY_AND_MANUAL: "定期检查 · 可手动更新",
  MANUAL_TASK_EVENT: "手动任务 · 按事件",
  SOURCE_DEPENDENT: "按来源分别更新",
};
const manualGapDateKinds = {
  MISSING: "缺少数据", SOURCE_MISSING: "源文件缺少", IMPORT_MISSING: "尚未入库",
  SOURCE_AHEAD: "源文件已更新，等待入库", CANDIDATE: "待核实",
};

function manualGapDateType(item) {
  const confidence = String(item.confidence || "").toUpperCase();
  if (["VERIFIED", "CONFIRMED", "VERIFIED_MISSING"].includes(confidence)) return "verified";
  return "candidate";
}

function manualGapStatusText(item) {
  const status = manualGapStatuses[item.status] || item.status || "状态待核验";
  const dates = Array.isArray(item.missingDates) ? item.missingDates : [];
  const verified = dates.filter((date) => manualGapDateType(date) === "verified").length;
  if (item.dateSemantics === "EVENT_DRIVEN") return Number(item.missingItemCount) > 0
    ? `按事件发布 · 最近断点有 ${count(item.missingItemCount)} 个失败代码，无逐日缺口定义`
    : "按事件发布，无逐日缺口定义";
  const total = item.dateSemantics === "TRADING_DAY_VERIFIED" && Number.isFinite(item.missingCount) && item.missingCount >= 0
    ? `${count(item.missingCount)} 个相关日期` : null;
  if (item.status === "SOURCE_AHEAD") return "本地原文件较已入库数据更新，等待导入核对";
  if (item.status === "CALENDAR_STALE") return "交易日历待更新，不能确认日期缺口";
  if (verified > 0) return `${status} · ${total || `已列 ${count(dates.length)} 个相关日期`}，其中 ${count(verified)} 个已核验`;
  if (dates.length > 0) return `${status} · ${total || `已列 ${count(dates.length)} 个相关日期`}，仍需核实`;
  if (item.status === "NO_GAPS_OBSERVED" || item.status === "CURRENT_WITHIN_OBSERVED_RANGE") return "本次观测范围内未列出缺口";
  return `${status} · 尚无可列日期`;
}

function manualGapOperationButton(kind, label) {
  const known = [...document.querySelectorAll(".action-grid [data-operation]")].some((button) => button.dataset.operation === kind);
  if (!kind || !known) return null;
  const button = textElement("button", label);
  button.type = "button";
  button.dataset.operation = kind;
  button.disabled = !state?.ready || Boolean(state?.action);
  button.addEventListener("click", () => { void submitOperation(kind); });
  return button;
}

function renderManualGapItem(item, filter = {}) {
  const card = textElement("section", "", "manual-gap-entry");
  const heading = textElement("div", "", "manual-gap-entry-heading");
  const headingText = textElement("div", "");
  headingText.append(textElement("h3", item.label || item.key || "未命名数据源"));
  const mode = manualGapUpdateModes[item.updateMode] || item.updateMode || "更新方式未登记";
  const semantics = manualGapSemantics[item.dateSemantics] || item.dateSemantics || "日期口径待核验";
  headingText.append(textElement("p", `${mode} · ${semantics} · 已有记录 ${dateRange(item.startDate, item.latestDate)}`, "manual-gap-meta"));
  if (item.importedLatestDate) headingText.append(textElement("p", `已入库至 ${fileDate(item.importedLatestDate)}`, "manual-gap-meta"));
  const status = textElement("span", manualGapStatuses[item.status] || item.status || "待核验", `badge manual-gap-status ${item.status || ""}`);
  heading.append(headingText, status); card.append(heading);

  const dateRows = Array.isArray(item.missingDates) ? item.missingDates : [];
  card.append(textElement("p", manualGapStatusText(item)));
  if (Number.isFinite(item.candidateDateCount) && item.candidateDateCount > 0) {
    const occurrences = Number.isFinite(item.candidateGapCount) && item.candidateGapCount !== item.candidateDateCount
      ? ` · 涉及 ${count(item.candidateGapCount)} 个文件或数据槽` : "";
    card.append(textElement("p", `${count(item.candidateDateCount)} 个不同的候选日期${occurrences}；并非已核验缺失交易日。`, "manual-gap-meta"));
  }
  if (item.expectedThroughDate && item.dateSemantics !== "EVENT_DRIVEN") {
    card.append(textElement("p", `比较截至 ${fileDate(item.expectedThroughDate)} · 最近核验 ${fullBeijingTime(item.auditAt)}`, "manual-gap-meta"));
  } else {
    card.append(textElement("p", `最近核验 ${fullBeijingTime(item.auditAt)}`, "manual-gap-meta"));
  }
  if (item.lastRecordedBatch) {
    const batch = item.lastRecordedBatch;
    card.append(textElement("p", `最近已记录批次 ${fullBeijingTime(batch.completedAt)} · 本次更新 ${count(batch.updated)} 家 · 待处理 ${count(batch.remaining)} 家 · 失败 ${count(batch.failed)} 项 · 本机已有记录 ${count(batch.stored)} 家`, "manual-gap-meta"));
  }
  if (item.observedStatistics) {
    const stats = item.observedStatistics;
    card.append(textElement("p", `已入库 ${count(stats.records)} 条指标记录 · ${count(stats.categories)} 个统计类别 · ${count(stats.metrics)} 个指标名称 · 最近本机写入 ${fullBeijingTime(stats.lastWrittenAt)}`, "manual-gap-meta"));
    card.append(textElement("p", `已观测日期 ${fileDate(item.startDate)} 至 ${fileDate(item.latestDate)}；不代表每日完整或个股数量。`, "manual-gap-meta"));
  }
  if (item.listingSnapshot) {
    const listing = item.listingSnapshot;
    const label = listing.status === "PASS" ? `所选范围 ${count(listing.records)} 条 · 快照总计 ${count(listing.totalRecords)} 条 · 本机抓取 ${fullBeijingTime(listing.fetchedAt)}`
      : listing.status === "INVALID" ? "来源快照校验失败，记录数未知。" : "尚无已核验快照，记录数未知。";
    card.append(textElement("p", label, "manual-gap-meta"));
    if (listing.review) {
      const review = listing.review;
      card.append(textElement("p", `名单历史记录发生变化：${count(review.affectedCount)} 只待核 · 本机发现 ${fullBeijingTime(review.observedAt)}；旧快照保留。`, "manual-gap-meta"));
      if (review.lastCheckedAt) card.append(textElement("p", `最近再次检查 ${fullBeijingTime(review.lastCheckedAt)}；来源原件与待核版本一致。`, "manual-gap-meta"));
      if (Array.isArray(review.changes) && review.changes.length) {
        const details = textElement("details", "");
        details.append(textElement("summary", `查看变动明细（已列 ${count(review.changes.length)} / ${count(review.matchedCount)} 条）`));
        details.append(textElement("p", "按变动前或变动后的摘牌日期筛选；候选缺失不等于确认重新上市。", "manual-gap-meta"));
        const list = textElement("ul", "", "manual-gap-meta");
        const kinds = { MISSING:"候选名单未见该代码", DATE_CHANGED:"摘牌日期变化", NAME_CHANGED:"名称变化" };
        const renderChanges = (changes) => {
          list.replaceChildren();
          for (const change of changes) {
            const before = change.previousDate || "未知";
            const after = change.currentDate || "未提供";
            const names = change.previousName || change.currentName ? `；原名称：${change.previousName || "未知"}，候选名称：${change.currentName || "未提供"}` : "";
            list.append(textElement("li", `${change.instrumentId || "未知代码"} · ${kinds[change.kind] || "类型待核"}；原摘牌日：${before}，候选摘牌日：${after}${names}`));
          }
        };
        renderChanges(review.changes);
        details.append(list);
        if (review.truncated && /^[0-9a-f]{64}$/.test(review.version || "")) {
          const pageSize = review.changes.length;
          let offset = 0, pageBusy = false, blocked = false;
          const controls = textElement("div", "", "button-row");
          const previous = textElement("button", "上一页"), next = textElement("button", "下一页");
          previous.type = next.type = "button";
          const pageStatus = textElement("span", `第 1–${count(pageSize)} 条，共 ${count(review.matchedCount)} 条`, "manual-gap-meta");
          const message = textElement("p", "", "manual-gap-meta"); message.setAttribute("role", "status");
          previous.disabled = true;
          const setDisabled = () => { previous.disabled = pageBusy || blocked || offset === 0; next.disabled = pageBusy || blocked || offset + pageSize >= review.matchedCount; };
          const go = async (nextOffset) => {
            if (pageBusy || blocked) return;
            pageBusy = true; message.textContent = "正在读取…"; setDisabled();
            const exchange = item.key === "exchange.sse-delisted" ? "SSE" : "SZSE";
            const params = new URLSearchParams({version:review.version,offset:String(nextOffset),limit:String(pageSize)});
            if (filter.start) params.set("start", filter.start);
            if (filter.end) params.set("end", filter.end);
            try {
              const data = await api(`/api/admin/delisted-review/${exchange}?${params}`);
              if (!details.isConnected) return;
              if (data.version !== review.version || data.offset !== nextOffset || data.matchedCount !== review.matchedCount || !Array.isArray(data.changes)) throw new Error("待核名单已变化，请重新查询");
              offset = nextOffset; renderChanges(data.changes); message.textContent = "";
              pageStatus.textContent = `第 ${count(offset + 1)}–${count(offset + data.changes.length)} 条，共 ${count(data.matchedCount)} 条`;
            } catch (error) {
              if (!details.isConnected) return;
              message.textContent = `${error.message}；保留当前页。`;
              if (error.message.includes("待核名单已变化")) blocked = true;
            } finally { pageBusy = false; if (details.isConnected) setDisabled(); }
          };
          previous.addEventListener("click", () => { void go(Math.max(0, offset - pageSize)); });
          next.addEventListener("click", () => { void go(offset + pageSize); });
          controls.append(previous, next, pageStatus); details.append(controls, message);
        } else if (review.truncated) {
          details.append(textElement("p", "仅展示部分条目；当前服务版本尚不支持翻页。", "manual-gap-meta"));
        }
        try {
          const link = new URL(review.sourceUrl);
          if (link.protocol === "https:" && ["www.sse.com.cn", "www.szse.cn"].includes(link.hostname)) {
            const anchor = textElement("a", "查看交易所原名单");
            anchor.href = link.href; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
            details.append(anchor);
          }
        } catch { /* malformed evidence URL is not presented */ }
        card.append(details);
      } else if (Array.isArray(review.affectedIds) && review.affectedIds.length) {
        card.append(textElement("p", `待核代码：${review.affectedIds.join("、")}${review.truncated ? " 等（仅展示部分）" : ""}`, "manual-gap-meta"));
      } else if (review.matchedCount === 0) {
        card.append(textElement("p", "所选日期范围内没有变动条目；可调整日期范围查看。", "manual-gap-meta"));
      }
    }
    if (listing.lastJob) {
      const job = listing.lastJob;
      const states = { QUEUED:"排队中", RUNNING:"运行中", PASS:"通过", PARTIAL_FAILURE:"部分失败", FAILED:"失败", CANCELLED:"已取消" };
      const outcomes = { PASS:"本所更新成功", SKIPPED_FRESH:"本所快照在当前窗口内", FAILED:"本所更新失败，保留旧快照" };
      card.append(textElement("p", `最近自动任务 ${fullBeijingTime(job.at)} · ${states[job.status] || "状态未知"}${job.sourceOutcome ? ` · ${outcomes[job.sourceOutcome] || "本所结果未知"}` : ""}`, "manual-gap-meta"));
    }
    card.append(textElement("p", "摘牌日期范围仅反映名单中记录，不代表每日缺口或历史公告时间。", "manual-gap-meta"));
  }
  if (item.observedSeries) {
    const series = item.observedSeries;
    const frequencies = { DAILY: "日度", MONTHLY: "月度", QUARTERLY: "季度" };
    card.append(textElement("p", `已入库 ${count(series.records)} 条 · ${frequencies[series.frequency] || series.frequency} · 观测范围 ${series.first} 至 ${series.latest} · 最近本机写入 ${fullBeijingTime(series.lastWrittenAt)}`, "manual-gap-meta"));
    card.append(textElement("p", "全库范围，不随上方日期筛选；不代表逐期完整。", "manual-gap-meta"));
  }
  if (item.qualityIssue) {
    const issue = item.qualityIssue;
    card.append(textElement("p", `相关历史分区 ${count(issue.partitions)} 个 · 清单记录 ${count(issue.rows)} 行 · 最后截点 ${fullBeijingTime(issue.lastCutoff)}；是否错误仍须逐条核验。`, "manual-gap-meta"));
  }
  if (item.key?.startsWith("collector.")) {
    const activity = item.collectorActivity;
    const labels = { PASS: "通过", SKIPPED: "跳过", PARTIAL_FAILURE: "部分失败", FAILED: "失败", BLOCKED: "受阻" };
    card.append(textElement("p", activity
      ? `最近有记录会话 ${fullBeijingTime(activity.sessionCompletedAt)} · ${labels[activity.status] || "状态未知"} · 处理 ${count(activity.rowsProcessed)} 条（非新增数）`
      : "尚无可读取的采集会话记录；不代表没有数据。", "manual-gap-meta"));
    const warnings = Array.isArray(activity?.freshnessWarnings) ? activity.freshnessWarnings : [];
    const names = { US_NONFARM_PAYROLLS_SA: "旧非农来源", PMI_CAIXIN_MANUFACTURING: "财新制造业 PMI", PMI_CAIXIN_SERVICES: "财新服务业 PMI" };
    for (const warning of warnings) {
      const name = names[warning.seriesId] || "宏观指标";
      const reason = warning.reason === "STALE_OVER_180_DAYS" ? "本次响应最新观测超过 180 天" : "本次未取得可用观测";
      card.append(textElement("p", `${name}：${reason}；不据此推断发布日期或是否缺报。`, "manual-gap-meta"));
    }
  }
  if (dateRows.length) {
    const details = textElement("details", "");
    const shown = Number.isFinite(item.returnedCount) ? item.returnedCount : dateRows.length;
    details.append(textElement("summary", `查看相关日期（已列 ${count(shown)}${item.truncated ? "，非完整清单" : ""}）`));
    details.append(textElement("p", "实线框表示已核验日期；虚线框表示候选或抽样日期。日期类别可悬停查看。", "manual-gap-meta"));
    const dates = textElement("div", "", "manual-gap-dates");
    for (const entry of dateRows) {
      const candidate = manualGapDateType(entry) !== "verified";
      const day = fileDate(entry.date);
      const time = entry.time ? ` ${entry.time}` : "";
      const chip = textElement("span", `${day}${time}`, `manual-gap-date${candidate ? " candidate" : ""}`);
      const kind = manualGapDateKinds[entry.kind] || entry.kind || "";
      const certainty = candidate ? "待核实或抽样日期" : "已核验相关日期";
      const affected = Number.isFinite(entry.affectedFiles) ? ` · 涉及${count(entry.affectedFiles)}个文件` : "";
      const examples = Array.isArray(entry.sampleContracts) && entry.sampleContracts.length
        ? ` · 示例合约：${entry.sampleContracts.join("、")}` : "";
      chip.title = `${certainty}${kind ? ` · ${kind}` : ""}${affected}${examples}`;
      chip.setAttribute("aria-label", `${day}${time} · ${certainty}${kind ? ` · ${kind}` : ""}${affected}${examples}`);
      dates.append(chip);
    }
    details.append(dates);
    if (item.truncated) details.append(textElement("p", "只列出部分日期；可缩小查询区间继续核对。", "manual-gap-meta"));
    card.append(details);
  }
  if (Array.isArray(item.missingItems) && item.missingItems.length) {
    const details = textElement("details", "");
    details.append(textElement("summary", `查看失败代码（${count(item.missingItemCount || item.missingItems.length)}）`));
    const codes = textElement("div", "", "manual-gap-dates");
    for (const missing of item.missingItems) {
      const code = String(missing.code || "");
      if (code) codes.append(textElement("span", code, "manual-gap-date candidate"));
    }
    details.append(codes);
    if (item.truncated) details.append(textElement("p", "只列出部分失败代码，可在采集断点中查看其余项目。", "manual-gap-meta"));
    card.append(details);
  }
  if (item.notes) {
    const note = textElement("details", "", "manual-gap-notes");
    note.append(textElement("summary", "查看口径与限制"), textElement("p", item.notes, "manual-gap-meta"));
    card.append(note);
  }

  const footer = textElement("div", "", "manual-gap-entry-footer");
  footer.append(textElement("small", item.key || ""));
  const actions = textElement("div", "", "button-row");
  const check = manualGapOperationButton(item.checkOperation, "重新核验");
  if (check) actions.append(check);
  if (item.actionKind && item.actionKind !== item.checkOperation) {
    const update = manualGapOperationButton(item.actionKind, state?.operationLabels?.[item.actionKind] || "提交更新");
    if (update) actions.append(update);
  }
  footer.append(actions); card.append(footer);
  return card;
}

function renderManualGaps(value, filter = {}) {
  const items = Array.isArray(value.items) ? value.items : [];
  const source = $("manual-gap-source");
  if (source.value === "all" && items.length) manualGapCatalog = items.map((item) => ({ key: item.key, label: item.label }));
  const chosen = source.value;
  source.replaceChildren(new Option("全部已登记类型", "all"));
  for (const item of manualGapCatalog) source.append(new Option(item.label || item.key, item.key));
  if ([...source.options].some((option) => option.value === chosen)) source.value = chosen;
  $("manual-gap-audit").textContent = `查询于 ${fullBeijingTime(value.generatedAt)} · ${count(items.length)} 类数据；每项核验时间见下方`
    + (value.backendError ? `。客户服务覆盖暂不可读：${value.backendError}` : "");
  const summary = $("manual-gap-summary"); summary.replaceChildren();
  for (const [label, amount] of [
    ["已登记类型", items.length],
    ["有确证日期", items.filter((item) => (item.missingDates || []).some((date) => manualGapDateType(date) === "verified")).length],
    ["待核实或抽样", items.filter((item) => (item.missingDates || []).some((date) => manualGapDateType(date) !== "verified")).length],
    ["不按日发布", items.filter((item) => item.dateSemantics === "EVENT_DRIVEN").length],
  ]) {
    const stat = textElement("span", "", "manual-gap-stat");
    stat.append(textElement("strong", count(amount)), textElement("span", label)); summary.append(stat);
  }
  const list = $("manual-gap-list"); list.replaceChildren();
  if (!items.length) {
    list.append(textElement("p", value.backendError
      ? "客户服务覆盖暂不可读，请启动客户服务后重新查询；本地通达信审计仍可单独查看。"
      : "所选条件下没有已登记的数据类型；请调整筛选或核对来源配置。", "manual-gap-empty"));
    return;
  }
  for (const item of items) list.append(renderManualGapItem(item, filter));
}

async function loadManualGaps() {
  if (manualGapLoading) return;
  const start = $("manual-gap-start").value;
  const end = $("manual-gap-end").value;
  if (start && end && start > end) { notice("起始日期不能晚于截至日期。", true); return; }
  manualGapLoading = true;
  const button = $("load-manual-gaps"); button.disabled = true;
  const oldText = button.textContent; button.textContent = "查询中…";
  $("manual-gap-audit").textContent = manualGapLoaded ? "正在重新核验查询，保留上次结果…" : "正在查询已保存的缺口证据…";
  const params = new URLSearchParams({ limit: "40" });
  if ($("manual-gap-source").value !== "all") params.set("source", $("manual-gap-source").value);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  try {
    renderManualGaps(await api(`/api/admin/manual-data-gaps?${params}`), { start, end });
    manualGapLoaded = true;
  } catch (error) {
    $("manual-gap-audit").textContent = `缺口查询失败：${error.message}${manualGapLoaded ? "；保留上次结果" : ""}`;
  } finally {
    manualGapLoading = false;
    button.disabled = false;
    button.textContent = oldText;
  }
}

window.setInterval(() => {
  const controls = document.querySelector(".manual-gap-controls");
  const readingDetails = $("manual-gap-list").querySelector("details[open]");
  if (!document.hidden && !$('page-data').classList.contains('hidden') && manualGapLoaded
      && !readingDetails && !controls?.contains(document.activeElement)) void loadManualGaps();
}, 120_000);

function showPage(key) {
  if (!pageInfo[key]) key = "overview";
  page = key;
  document.querySelectorAll(".page").forEach((section) => section.classList.toggle("hidden", section.id !== "page-" + key));
  document.querySelectorAll(".sidebar [data-page]").forEach((button) => {
    const active = button.dataset.page === key;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("page-title").textContent = pageInfo[key][0]; $("page-subtitle").textContent = pageInfo[key][1];
  history.replaceState(null, "", "#" + key);
  if (!csrf) return;
  if (key === "logs") void loadLogs();
  if (key === "diagnostics") { void diagnostics(); void loadMarketPublication(); }
  if (key === "tasks") void loadTasks();
  if (key === "sources") void loadInventory();
  if (key === "settings") void loadBackups();
  if (key === "overview" && Date.now() - tdxLastReadAt > 20000) void loadTdxStatus();
  if (key === "data" && !manualGapLoaded) void loadManualGaps();
  if (key === "research") { void loadResearchStatus(); void loadResearchCandidates(); }
}

async function saveSettings(changes = {}) {
  if (!state || settingsSaving) return;
  const value = { ...state.settings, auto_start: $("setting-auto-start").checked, auto_update: $("setting-auto-update").checked, background_priority: $("setting-background").checked, ...changes };
  settingsSaving = true;
  try { await api("/api/admin/settings", "PUT", value); state.settings = value; applyTheme(value.theme); notice("设置已保存；服务运行策略在下次启动客户服务时生效。"); }
  catch (error) { notice(error.message, true); renderState(state); }
  finally { settingsSaving = false; }
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); notice("已复制"); } catch { notice("复制未完成，请在页面选择文本复制。", true); }
}

async function refresh() {
  if (polling || closing || document.hidden) return;
  polling = true;
  try {
    const wasReady = state?.ready;
    renderState(await api("/api/admin/state", "GET", undefined, 6000));
    if (state.ready && !wasReady) {
      taskLastReadAt = 0;
      taskFastUntil = Date.now() + 20000;
    }
    const activeTasks = taskItems.some((item) => item.status === "RUNNING" || item.status === "QUEUED");
    const taskInterval = page === "tasks" || activeTasks || Date.now() < taskFastUntil ? 2000 : 5000;
    if (state.ready && Date.now() - taskLastReadAt >= taskInterval) void loadTasks();
    if (page === "logs") void loadLogs();
    if (page === "diagnostics") void loadMarketPublication();
    if (page === "overview" && (tdxStatus?.scanning || Date.now() - tdxLastReadAt > 20000)) void loadTdxStatus();
    if (page === "research" && Date.now() - researchLastReadAt > 5000) void loadResearchStatus();
  }
  catch (error) {
    const sessionMissing = Boolean(error.loginRequired);
    $("connection-note").textContent = sessionMissing ? "正在恢复本机会话" : "管理服务状态暂不可读，正在重试";
    $("service-title").textContent = sessionMissing ? "正在建立本机会话" : "正在核对客户服务状态";
    $("service-description").textContent = sessionMissing
      ? "本机浏览器会自动建立管理会话，请稍候。"
      : `${error.message}。不能据此判断客户进程已经停止；页面会继续自动重试。`;
    $("service-badge").textContent = sessionMissing ? "建立会话中" : "核对中";
    $("service-badge").className = `badge ${sessionMissing ? "FAILED" : "unresponsive"}`;
    $("service-card").classList.toggle("has-error", sessionMissing);
    $("service-log-shortcut").classList.add("hidden");
    document.querySelectorAll("[data-service]").forEach((button) => { button.disabled = true; });
    if (!sessionMissing) notice(error.message, true);
  }
  finally { polling = false; }
}

async function loadResearchStatus() {
  try {
    const value = await api("/api/admin/research/status");
    researchLastReadAt = Date.now();
    const corpus = value.corpus || {};
    $("research-status").textContent = `${count(corpus.documents)} 篇去重文档 · ${count(corpus.paths)} 个文件路径 · ${count(corpus.completedPages)} / ${count(corpus.pages)} 页已处理 · ${count(corpus.pendingPages)} 页待处理。页面：${JSON.stringify(corpus.pageStates || {})}；OCR：${JSON.stringify(corpus.ocrEngines || {})}；知识候选：${JSON.stringify(corpus.candidateStates || {})}；视觉页：${JSON.stringify(corpus.visualQueueStates || {})}；识图观察：${JSON.stringify(corpus.visualStates || {})}`;
    const background = value.background;
    $("research-background").textContent = background
      ? `全量提取：${background.phase} · 本次 ${count(background.sessionProcessed)} 页 · 最近记录 ${fullBeijingTime(background.updatedAt)}`
      : "全量后台提取尚无进度记录；可用单批按钮先行处理。";
    $("research-background-pause").disabled = !background || background.phase !== "RUNNING";
    $("research-background-resume").disabled = !background || !["PAUSED", "REVIEW_REQUIRED", "INTERRUPTED"].includes(background.phase);
    const models = value.models || {};
    $("research-model-status").textContent = `文本模型：${models.text?.status || "未知"} · 视觉模型：${models.visual?.status || "未知"}。两种模型顺序使用，避免争用显存。`;
    document.querySelectorAll("[data-model-control]").forEach((button) => {
      const [kind, action] = button.dataset.modelControl.split(":");
      const status = models[kind]?.status;
      button.disabled = status === "FOREIGN_PORT" || (action === "start" ? status !== "STOPPED" : status !== "RUNNING");
    });
    const jobs = $("research-jobs"); jobs.replaceChildren();
    for (const job of (value.jobs || []).slice(-5).reverse()) {
      const row = document.createElement("div"); row.className = "research-job";
      row.append(textElement("span", `${job.action} · ${job.status} · ${JSON.stringify(job.progress || job.result || {})}${job.detail ? ` · ${job.detail}` : ""}`));
      if (job.status === "RUNNING") {
        const cancel = document.createElement("button"); cancel.textContent = "暂停";
        cancel.addEventListener("click", async () => {
          try { await api(`/api/admin/research/jobs/${job.id}/cancel`, "POST"); notice("正在完成当前页后暂停"); }
          catch (error) { notice(error.message, true); }
        }); row.append(cancel);
      }
      jobs.append(row);
    }
    if (!(value.jobs || []).length) jobs.textContent = "尚无研报作业";
    const versions = $("research-versions");
    const chosen = versions.value;
    versions.replaceChildren();
    for (const version of value.publications || []) {
      const option = document.createElement("option"); option.value = version; option.textContent = version; versions.append(option);
    }
    if (chosen && (value.publications || []).includes(chosen)) versions.value = chosen;
  } catch (error) { $("research-status").textContent = `状态读取失败：${error.message}`; }
}

async function loadResearchDocuments() {
  try {
    const value = await api("/api/admin/research/documents?limit=100");
    const host = $("research-documents"); host.replaceChildren();
    for (const item of value.items || []) {
      const button = document.createElement("button"); button.type = "button";
      button.textContent = `${item.title} · ${item.document_kind} · ${item.completed_pages || 0}/${item.page_count} 页`;
      button.addEventListener("click", () => void openResearchPage(item.sha256, 1, item.title)); host.append(button);
    }
    if (!(value.items || []).length) host.textContent = "导入清单为空，请先扫描两份研报目录。";
  } catch (error) { notice(error.message, true); }
}

async function openResearchPage(sha256, page, label = "") {
  try {
    const value = await api(`/api/admin/research/pages/${sha256}/${page}`);
    selectedResearchPage = { sha256, page };
    $("research-page-label").textContent = `${label ? label + " · " : ""}原始第 ${page} 页 · ${value.status}${value.ocrEngine ? ` · ${value.ocrEngine}` : ""}`;
    $("research-page-text").textContent = value.text || "该页未识别到文字；请核对原图。";
    const image = $("research-page-image"); image.src = `/api/admin/research/pages/${sha256}/${page}/image`;
    image.classList.remove("hidden");
    $("research-confirm-blank").classList.toggle("hidden", value.status !== "NEEDS_REVIEW" || Boolean(value.text?.trim()));
    const disagreement = value.status === "NEEDS_REVIEW" && value.ocrComparison;
    $("research-ocr-comparison").classList.toggle("hidden", !disagreement);
    if (disagreement) {
      $("research-umi-text").textContent = `Umi-OCR · 数字一致率 ${Math.round((value.ocrComparison.number_agreement || 0) * 100)}%\n${value.ocrComparison.umi_text || "无文本"}`;
      $("research-rapid-text").textContent = `RapidOCR\n${value.ocrComparison.rapid_text || "无文本"}`;
    }
  } catch (error) { notice(error.message, true); }
}

async function loadResearchCandidates() {
  try {
    const status = $("research-candidate-status").value;
    const value = await api(`/api/admin/research/candidates?status=${status}&limit=100`);
    const host = $("research-candidates"); host.replaceChildren();
    for (const item of value.items || []) {
      const card = document.createElement("div"); card.className = "research-candidate";
      card.append(textElement("strong", `${item.kind === "QUANT_METHOD" ? "金工方法" : "产业关系"} · ${item.title} · 第 ${item.page} 页`));
      card.append(textElement("p", JSON.stringify(item.payload, null, 2)));
      card.append(textElement("blockquote", item.evidence_text));
      const actions = document.createElement("div"); actions.className = "button-row";
      const preview = document.createElement("button"); preview.textContent = "核对原页";
      preview.addEventListener("click", () => void openResearchPage(item.sha256, item.page, item.title)); actions.append(preview);
      if (item.status === "PENDING") for (const [decision, title] of [["APPROVED", "通过"], ["REJECTED", "拒绝"]]) {
        const button = document.createElement("button"); button.textContent = title;
        button.addEventListener("click", async () => {
          const reviewer = $("research-reviewer").value.trim(), note = $("research-review-note").value.trim();
          if (!reviewer || !note) { notice("请填写审核人和审核说明", true); return; }
          try { await api(`/api/admin/research/candidates/${item.id}/review`, "POST", { status: decision, reviewer, note,
            currentVerified: $("research-current-verified").checked }); notice("审核结果已保存"); await loadResearchCandidates(); await loadResearchStatus(); }
          catch (error) { notice(error.message, true); }
        }); actions.append(button);
      }
      card.append(actions); host.append(card);
    }
    if (!(value.items || []).length) host.textContent = "当前没有候选。未运行模型或尚未完成逐页提取时，这里会保持为空。";
  } catch (error) { notice(error.message, true); }
}

async function loadResearchVisual() {
  try {
    const status = $("research-visual-status").value;
    const value = await api(`/api/admin/research/visual?status=${status}&limit=100`);
    const host = $("research-visual-list"); host.replaceChildren();
    for (const item of value.items || []) {
      const card = document.createElement("div"); card.className = "research-candidate";
      card.append(textElement("strong", `${item.kind} · ${item.title} · 第 ${item.page} 页`));
      card.append(textElement("p", JSON.stringify(item.payload, null, 2)));
      const actions = document.createElement("div"); actions.className = "button-row";
      const preview = document.createElement("button"); preview.textContent = "核对原页";
      preview.addEventListener("click", () => void openResearchPage(item.sha256, item.page, item.title)); actions.append(preview);
      if (item.status === "PENDING") for (const [decision, title] of [["APPROVED", "通过"], ["REJECTED", "拒绝"]]) {
        const button = document.createElement("button"); button.textContent = title;
        button.addEventListener("click", async () => {
          const reviewer = $("research-reviewer").value.trim(), note = $("research-review-note").value.trim();
          if (!reviewer || !note) { notice("请填写审核人和审核说明", true); return; }
          try { await api(`/api/admin/research/visual/${item.id}/review`, "POST", { status: decision, reviewer, note }); notice("识图审核已保存"); await loadResearchVisual(); }
          catch (error) { notice(error.message, true); }
        }); actions.append(button);
      }
      if (item.status === "APPROVED") {
        const promote = document.createElement("button"); promote.textContent = "转为知识候选";
        promote.addEventListener("click", async () => {
          try {
            const payload = JSON.parse($("research-manual-payload").value);
            await api(`/api/admin/research/visual/${item.id}/candidate`, "POST", {
              kind: $("research-manual-kind").value, payload });
            notice("已生成待审知识候选，请再核对关系或方法字段"); await loadResearchCandidates();
          } catch (error) { notice(error.message, true); }
        }); actions.append(promote);
      }
      card.append(actions); host.append(card);
    }
    if (!(value.items || []).length) host.textContent = "当前没有该状态的识图观察。";
  } catch (error) { notice(error.message, true); }
}

async function loadResearchVisualQueue() {
  try {
    const status = $("research-visual-queue-status").value;
    const value = await api(`/api/admin/research/visual-queue?status=${status}&limit=100`);
    const host = $("research-visual-queue"); host.replaceChildren();
    for (const item of value.items || []) {
      const row = document.createElement("div"); row.className = "research-job";
      row.append(textElement("span", `${item.title} · 第 ${item.page} 页 · ${item.reasons.join("、")}`));
      const actions = document.createElement("div"); actions.className = "button-row";
      const preview = document.createElement("button"); preview.textContent = "核对原页";
      preview.addEventListener("click", () => void openResearchPage(item.sha256, item.page, item.title)); actions.append(preview);
      if (["PROPOSED", "MODEL_EMPTY"].includes(item.status)) {
        const checked = document.createElement("button"); checked.textContent = "原页已核";
        checked.addEventListener("click", async () => {
          const reviewer = $("research-reviewer").value.trim(), note = $("research-review-note").value.trim();
          if (!reviewer || !note) { notice("请填写审核人和原页核对说明", true); return; }
          try {
            await api(`/api/admin/research/visual-queue/${item.sha256}/${item.page}/review`, "POST",
              { status: "APPROVED", reviewer, note });
            notice("页级视觉核验已保存；单项观察仍须分别审核"); await loadResearchVisualQueue(); await loadResearchStatus();
          } catch (error) { notice(error.message, true); }
        }); actions.append(checked);
      }
      row.append(actions); host.append(row);
    }
    if (!(value.items || []).length) host.textContent = "当前没有此状态的视觉页。";
  } catch (error) { notice(error.message, true); }
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
$("research-refresh").addEventListener("click", () => void loadResearchStatus());
document.querySelectorAll("[data-model-control]").forEach((button) => button.addEventListener("click", async () => {
  const [kind, action] = button.dataset.modelControl.split(":");
  button.disabled = true;
  try {
    await api(`/api/admin/research/models/${kind}/${action}`, "POST", undefined, 45000);
    notice(`${kind === "text" ? "文本" : "视觉"}模型${action === "start" ? "已启动" : "已停止"}`);
  } catch (error) { notice(error.message, true); }
  finally { await loadResearchStatus(); }
}));
for (const action of ["pause", "resume"]) $("research-background-" + action).addEventListener("click", async () => {
  try { await api(`/api/admin/research/background/${action}`, "POST"); notice(action === "pause" ? "当前页结束后暂停" : "全量逐页提取已重新启动"); await loadResearchStatus(); }
  catch (error) { notice(error.message, true); }
});
$("research-load-documents").addEventListener("click", () => void loadResearchDocuments());
$("research-load-errors").addEventListener("click", async () => {
  try {
    const value = await api("/api/admin/research/errors?limit=100");
    $("research-errors").classList.remove("hidden");
    $("research-errors").textContent = (value.items || []).map((item) => `${item.stage} · ${item.path} · ${item.reason}`).join("\n") || "没有已记录的来源失败项";
  } catch (error) { notice(error.message, true); }
});
$("research-load-candidates").addEventListener("click", () => void loadResearchCandidates());
$("research-load-visual").addEventListener("click", () => void loadResearchVisual());
$("research-load-visual-queue").addEventListener("click", () => void loadResearchVisualQueue());
for (const [buttonId, action] of [["research-visual-backfill", "visual-queue"], ["research-visual-batch", "visual-batch"]]) {
  $(buttonId).addEventListener("click", async () => {
    try {
      await api("/api/admin/research/jobs", "POST", { action, maxPages: action === "visual-batch" ? 20 : 1,
        modelUrl: $("research-visual-url").value.trim(), modelName: $("research-visual-model").value.trim(),
        extractionVersion: $("research-visual-version").value.trim() });
      notice(action === "visual-queue" ? "正在盘点关键视觉页" : "本机批量识图已排入后台"); await loadResearchStatus();
    } catch (error) { notice(error.message, true); }
  });
}
$("research-visual-propose").addEventListener("click", async () => {
  if (!selectedResearchPage) { notice("请先选择要识图的原页", true); return; }
  try {
    await api("/api/admin/research/jobs", "POST", { action: "visual", maxPages: 1,
      sha256: selectedResearchPage.sha256, page: selectedResearchPage.page,
      modelUrl: $("research-visual-url").value.trim(), modelName: $("research-visual-model").value.trim(),
      extractionVersion: $("research-visual-version").value.trim() });
    notice("本机识图已开始；完成后读取识图结果"); await loadResearchStatus();
  } catch (error) { notice(error.message, true); }
});
document.querySelectorAll("[data-research-action]").forEach((button) => button.addEventListener("click", async () => {
  const action = button.dataset.researchAction;
  const body = { action, maxPages: Number($("research-max-pages").value) || 500,
    retryProblemPages: $("research-retry-pages").checked,
    modelUrl: $("research-model-url").value.trim() || null,
    modelName: $("research-model-name").value.trim() || null,
    extractionVersion: $("research-model-version").value.trim() || null };
  try { await api("/api/admin/research/jobs", "POST", body); notice("研报作业已启动，处理过程可以断点续跑"); await loadResearchStatus(); }
  catch (error) { notice(error.message, true); }
}));
$("research-confirm-blank").addEventListener("click", async () => {
  if (!selectedResearchPage) return;
  try { await api(`/api/admin/research/pages/${selectedResearchPage.sha256}/${selectedResearchPage.page}/confirm-blank`, "POST"); await openResearchPage(selectedResearchPage.sha256, selectedResearchPage.page); await loadResearchStatus(); }
  catch (error) { notice(error.message, true); }
});
document.querySelectorAll("[data-ocr-resolution]").forEach((button) => button.addEventListener("click", async () => {
  if (!selectedResearchPage) return;
  const reviewer = $("research-reviewer").value.trim(), note = $("research-review-note").value.trim();
  if (!reviewer || !note) { notice("请填写审核人和判断依据", true); return; }
  try {
    await api(`/api/admin/research/pages/${selectedResearchPage.sha256}/${selectedResearchPage.page}/resolve-ocr`, "POST",
      { engine: button.dataset.ocrResolution, reviewer, note });
    notice("已保存人工 OCR 选择");
    await openResearchPage(selectedResearchPage.sha256, selectedResearchPage.page);
    await loadResearchStatus();
  } catch (error) { notice(error.message, true); }
}));
$("research-add-candidate").addEventListener("click", async () => {
  if (!selectedResearchPage) { notice("请先选择一页已提取的原页", true); return; }
  try {
    const payload = JSON.parse($("research-manual-payload").value);
    await api("/api/admin/research/candidates", "POST", { ...selectedResearchPage,
      kind: $("research-manual-kind").value, evidenceText: $("research-manual-evidence").value, payload });
    notice("待审候选已保存"); await loadResearchCandidates();
  } catch (error) { notice(error.message, true); }
});
$("research-rollback").addEventListener("click", async () => {
  const version = $("research-versions").value;
  if (!version) return;
  try { await api("/api/admin/research/rollback", "POST", { version }); notice("已切换研究快照"); await loadResearchStatus(); }
  catch (error) { notice(error.message, true); }
});
document.querySelectorAll("[data-service]").forEach((button) => button.addEventListener("click", async () => {
  const action = button.dataset.service;
  if (action !== "start" && taskItems.some((item) => item.status === "RUNNING") && !confirm("当前有数据任务正在运行。停止或重启会中断该任务，是否继续？")) return;
  button.disabled = true;
  try {
    await api(`/api/admin/service/${action}`, "POST", undefined, 8000);
    notice("服务操作已提交");
    await refresh();
    window.setTimeout(() => { void refresh(); }, 500);
  } catch (error) { notice(error.message, true); await refresh(); }
}));
$("service-log-shortcut").addEventListener("click", () => {
  $("log-source").value = "backend";
  showPage("logs");
});
async function submitOperation(kind) {
  try {
    const result = await api("/api/admin/tasks", "POST", { kind });
    notice(result.created ? "任务已排队" : "该任务已经在排队或运行");
    await loadTasks();
    if (kind === "BACKUP_CREATE") void loadBackups();
  } catch (error) { notice(error.message, true); }
}
document.querySelectorAll("[data-operation]").forEach((button) => button.addEventListener("click", async () => {
  await submitOperation(button.dataset.operation);
}));
document.querySelectorAll("[data-terminal]").forEach((button) => button.addEventListener("click", async () => {
  try { await api(`/api/admin/terminals/${button.dataset.terminal}/${button.dataset.action}`, "POST"); notice("通达信任务已提交；可在任务队列和通达信日志查看进度，完成后仍需核对原始文件与入库质量。"); await refresh(); } catch (error) { notice(error.message, true); }
}));
document.querySelectorAll("[data-theme-choice]").forEach((button) => button.addEventListener("click", () => saveSettings({ theme: button.dataset.themeChoice })));
for (const key of ["setting-auto-start", "setting-auto-update", "setting-background"]) $(key).addEventListener("change", () => saveSettings());
$("setting-startup").addEventListener("change", async () => { try { await api("/api/admin/startup", "PUT", { enabled: $("setting-startup").checked }); notice("系统登录启动设置已保存"); } catch (error) { notice(error.message, true); $("setting-startup").checked = state.startupEnabled; } });
$("refresh-tasks").addEventListener("click", loadTasks);
$("task-filter").addEventListener("change", renderTaskList);
$("refresh-backups").addEventListener("click", loadBackups);
$("load-diagnostics").addEventListener("click", diagnostics);
$("load-market-publication").addEventListener("click", loadMarketPublication);
$("load-internal-data").addEventListener("click", async () => {
  const button = $("load-internal-data"); button.disabled = true;
  try { const result = await api(`/api/admin/data/${encodeURIComponent($("internal-view").value)}`); $("internal-data-result").textContent = JSON.stringify(result, null, 2); }
  catch (error) { $("internal-data-result").textContent = error.message; }
  finally { button.disabled = false; }
});
$("load-inventory").addEventListener("click", loadInventory);
$("load-manual-gaps").addEventListener("click", loadManualGaps);
$("manual-gap-source").addEventListener("change", loadManualGaps);
$("refresh-tdx-status").addEventListener("click", () => loadTdxStatus(true));
$("load-providers").addEventListener("click", async () => { try { const value = await api("/api/admin/providers"); $("provider-result").textContent = (value.items || []).map((item) => `${item.name} · ${item.status}\n${item.fieldNotes || item.access || ""}`).join("\n\n") || "暂无已登记适配器"; } catch (error) { $("provider-result").textContent = error.message; } });
$("load-coverage").addEventListener("click", async () => { try { const result = await api("/api/admin/coverage"); $("coverage-result").textContent = JSON.stringify(result, null, 2); } catch (error) { $("coverage-result").textContent = error.message; } });
$("log-source").addEventListener("change", () => { logLines = []; renderLogs(); void loadLogs(); });
for (const key of ["log-search", "log-level", "log-compact"]) $(key).addEventListener("input", renderLogs);
$("log-pause").addEventListener("click", () => { logPaused = !logPaused; $("log-pause").textContent = logPaused ? "继续刷新" : "暂停刷新"; if (!logPaused) void loadLogs(); });
$("copy-logs").addEventListener("click", () => copy($("log-output").textContent));
$("copy-client-url").addEventListener("click", () => copy(state?.backendUrl || "http://127.0.0.1:8765/"));
$("open-client").addEventListener("click", async (event) => {
  event.preventDefault();
  // Open synchronously so browser popup protection does not block the tab.
  const opened = window.open("about:blank", "_blank");
  if (opened) opened.opener = null;
  try {
    const value = await api("/api/admin/open-product", "POST", {});
    const url = value.url + "#ml-bootstrap=" + encodeURIComponent(value.bootstrap);
    if (opened) opened.location.replace(url);
    else notice("浏览器拦截了新标签页，请允许本机管理台打开页面后重试。", true);
  } catch (error) { if (opened) opened.close(); notice(error.message, true); }
});
$("copy-project-path").addEventListener("click", () => copy(state?.projectPath || ""));
$("copy-admin-url").addEventListener("click", () => copy(location.origin + "/"));
$("shutdown-console").addEventListener("click", async () => {
  if (!confirm("关闭管理服务会停止它管理的客户服务。确认关闭？")) return;
  try { await api("/api/admin/shutdown", "POST"); closing = true; notice("管理服务正在关闭。下次可直接打开本机管理地址。"); document.querySelectorAll("button").forEach((button) => { button.disabled = true; }); }
  catch (error) { notice(error.message, true); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(); });
document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "f") { event.preventDefault(); showPage("logs"); $("log-search").focus(); } });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state?.settings.theme === "system") applyTheme("system"); });
showPage(pendingBootstrap !== null ? "overview" : initialPageHash);
async function pollServiceState() {
  if (closing) return;
  await refresh();
  const interval = document.hidden ? 2000 : state?.action ? 500 : 2000;
  window.setTimeout(() => { void pollServiceState(); }, interval);
}
void pollServiceState();
