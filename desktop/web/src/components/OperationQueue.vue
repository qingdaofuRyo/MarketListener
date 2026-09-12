<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, apiPost, formatOperation, formatStatus, formatTime, invalidateQuery } from "../domain/api";
interface Operation { operation_id: string; kind: string; status: string; created_at: string; detail?: string }
const items = ref<Operation[]>([]);
const loading = ref(false);
const error = ref("");
const cancelling = ref("");
let disposed = false;
async function load() {
  if (loading.value) return;
  loading.value = true; error.value = "";
  try {
    const data = await apiGet<{items: Operation[]}>("/api/operations", undefined, {force: true});
    if (!disposed) items.value = data.items;
  } catch { if (!disposed) error.value = "任务队列加载失败，请重试"; }
  finally { loading.value = false; }
}
async function cancel(id: string) {
  cancelling.value = id;
  try {
    await apiPost(`/api/operations/${encodeURIComponent(id)}/cancel`, {});
    invalidateQuery("/api/operations");
    if (!disposed) await load();
  } catch { if (!disposed) error.value = "取消失败，请刷新确认任务状态"; }
  finally { cancelling.value = ""; }
}
onMounted(() => void load());
onBeforeUnmount(() => { disposed = true; });
</script>
<template>
  <section class="panel" aria-label="任务队列">
    <div class="panel-title"><h2>任务队列</h2><el-button :loading="loading" @click="load">刷新任务</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="items" max-height="360" empty-text="暂无任务">
      <el-table-column label="操作" min-width="160"><template #default="{row}">{{ formatOperation(row.kind) }}</template></el-table-column>
      <el-table-column label="状态" width="130"><template #default="{row}">{{ formatStatus(row.status) }}</template></el-table-column>
      <el-table-column label="创建时间" min-width="180"><template #default="{row}">{{ formatTime(row.created_at) }}</template></el-table-column>
      <el-table-column prop="detail" label="结果" min-width="200" show-overflow-tooltip />
      <el-table-column label="操作" width="90"><template #default="{row}"><el-button v-if="row.status === 'QUEUED'" :disabled="loading || !!cancelling" @click="cancel(row.operation_id)">取消</el-button></template></el-table-column>
    </el-table>
  </section>
</template>
