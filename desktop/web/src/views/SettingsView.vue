<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiDelete, apiGet, formatTime } from "../domain/api";

interface DrawingEntry {
  instrumentId: string;
  symbol?: string;
  name?: string;
  count: number;
  updatedAt?: string;
}

const items = ref<DrawingEntry[]>([]);
const selected = ref<string[]>([]);
const loading = ref(false);
const deleting = ref(false);
const error = ref("");
const notice = ref("");

const allSelected = computed(() => items.value.length > 0 && selected.value.length === items.value.length);

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    items.value = (await apiGet<{ items: DrawingEntry[] }>("/api/market/drawings/index", undefined, { ttlMs: 5_000, persist: false, force: true })).items ?? [];
    selected.value = selected.value.filter((id) => items.value.some((entry) => entry.instrumentId === id));
  } catch (reason) {
    items.value = [];
    error.value = reason instanceof Error ? reason.message : "画线列表加载失败";
  } finally {
    loading.value = false;
  }
}

function toggleAll(checked: boolean): void {
  selected.value = checked ? items.value.map((entry) => entry.instrumentId) : [];
}

function toggleOne(instrumentId: string, checked: boolean): void {
  selected.value = checked ? [...selected.value, instrumentId] : selected.value.filter((id) => id !== instrumentId);
}

async function removeSelected(): Promise<void> {
  if (!selected.value.length || deleting.value) return;
  deleting.value = true;
  notice.value = "";
  error.value = "";
  const targets = [...selected.value];
  try {
    await apiDelete<{ deleted: number }>("/api/market/drawings", { instrumentIds: targets });
    notice.value = `已删除 ${targets.length} 个标的的画线`;
    selected.value = [];
    await load();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "删除画线失败";
  } finally {
    deleting.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="settings-page">
    <div class="page-head"><h1 class="page-title">设置</h1></div>
    <section class="settings-section">
      <header>
        <h2>画线管理</h2>
        <span>可选择单个、多个或全部标的，删除其保存在本地的所有画线。</span>
      </header>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
      <div class="settings-actions">
        <el-button :disabled="loading" @click="load">刷新</el-button>
        <el-button type="danger" :disabled="!selected.length" :loading="deleting" @click="removeSelected">删除所选画线</el-button>
        <span v-if="notice" class="settings-notice">{{ notice }}</span>
      </div>
      <div v-loading="loading" class="drawing-table">
        <div class="drawing-row drawing-head">
          <el-checkbox :model-value="allSelected" :disabled="!items.length" @change="toggleAll">全选</el-checkbox>
          <span>标的</span><span class="drawing-count">画线数量</span><time>更新时间</time>
        </div>
        <div v-for="entry in items" :key="entry.instrumentId" class="drawing-row">
          <el-checkbox :model-value="selected.includes(entry.instrumentId)" @change="toggleOne(entry.instrumentId, $event)" />
          <div class="drawing-instrument"><b>{{ entry.symbol || entry.instrumentId }}</b><span>{{ entry.name || "—" }}</span></div>
          <span class="drawing-count">{{ entry.count }}</span>
          <time>{{ entry.updatedAt ? formatTime(entry.updatedAt) : "—" }}</time>
        </div>
        <p v-if="!items.length && !loading" class="muted">暂无已保存的画线。</p>
      </div>
    </section>
  </main>
</template>

<style scoped>
.settings-page{width:96vw;max-width:1400px;margin:0 2vw}
.page-head h1{margin:0;font-size:clamp(22px,2vw,30px)}
.settings-section{margin-top:28px}
.settings-section header{display:flex;align-items:baseline;gap:12px;margin-bottom:14px}
.settings-section h2{margin:0;font-size:20px}
.settings-section header span{color:var(--ml-text-secondary);font-size:12px}
.settings-actions{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.settings-notice{color:var(--ml-success,#22c55e);font-size:12px}
.drawing-table{border:1px solid var(--ml-divider);border-radius:6px;overflow:hidden;background:var(--ml-surface)}
.drawing-row{display:grid;grid-template-columns:150px minmax(0,1fr) 120px 210px;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--ml-divider);color:var(--ml-text-secondary);font-size:12px}
.drawing-row:last-child{border-bottom:0}
.drawing-head{background:var(--ml-surface-elevated);font-weight:700}
.drawing-instrument{display:flex;align-items:baseline;gap:8px;min-width:0}
.drawing-instrument b{overflow:hidden;color:var(--ml-text-primary);font-family:ui-monospace,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}
.drawing-instrument span{overflow:hidden;color:var(--ml-text-secondary);text-overflow:ellipsis;white-space:nowrap}
.drawing-count{text-align:right}
.muted{padding:18px;text-align:center;color:var(--ml-text-disabled);font-size:12px}
</style>
