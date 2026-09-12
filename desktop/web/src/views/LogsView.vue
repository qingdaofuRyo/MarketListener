<script setup lang="ts">
import OperationQueue from "../components/OperationQueue.vue";
import { onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, formatCategory, formatOperation, formatStatus, formatTime } from "../domain/api";
interface EventRow { timestamp: string; category: string; status?: string; operation?: string; detail?: string }
const category = ref(""); const rows = ref<EventRow[]>([]); const total = ref(0); const loading = ref(false);
const error = ref("");
let disposed = false;
async function load() {
  if (loading.value) return;
  loading.value = true; error.value = "";
  try {
    const data = await apiGet<{items: EventRow[]; total: number}>("/api/logs", {page_size: 500, category: category.value || undefined}, {force: true});
    if (!disposed) { rows.value = data.items; total.value = data.total; }
  } catch { if (!disposed) error.value = "事件日志加载失败，请重试"; }
  finally { loading.value = false; }
}
onBeforeUnmount(() => { disposed = true; });
onMounted(() => void load());
</script>
<template><section><h1 class="page-title">日志</h1><p class="page-note">查看任务进度与本机事件记录。事件日志用于追溯处理过程，不替代业务数据。</p><OperationQueue /><el-alert v-if="error" :title="error" type="error" :closable="false" /><section class="panel data-controls"><el-select v-model="category" clearable placeholder="全部类别"><el-option v-for="item in ['Operation','Market','F10','Report','Industry','Android','Provider','Quality']" :key="item" :label="formatCategory(item)" :value="item" /></el-select><el-button type="primary" :loading="loading" @click="load">筛选</el-button><span class="muted">{{ total }} 条，最多预览 500 条</span></section><section class="panel"><el-table :data="rows" v-loading="loading" max-height="600"><el-table-column label="时间" min-width="190"><template #default="scope">{{ formatTime(scope.row.timestamp) }}</template></el-table-column><el-table-column label="类别" width="150"><template #default="scope">{{ formatCategory(scope.row.category) }}</template></el-table-column><el-table-column label="操作" min-width="160"><template #default="scope">{{ formatOperation(scope.row.operation) }}</template></el-table-column><el-table-column label="状态" width="150"><template #default="scope">{{ formatStatus(scope.row.status) }}</template></el-table-column><el-table-column prop="detail" label="详情" min-width="260" show-overflow-tooltip /></el-table></section></section></template>
