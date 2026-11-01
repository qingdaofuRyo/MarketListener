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
const confirmDelete = ref(false);
const error = ref("");
const notice = ref("");

const allSelected = computed(() => items.value.length > 0 && selected.value.length === items.value.length);
const selectedEntries = computed(() => items.value.filter((entry) => selected.value.includes(entry.instrumentId)));
const selectedDrawingCount = computed(() => selectedEntries.value.reduce((sum, entry) => sum + entry.count, 0));

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    items.value = (await apiGet<{ items: DrawingEntry[] }>("/api/market/drawings/index", undefined, { ttlMs: 5_000, persist: false, force: true })).items ?? [];
    selected.value = selected.value.filter((id) => items.value.some((entry) => entry.instrumentId === id));
  } catch (reason) {
    error.value = `${reason instanceof Error ? reason.message : "画线列表加载失败"}；已保留当前列表`;
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
    const result = await apiDelete<{ deleted: number }>("/api/market/drawings", { instrumentIds: targets });
    notice.value = `已删除 ${result.deleted} 个标的的画线`;
    confirmDelete.value = false;
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
    <div class="page-head"><span class="eyebrow">WORKSPACE</span><h1 class="page-title">设置</h1><p>管理保存在本机的工作区资料。</p></div>
    <section class="settings-section">
      <header>
        <div><h2>画线管理</h2><span>按标的管理本机画线。删除前会显示影响范围。</span></div>
        <strong class="drawing-total">{{ items.length }} 个标的</strong>
      </header>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
      <div class="settings-actions">
        <el-button :disabled="loading || deleting" @click="load">刷新列表</el-button>
        <span class="selection-summary" role="status">已选 {{ selected.length }} 个标的 · {{ selectedDrawingCount }} 条画线</span>
        <el-button type="danger" :disabled="!selected.length || deleting" @click="confirmDelete = true">删除所选画线</el-button>
        <span v-if="notice" class="settings-notice" role="status">{{ notice }}</span>
      </div>
      <div class="drawing-table" :class="{ refreshing: loading && items.length }" :aria-busy="loading">
        <div v-if="loading && !items.length" class="drawing-skeleton" aria-hidden="true"><i v-for="index in 5" :key="index" /></div>
        <span v-if="loading && !items.length" class="sr-only" role="status">正在读取已保存画线</span>
        <div v-if="items.length" class="drawing-scroll">
          <div class="drawing-row drawing-head">
            <el-checkbox :model-value="allSelected" :disabled="!items.length" @change="toggleAll(Boolean($event))">全选</el-checkbox>
            <span>标的</span><span class="drawing-count">画线数量</span><time>更新时间</time>
          </div>
          <div v-for="entry in items" :key="entry.instrumentId" class="drawing-row" :class="{ selected: selected.includes(entry.instrumentId) }">
            <el-checkbox :model-value="selected.includes(entry.instrumentId)" :aria-label="`选择${entry.name || entry.symbol || entry.instrumentId}的画线`" @change="toggleOne(entry.instrumentId, Boolean($event))" />
            <div class="drawing-instrument"><b>{{ entry.symbol || entry.instrumentId }}</b><span>{{ entry.name || "—" }}</span></div>
            <span class="drawing-count">{{ entry.count }}</span>
            <time>{{ entry.updatedAt ? formatTime(entry.updatedAt) : "—" }}</time>
          </div>
        </div>
        <div v-else-if="!loading" class="drawing-empty">
          <strong>尚无已保存画线</strong>
          <p>在行情图表中创建画线后，可在这里按标的集中管理。</p>
          <router-link to="/market/all/">前往全部行情</router-link>
        </div>
      </div>
    </section>
    <el-dialog v-model="confirmDelete" title="确认删除画线" width="min(480px, 92vw)" :close-on-click-modal="!deleting" :close-on-press-escape="!deleting" :show-close="!deleting">
      <p class="delete-explanation">将删除以下 {{ selectedEntries.length }} 个标的保存的 {{ selectedDrawingCount }} 条本机画线：</p>
      <ul class="delete-list"><li v-for="entry in selectedEntries" :key="entry.instrumentId">{{ entry.name || entry.symbol || entry.instrumentId }} <span>{{ entry.count }} 条</span></li></ul>
      <template #footer><el-button :disabled="deleting" @click="confirmDelete = false">取消</el-button><el-button type="danger" :loading="deleting" @click="removeSelected">确认删除</el-button></template>
    </el-dialog>
  </main>
</template>

<style scoped>
.settings-page{width:min(100%,1400px);margin:0 auto;padding:0 4px 28px}
.page-head{padding:3px 0 18px;border-bottom:1px solid var(--ml-divider)}
.page-head .eyebrow{color:var(--ml-accent);font-size:10px;font-weight:700;letter-spacing:.12em}
.page-head h1{margin:5px 0 3px;font-size:clamp(22px,2vw,30px)}
.page-head p{margin:0;color:var(--ml-text-secondary);font-size:12px}
.settings-section{margin-top:20px;padding:20px;border:1px solid var(--ml-divider);border-radius:var(--ml-radius-panel);background:var(--ml-surface)}
.settings-section header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:18px}
.settings-section h2{margin:0 0 4px;font-size:18px}
.settings-section header span{color:var(--ml-text-secondary);font-size:12px}
.drawing-total{padding:4px 9px;border:1px solid var(--ml-divider);border-radius:999px;background:var(--ml-surface-elevated);color:var(--ml-text-secondary);font-size:11px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.settings-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0 0 14px}
.selection-summary{margin-right:auto;color:var(--ml-text-secondary);font-size:12px;font-variant-numeric:tabular-nums}
.settings-notice{flex-basis:100%;color:var(--ml-price-down);font-size:12px}
.drawing-table{border:1px solid var(--ml-divider);border-radius:8px;overflow:hidden;background:var(--ml-surface)}
.drawing-table.refreshing{border-color:var(--ml-accent)}
.drawing-scroll{max-height:min(62vh,660px);overflow:auto}
.drawing-row{display:grid;grid-template-columns:94px minmax(0,1fr) 100px 180px;align-items:center;gap:10px;min-width:560px;padding:9px 12px;border-bottom:1px solid var(--ml-divider);color:var(--ml-text-secondary);font-size:12px;transition:background-color var(--ml-motion-fast)}
.drawing-row:last-child{border-bottom:0}
.drawing-row:not(.drawing-head):hover{background:var(--ml-surface-elevated)}
.drawing-row.selected{background:var(--ml-surface-selected);box-shadow:inset 3px 0 var(--ml-accent)}
.drawing-head{position:sticky;top:0;z-index:1;background:var(--ml-surface-elevated);font-weight:700}
.drawing-instrument{display:flex;align-items:baseline;gap:8px;min-width:0}
.drawing-instrument b{overflow:hidden;color:var(--ml-text-primary);font-family:ui-monospace,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}
.drawing-instrument span{overflow:hidden;color:var(--ml-text-secondary);text-overflow:ellipsis;white-space:nowrap}
.drawing-count{text-align:right;font-variant-numeric:tabular-nums}
.drawing-row time{font-variant-numeric:tabular-nums}
.drawing-empty{display:grid;justify-items:start;gap:5px;padding:30px;color:var(--ml-text-secondary);font-size:12px}
.drawing-empty strong{color:var(--ml-text-primary);font-size:15px}
.drawing-empty p{margin:0 0 7px}
.drawing-empty a{color:var(--ml-accent);text-decoration:none;font-weight:600}
.drawing-empty a:hover{text-decoration:underline;text-underline-offset:3px}
.drawing-skeleton{display:grid;gap:10px;padding:16px}
.drawing-skeleton i{height:34px;border-radius:5px;background:linear-gradient(100deg,var(--ml-surface-elevated),var(--ml-surface-selected),var(--ml-surface-elevated));background-size:200% 100%;animation:drawing-shimmer 1.5s ease-in-out infinite}
.drawing-skeleton i:nth-child(even){width:84%}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
.delete-explanation{margin:0 0 10px;color:var(--ml-text-primary);font-size:13px}
.delete-list{max-height:220px;overflow:auto;margin:0;padding:0;list-style:none;border:1px solid var(--ml-divider);border-radius:7px}
.delete-list li{display:flex;justify-content:space-between;gap:14px;padding:8px 11px;border-bottom:1px solid var(--ml-divider);font-size:12px}
.delete-list li:last-child{border-bottom:0}
.delete-list span{color:var(--ml-text-secondary);white-space:nowrap;font-variant-numeric:tabular-nums}
@keyframes drawing-shimmer{to{background-position:-200% 0}}
@media(max-width:680px){.settings-section{padding:15px}.settings-actions{gap:8px}.selection-summary{flex-basis:100%;order:2}.settings-actions .el-button[type="danger"]{order:3}.drawing-row{grid-template-columns:76px minmax(0,1fr) 84px;min-width:0}.drawing-row time{display:none}.drawing-instrument{display:grid;gap:0}.settings-section header{align-items:center}}
@media(prefers-reduced-motion:reduce){.drawing-row{transition:none}.drawing-skeleton i{animation:none}}
</style>
