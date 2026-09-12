<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, apiPost, invalidateQuery } from "../domain/api";
import { formatBytes } from "../domain/sourceDashboard";

interface Health { stats?: { run_count?: number; partition_count?: number; quarantine_count?: number; storage_bytes?: number } }

const health = ref<Health>({});
const busy = ref<string | null>(null);
const error = ref("");
let disposed = false;
const operationButtons = [
  ["MARKET_UPDATE", "更新行情"], ["F10_UPDATE_CN", "更新 A 股 F10"], ["F10_UPDATE_HK", "更新港股 F10"],
  ["REVENUE_UPDATE", "更新收入构成"], ["REPORT_PROCESS", "处理研报"], ["REPORT_VERIFY", "校验研报"],
  ["CHAIN_REBUILD", "重建产业链"], ["ATLAS_BUILD", "构建 Atlas"], ["ANDROID_PACKAGE_BUILD", "构建 Android 同步包"],
  ["STATUS_REFRESH", "刷新状态"],
] as const;

async function refresh() {
  try {
    const result = await apiGet<Health>("/api/health", undefined, { force: true });
    if (!disposed) health.value = result;
  } catch { if (!disposed) error.value = "仪表盘状态加载失败"; }
}

async function submit(kind: string) {
  busy.value = kind;
  error.value = "";
  try {
    await apiPost("/api/operations", { kind });
    invalidateQuery("/api/operations"); invalidateQuery("/api/health");
    if (!disposed) await refresh();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "操作创建失败";
  } finally {
    busy.value = null;
  }
}

onMounted(() => { void refresh(); });
onBeforeUnmount(() => { disposed = true; });
</script>

<template>
  <section>
    <h1 class="page-title">仪表盘</h1>
    <p class="page-note">查看本地运行状态并提交数据处理任务；任务由系统依次执行，进度请到日志页查看。</p>
    <el-alert v-if="error" type="error" :title="error" :closable="false" class="page-alert" />
    <section class="home-stats">
      <div class="metric"><span>运行记录</span><strong>{{ health.stats?.run_count ?? "—" }}</strong></div>
      <div class="metric"><span>数据分区</span><strong>{{ health.stats?.partition_count ?? "—" }}</strong></div>
      <div class="metric"><span>隔离问题</span><strong>{{ health.stats?.quarantine_count ?? "—" }}</strong></div>
      <div class="metric"><span>本地存储容量</span><strong>{{ formatBytes(health.stats?.storage_bytes) }}</strong></div>
    </section>
    <section class="panel"><h2>受控操作</h2><div class="operation-buttons"><el-button v-for="[kind,label] in operationButtons" :key="kind" :loading="busy === kind" @click="submit(kind)">{{ label }}</el-button></div></section>
    <section class="panel"><h2>运行与数据</h2><p>任务提交后，请前往 <router-link to="/logs/">日志与任务队列</router-link> 查看进度。</p><router-link to="/data-sources/">查看本地数据容量、来源与更新时间</router-link></section>
  </section>
</template>
