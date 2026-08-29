<script setup lang="ts">
import { onMounted, ref } from "vue";
import FuturesLongShortHeat from "../components/futures/FuturesLongShortHeat.vue";
import { apiGet } from "../domain/api";
import type { LongShortHeatResponse } from "../domain/futures";

const payload = ref<LongShortHeatResponse>();
const loading = ref(false);
const error = ref("");

async function load(force = false): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await apiGet<LongShortHeatResponse>("/api/futures/heat", undefined, { force, ttlMs: 60_000 });
  } catch (reason) {
    payload.value = undefined;
    error.value = reason instanceof Error ? reason.message : "多空热度加载失败";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <main class="futures-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">国内期货数据</h1>
        <p class="page-note">中国商品期货的市场广度、资金方向与结构数据；默认排除金融期货。</p>
      </div>
      <el-button :loading="loading" data-test="futures-refresh" @click="void load(true)">刷新</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
    <section v-if="loading && !payload" class="panel loading-panel" aria-label="正在加载多空热度">
      <el-skeleton :rows="7" animated />
    </section>
    <FuturesLongShortHeat v-else-if="payload?.available" :payload="payload" />
    <section v-else-if="payload && !payload.available" class="panel empty-state" data-test="futures-heat-empty">
      <h2>多空热度暂无可用数据</h2>
      <p>系统不会用演示序列替代真实期货行情，请先完成数据覆盖与 Gold 派生计算。</p>
    </section>

    <footer v-if="payload" class="futures-footnote">
      <span v-if="payload.generatedAt">生成时间：{{ payload.generatedAt }}</span>
      <span v-if="payload.source">来源：{{ payload.source }}</span>
      <span v-if="payload.formulaVersion">公式版本：{{ payload.formulaVersion }}</span>
      <ul v-if="payload.limitations?.length">
        <li v-for="item in payload.limitations" :key="item">{{ item }}</li>
      </ul>
    </footer>
  </main>
</template>

<style scoped>
.futures-page { max-width: 1560px; margin: 0 auto; }
.loading-panel { min-height: 520px; }
.empty-state { padding-block: 54px; text-align: center; }
.empty-state p { margin: 8px 0 0; color: var(--ml-text-secondary); font-size: 13px; }
.futures-footnote {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin: 12px 4px 0;
  color: var(--ml-text-disabled);
  font-size: 11px;
}
.futures-footnote ul { flex-basis: 100%; margin: 0; padding-left: 18px; }
</style>
