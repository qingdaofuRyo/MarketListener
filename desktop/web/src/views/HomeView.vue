<script setup lang="ts">
import { ElMessageBox } from "element-plus";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, apiPost, formatNumber, formatOperation, formatPeriod, formatStatus, formatTime, invalidateQuery } from "../domain/api";
import { formatBytes } from "../domain/sourceDashboard";

interface Health { stats?: { run_count?: number; partition_count?: number; quarantine_count?: number; storage_bytes?: number } }
interface TdxCoverageGroup {
  terminal: string;
  period: string;
  files: number;
  commonStartDate?: string | null;
  commonStartFiles?: number | null;
  commonLatestDate?: string | null;
  commonLatestFiles?: number | null;
  continuousFiles?: number | null;
  gapFiles?: number | null;
  unreadableFiles?: number | null;
  writingFiles?: number | null;
  unknownCalendarFiles?: number | null;
}
interface TdxExtendedGroup {
  prefix: string;
  period: string;
  files: number;
  withRecords: number;
  commonStartDate?: string | null;
  commonStartFiles: number;
  commonLatestDate?: string | null;
  commonLatestFiles: number;
}
interface TdxCoverage {
  available: boolean;
  generatedAt?: string | null;
  summary?: { files: number; gapFiles: number; unreadableFiles: number; writingFiles?: number };
  groups?: TdxCoverageGroup[];
  extendedGroups?: TdxExtendedGroup[];
}
interface Operation {
  operation_id: string;
  kind: string;
  status: string;
  created_at?: string;
  started_at?: string | null;
  detail?: string | null;
}
interface SubmittedOperation { created: boolean; operation: Operation }

const health = ref<Health>({});
const busy = ref<string | null>(null);
const error = ref("");
const coverage = ref<TdxCoverage | null>(null);
const coverageLoading = ref(false);
const coverageError = ref("");
const submittedOperation = ref<Operation | null>(null);
const submittedCreated = ref<boolean | null>(null);
let disposed = false;
let operationPoller: number | null = null;
let operationPollBusy = false;
let elapsedTimer: number | null = null;
const currentTime = ref(Date.now());
const elapsedMinutes = computed(() => {
  const start = submittedOperation.value?.started_at;
  if (!start || submittedOperation.value?.status !== "RUNNING") return null;
  const elapsed = currentTime.value - Date.parse(start);
  return Number.isFinite(elapsed) ? Math.max(0, Math.floor(elapsed / 60_000)) : null;
});
const operationButtons = [
  ["MARKET_UPDATE", "更新行情"], ["F10_UPDATE_CN", "更新 A 股 F10"], ["F10_UPDATE_HK", "更新港股 F10"],
  ["REVENUE_UPDATE", "更新收入构成"], ["REPORT_PROCESS", "处理研报"], ["REPORT_VERIFY", "校验研报"],
  ["CHAIN_REBUILD", "重建产业链"], ["ATLAS_BUILD", "构建 Atlas"], ["ANDROID_PACKAGE_BUILD", "构建 Android 同步包"],
  ["STATUS_REFRESH", "刷新状态"],
] as const;
const tdxOperationButtons = [
  ["TDX_COVERAGE_CHECK", "检查通达信盘后数据"],
  ["TDX_AFTERHOURS_DOWNLOAD", "下载通达信盘后数据"],
] as const;

function requestFailure(reason: unknown): string {
  if (reason instanceof TypeError && /fetch|network/i.test(reason.message)) {
    return `无法连接本机服务 ${window.location.origin}；请启动后端服务后重试。`;
  }
  return reason instanceof Error ? reason.message : "请求失败，请稍后重试。";
}

async function refresh() {
  try {
    const result = await apiGet<Health>("/api/health", undefined, { force: true });
    if (!disposed) health.value = result;
  } catch (reason) { if (!disposed) error.value = `仪表盘状态加载失败：${requestFailure(reason)}`; }
}

async function refreshCoverage() {
  coverageLoading.value = true;
  coverageError.value = "";
  try {
    const result = await apiGet<TdxCoverage>("/api/tdx-afterhours/coverage", undefined, { force: true });
    if (!disposed) coverage.value = result;
  } catch (reason) {
    if (!disposed) {
      coverageError.value = `通达信盘后数据检查结果加载失败：${requestFailure(reason)} 现有结果可能已过期。`;
    }
  } finally {
    if (!disposed) coverageLoading.value = false;
  }
}

function formatTdxTerminal(value?: string | null): string {
  if (value?.toLowerCase() === "financial") return "沪深京所有AB股类品种";
  if (value?.toLowerCase() === "futures") return "所有国内期货品种";
  return value || "暂无数据";
}

const extendedPrefixLabels: Record<string, string> = {
  "10": "基本汇率", "12": "国际指数", "16": "COMEX 期货", "17": "NYMEX 期货", "18": "CBOT 期货",
  "27": "港股指数", "31": "港股", "38": "宏观", "48": "港股扩展", "62": "中证指数",
  "69": "华证指数", "102": "国证指数",
};

function formatExtendedPrefix(prefix: string): string {
  return `${prefix}# ${extendedPrefixLabels[prefix] ?? "扩展行情"}`;
}

function formatCoverageRange(group: TdxCoverageGroup): string {
  return `${formatCoverageAt(group.commonStartDate)}（${formatNumber(group.commonStartFiles)}个）至 ${formatCoverageAt(group.commonLatestDate)}（${formatNumber(group.commonLatestFiles)}个）`;
}

function formatCoverageAt(value?: string | null): string {
  if (value && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  return formatTime(value);
}

function formatCoverageResult(group: TdxCoverageGroup): string {
  const parts = [
    `连续 ${formatNumber(group.continuousFiles)} 个`,
    `缺口 ${formatNumber(group.gapFiles)} 个`,
    `不可读 ${formatNumber(group.unreadableFiles)} 个`,
  ];
  if (typeof group.writingFiles === "number" && group.writingFiles > 0) {
    parts.push(`写入中 ${formatNumber(group.writingFiles)} 个`);
  }
  if (typeof group.unknownCalendarFiles === "number" && group.unknownCalendarFiles > 0) {
    parts.push(`日历未知 ${formatNumber(group.unknownCalendarFiles)} 个`);
  }
  return parts.join(" · ");
}

function operationDetail(operation: Operation): string {
  const detail = operation.detail || "";
  if (operation.kind === "MARKET_UPDATE" && detail.startsWith("{")) {
    try {
      const result = JSON.parse(detail) as { task_count?: number; passed?: number; partial_failure?: number; failed?: number };
      return `共 ${result.task_count ?? 0} 项：通过 ${result.passed ?? 0}，部分失败 ${result.partial_failure ?? 0}，失败 ${result.failed ?? 0}。`;
    } catch { /* Keep the service's short detail below. */ }
  }
  if (operation.kind === "TDX_AFTERHOURS_DOWNLOAD") {
    return detail
      .replaceAll("OPEN_DOWNLOAD_DIALOG_MANUALLY", "请先在金融终端打开“选项 → 盘后数据下载”窗口")
      .replaceAll("WAITING_FOR_LOGIN_OR_MAIN_WINDOW", "请确认期货通已登录并显示主窗口")
      .replaceAll("WAITING_FOR_LOGIN", "请先登录通达信终端")
      .replaceAll("EXISTING_DOWNLOAD_BUSY", "终端已有人工下载，未接管")
      .replaceAll("DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN", "下载超时，结果待核对");
  }
  return detail.length > 500 ? `${detail.slice(0, 500)}…` : detail;
}

function operationStatusLabel(operation: Operation): string {
  if (operation.kind === "TDX_COVERAGE_CHECK" && operation.status === "PARTIAL_FAILURE") {
    return "检查完成，存在缺口或待核验项";
  }
  return formatStatus(operation.status);
}

function stopOperationPolling(): void {
  if (operationPoller !== null) window.clearInterval(operationPoller);
  operationPoller = null;
}

function startOperationPolling(): void {
  stopOperationPolling();
  operationPoller = window.setInterval(() => { void pollSubmittedOperation(); }, 3000);
}

async function restoreRecentOperation(): Promise<void> {
  try {
    const response = await apiGet<{ items: Operation[] }>("/api/operations", undefined, { force: true });
    if (disposed || submittedOperation.value) return;
    const recentCutoff = Date.now() - 24 * 60 * 60_000;
    const recent = response.items.find(item =>
      [
        "MARKET_UPDATE", "TDX_COVERAGE_CHECK", "TDX_AFTERHOURS_DOWNLOAD", "TDX_LOCAL_IMPORT",
        "FUTURES_HEAT_UPDATE", "FUTURES_OFFICIAL_DAILY_UPDATE", "FUTURES_STRUCTURE_UPDATE",
      ].includes(item.kind)
      && (["QUEUED", "RUNNING"].includes(item.status)
        || (item.created_at && Date.parse(item.created_at) >= recentCutoff)),
    );
    if (!recent) return;
    submittedOperation.value = recent;
    submittedCreated.value = null;
    if (["QUEUED", "RUNNING"].includes(recent.status)) startOperationPolling();
  } catch { /* Health and coverage remain independently available. */ }
}

async function pollSubmittedOperation(): Promise<void> {
  const id = submittedOperation.value?.operation_id;
  if (!id || operationPollBusy || disposed) return;
  operationPollBusy = true;
  try {
    const response = await apiGet<{ items: Operation[] }>("/api/operations", undefined, { force: true });
    const current = response.items.find(item => item.operation_id === id);
    if (!current || disposed || submittedOperation.value?.operation_id !== id) return;
    submittedOperation.value = current;
    if (!["QUEUED", "RUNNING"].includes(current.status)) {
      stopOperationPolling();
      void refresh();
      if (current.kind === "TDX_COVERAGE_CHECK") void refreshCoverage();
      if (["FUTURES_HEAT_UPDATE", "TDX_LOCAL_IMPORT"].includes(current.kind)) {
        invalidateQuery("/api/futures/contract-heat");
      }
    }
  } catch {
    // The link to the persistent task queue remains available for recovery.
  } finally {
    operationPollBusy = false;
  }
}

async function submit(kind: string) {
  if (kind === "TDX_AFTERHOURS_DOWNLOAD") {
    try {
      await ElMessageBox.confirm(
        "将在当前 Windows 桌面尝试执行两款通达信终端已保存的六批下载。当前金融终端版本请先打开“选项 → 盘后数据下载”窗口，期货通请先完成登录；已有人工下载不会被接管。界面完成后仍需检查文件覆盖。",
        "确认下载通达信盘后数据",
        { confirmButtonText: "创建下载任务", cancelButtonText: "取消", type: "warning" },
      );
    } catch {
      return;
    }
  }
  busy.value = kind;
  error.value = "";
  try {
    const result = await apiPost<SubmittedOperation>("/api/operations", { kind });
    submittedOperation.value = result.operation;
    submittedCreated.value = result.created;
    startOperationPolling();
    invalidateQuery("/api/operations"); invalidateQuery("/api/health");
    if (kind === "TDX_COVERAGE_CHECK") invalidateQuery("/api/tdx-afterhours/coverage");
  } catch (reason) {
    error.value = `操作创建失败：${requestFailure(reason)}`;
  } finally {
    busy.value = null;
  }
}

onMounted(() => {
  void refresh(); void refreshCoverage(); void restoreRecentOperation();
  elapsedTimer = window.setInterval(() => { currentTime.value = Date.now(); }, 5000);
});
onBeforeUnmount(() => {
  disposed = true;
  stopOperationPolling();
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer);
});
</script>

<template>
  <section>
    <h1 class="page-title">仪表盘</h1>
    <p class="page-note">查看本地运行状态并提交数据处理任务；任务由系统依次执行，进度请到日志页查看。</p>
    <el-alert v-if="error" type="error" :title="error" :closable="false" class="page-alert" />
    <el-alert
      v-if="submittedOperation"
      class="page-alert"
      :type="submittedOperation.status === 'PASS' ? 'success' : submittedOperation.status === 'FAILED' ? 'error' : submittedOperation.status === 'PARTIAL_FAILURE' ? 'warning' : 'info'"
      :closable="false"
      data-test="submitted-operation"
    >
      <template #title>{{ formatOperation(submittedOperation.kind) }}：{{ submittedCreated === true ? '已创建任务' : submittedCreated === false ? '已有任务正在执行' : '最近任务' }} · {{ operationStatusLabel(submittedOperation) }}</template>
      <p v-if="elapsedMinutes !== null">已运行 {{ elapsedMinutes }} 分钟；{{ submittedOperation?.kind === 'FUTURES_HEAT_UPDATE' ? '期货热度更新包含本地文件增量导入与交易所同步。' : '通达信全盘检查首次或文件大量变化时可能持续十余分钟。' }}</p>
      <p v-if="submittedOperation.detail">{{ operationDetail(submittedOperation) }}</p>
      <router-link to="/logs/">查看任务详情</router-link>
    </el-alert>
    <section class="home-stats">
      <div class="metric"><span>运行记录</span><strong>{{ health.stats?.run_count ?? "—" }}</strong></div>
      <div class="metric"><span>数据分区</span><strong>{{ health.stats?.partition_count ?? "—" }}</strong></div>
      <div class="metric"><span>隔离问题</span><strong>{{ health.stats?.quarantine_count ?? "—" }}</strong></div>
      <div class="metric"><span>本地存储容量</span><strong>{{ formatBytes(health.stats?.storage_bytes) }}</strong></div>
    </section>
    <section class="panel"><h2>受控操作</h2><div class="operation-buttons"><el-button v-for="[kind,label] in operationButtons" :key="kind" :loading="busy === kind" @click="submit(kind)">{{ label }}</el-button></div></section>
    <section class="panel">
      <h2>期货月份合约热度</h2>
      <p class="muted">“更新行情”不会重建期货合约热度。此任务会增量导入期货通本地月份合约、尝试同步最新交易日的交易所官方逐合约成交额和规则，再重算热度；官方来源缺失会显示为部分覆盖，不补零。历史成交额未回填的日期仍会保留缺口。</p>
      <el-button :loading="busy === 'FUTURES_HEAT_UPDATE'" @click="submit('FUTURES_HEAT_UPDATE')">更新期货热度</el-button>
    </section>
    <section class="panel" aria-label="通达信盘后数据操作">
      <h2>通达信盘后数据</h2>
      <p class="muted">检查“沪深京所有AB股类品种”和“所有国内期货品种”的本地日线、5分钟线。终端分别维护两个周期的品种列表和下载进度，且并非每类都提供5分钟线；因此文件数可以不同，数量差本身不代表数据损坏。要确认差异来源，需按代码对比两个周期并核对终端下载列表。</p>
      <div class="operation-buttons"><el-button v-for="[kind,label] in tdxOperationButtons" :key="kind" :loading="busy === kind" @click="submit(kind)">{{ label }}</el-button></div>
      <p class="muted">下载将尝试执行已保存的六批任务；金融终端请先打开盘后数据下载窗口。任务排队后请到 <router-link to="/logs/">日志页</router-link> 查看进度。</p>
    </section>
    <section v-loading="coverageLoading" class="panel" aria-label="最近一次通达信盘后数据检查">
      <div class="panel-title"><h2>最近一次通达信盘后数据检查</h2><el-button :loading="coverageLoading" @click="refreshCoverage">刷新检查结果</el-button></div>
      <p v-if="coverage?.generatedAt" class="muted">生成时间：{{ formatTime(coverage.generatedAt) }}</p>
      <p v-if="coverage?.summary?.writingFiles" class="muted">扫描时有 {{ formatNumber(coverage.summary.writingFiles) }} 个文件正在写入，未将其判为文件损坏。</p>
      <el-alert v-if="coverageError" :title="coverageError" type="error" :closable="false" />
      <el-table v-else-if="coverage?.available" :data="coverage.groups ?? []" size="small" max-height="360" empty-text="本次检查未发现可显示的分组">
        <el-table-column label="终端" min-width="150"><template #default="{row}">{{ formatTdxTerminal(row.terminal) }}</template></el-table-column>
        <el-table-column label="周期" width="90"><template #default="{row}">{{ formatPeriod(row.period) }}</template></el-table-column>
        <el-table-column label="文件数" width="100"><template #default="{row}">{{ formatNumber(row.files) }}</template></el-table-column>
        <el-table-column label="覆盖最多文件的起始日 / 最新日" min-width="365"><template #default="{row}">{{ formatCoverageRange(row) }}</template></el-table-column>
        <el-table-column label="连续 / 缺口 / 不可读" min-width="235"><template #default="{row}">{{ formatCoverageResult(row) }}</template></el-table-column>
      </el-table>
      <template v-if="coverage?.available && coverage.extendedGroups?.length">
        <h3>金融终端扩展行情</h3>
      <p class="muted">起始日和最新日分别取组内文件首末记录日期中出现次数最多的日期；括号内是对应文件数。这是最常见日期，不保证超过半数文件，也不表示全组历史连续。</p>
        <el-table :data="coverage.extendedGroups" size="small" max-height="440" empty-text="无扩展行情原始文件">
          <el-table-column label="类别" min-width="145"><template #default="{row}">{{ formatExtendedPrefix(row.prefix) }}</template></el-table-column>
          <el-table-column label="周期" width="85"><template #default="{row}">{{ formatPeriod(row.period) }}</template></el-table-column>
          <el-table-column label="文件数" width="105"><template #default="{row}">{{ formatNumber(row.files) }}</template></el-table-column>
          <el-table-column label="覆盖最多文件的起始日" min-width="200"><template #default="{row}">{{ row.commonStartDate ?? '暂无数据' }}（{{ formatNumber(row.commonStartFiles) }} 个）</template></el-table-column>
          <el-table-column label="覆盖最多文件的最新日" min-width="200"><template #default="{row}">{{ row.commonLatestDate ?? '暂无数据' }}（{{ formatNumber(row.commonLatestFiles) }} 个）</template></el-table-column>
        </el-table>
      </template>
      <p v-if="coverage && !coverage.available && !coverageLoading && !coverageError" class="muted">尚未运行检查。</p>
    </section>
    <section class="panel"><h2>运行与数据</h2><p>任务提交后，请前往 <router-link to="/logs/">日志与任务队列</router-link> 查看进度。</p><router-link to="/data-sources/">查看本地数据容量、来源与更新时间</router-link></section>
  </section>
</template>
