<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { apiGet, apiPut, invalidateQuery } from "../domain/api";
import SeriesChart, { type NamedSeries } from "../components/charts/SeriesChart.vue";
import HeatmapChart, { type HeatmapCell } from "../components/charts/HeatmapChart.vue";
import RankingChart, { type RankingFrame } from "../components/charts/RankingChart.vue";

interface DashboardDefinition {
  id: string;
  title: string;
  category: string;
  available: boolean;
  description: string;
}

interface DashboardPayload {
  available: boolean;
  id?: string;
  title?: string;
  unit?: string;
  series?: NamedSeries[];
  generatedAt?: string;
  source?: string;
}

interface RankingPayload {
  category: string;
  available: boolean;
  frames: RankingFrame[];
}

interface HeatmapPayload {
  category: string;
  available: boolean;
  x: string[];
  y: string[];
  cells: HeatmapCell[];
}

interface R4SectionMetric { metricKey: string; tradingDate: string; metricName: string; value: number; definition?: string; calculationMethod?: string; coverage?: number; source?: string; unit?: string; }
interface R4SectionPanel { id: string; title: string; status: "PASS" | "PARTIAL" | "UNAVAILABLE"; availableSeries?: number; values?: R4SectionMetric[]; limitations?: string[]; }
interface R4Section { id: string; title: string; panels: R4SectionPanel[]; }
interface MacroCatalogItem { seriesId: string; country: "CN" | "US"; topic: string; name: string; frequency: string; unit: string; source: string; timeBasis: "OBSERVATION_PERIOD" | "SOURCE_DATE"; available: boolean; latestObservationPeriod?: string | null; latestFetchedAt?: string | null; }
interface MacroTimelineObservation { observationPeriod: string; value: number; releasedAt: string | null; fetchedAt: string; }
interface MacroSeasonalObservation { year: string; months: Array<number | null>; }
interface MacroSeriesPayload {
  available: boolean;
  series: MacroCatalogItem;
  view: "timeline" | "seasonal";
  observations: MacroTimelineObservation[] | MacroSeasonalObservation[];
  limitations: string[];
}
interface EquityOverviewPoint {
  tradingDay: string;
  totalMarketCapYi: number | null;
  turnoverYi: number | null;
  coverage?: number;
  turnoverCoverage?: number;
  breadthCoverage?: number;
}
interface EquityOverviewPayload {
  available: boolean;
  market: "CN" | "HK";
  segment: string;
  currency: string;
  points: EquityOverviewPoint[];
  limitations: string[];
}
type EquityListType = "st_warning" | "delisting_warning" | "regulatory" | "suspension";
interface EquityStatusListPayload {
  available: boolean;
  listType: EquityListType;
  listTitle: string;
  asOfDay: string | null;
  total: number | null;
  items: Array<{
    instrumentId: string; symbol: string; name: string; assetType: string; segment: string;
    status: string; effectiveFrom: string | null; expectedEnd: string | null;
    reason: string | null; source: string; capturedAt: string;
  }>;
  limitations: string[];
}

const browserViews = [
  ["market", "Market"],
  ["silver", "Silver"],
  ["gold", "Gold"],
  ["f10", "F10"],
  ["industry", "Industry"],
  ["runs", "Runs"],
  ["partitions", "Partitions"],
  ["quarantine", "Quarantine"],
  ["package", "Package"],
  ["storage", "Storage"],
  ["quality", "Quality"],
  ["freshness", "Freshness"],
] as const;

const definitions = ref<DashboardDefinition[]>([]);
const payloads = ref<Record<string, DashboardPayload>>({});
const expandedPanels = ref<Set<string>>(new Set());
interface PersonalPanel {
  id: string;
  title: string;
  metricId: string;
  chartType: "line" | "bar";
  color: string;
  opacity: number;
  rangeDays: number;
  width: "half" | "full";
  hidden: boolean;
}
const personalPanels = ref<PersonalPanel[]>([]);
const newPanelMetric = ref("market-breadth");
const loading = ref(false);
const error = ref("");
const categoryFilter = ref("");

const rankingCategory = ref("futures");
const ranking = ref<RankingPayload>({ category: "futures", available: false, frames: [] });
const heatmapCategory = ref("breadth");
const heatmap = ref<HeatmapPayload>({ category: "breadth", available: false, x: [], y: [], cells: [] });
const r4Sections = ref<R4Section[]>([]);
const r4Section = ref("cn-equities");
const route = useRoute();
const router = useRouter();
const macroCountry = ref<"CN" | "US">("CN");
const macroCatalog = ref<MacroCatalogItem[]>([]);
const selectedMacroId = ref("");
const macroView = ref<"timeline" | "seasonal">("timeline");
const macroSeries = ref<MacroSeriesPayload>();
const macroLoading = ref(false);
const equityOverview = ref<EquityOverviewPayload>();
const equityOverviewLoading = ref(false);
const equityListType = ref<EquityListType>("st_warning");
const equityListAsOfDay = ref<string | undefined>();
const equityListPage = ref(1);
const equityListPageSize = 50;
const equityStatusList = ref<EquityStatusListPayload>();
const equityStatusListLoading = ref(false);
const equityListTypeOptions: Array<{ value: EquityListType; label: string }> = [
  { value: "st_warning", label: "ST 风险警示" },
  { value: "delisting_warning", label: "退市风险警示" },
  { value: "regulatory", label: "监管期" },
  { value: "suspension", label: "停牌期" },
];

const categories = computed(() => [...new Set(definitions.value.map((item) => item.category))].sort());
const availablePanels = computed(() =>
  definitions.value.filter(
    (item) => item.available && (!categoryFilter.value || item.category === categoryFilter.value),
  ),
);

async function loadLayout(): Promise<void> {
  try {
    const payload = await apiGet<{ panels: Partial<PersonalPanel>[] }>("/api/personal/dashboard", undefined, { ttlMs: 5 * 60_000, persist: true });
    personalPanels.value = payload.panels
      .filter((panel): panel is PersonalPanel & { id: string; title: string; metricId: string } => Boolean(panel.id && panel.title && panel.metricId))
      .map((panel) => ({
        id: panel.id,
        title: panel.title,
        metricId: panel.metricId,
        chartType: panel.chartType === "bar" ? "bar" : "line",
        color: /^#[0-9a-fA-F]{6}$/.test(panel.color ?? "") ? panel.color! : "#d64b4b",
        opacity: typeof panel.opacity === "number" ? Math.min(1, Math.max(0, panel.opacity)) : 0.16,
        rangeDays: typeof panel.rangeDays === "number" ? Math.max(0, panel.rangeDays) : 0,
        width: panel.width === "full" ? "full" : "half",
        hidden: Boolean(panel.hidden),
      }));
  } catch { personalPanels.value = []; }
}
async function saveLayout(): Promise<void> { await apiPut("/api/personal/dashboard", { panels: personalPanels.value }); invalidateQuery("/api/personal/dashboard"); }
async function addPanel(): Promise<void> {
  const definition = definitions.value.find(item => item.id === newPanelMetric.value); if (!definition) return;
  personalPanels.value.push({ id: `panel-${Date.now()}`, title: definition.title, metricId: definition.id, chartType: "line", color: "#d64b4b", opacity: 0.16, rangeDays: 0, width: "half", hidden: false }); await saveLayout();
}
async function removePanel(id: string): Promise<void> { personalPanels.value = personalPanels.value.filter(panel => panel.id !== id); await saveLayout(); }
async function togglePanelHidden(panel: PersonalPanel): Promise<void> { panel.hidden = !panel.hidden; await saveLayout(); }
async function movePanel(id: string, offset: number): Promise<void> {
  const index = personalPanels.value.findIndex(panel => panel.id === id);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= personalPanels.value.length) return;
  const [panel] = personalPanels.value.splice(index, 1);
  personalPanels.value.splice(target, 0, panel);
  await saveLayout();
}

async function loadDefinitions(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const data = await apiGet<{ items: DashboardDefinition[] }>("/api/dashboard/definitions", undefined, { ttlMs: 5 * 60_000, persist: true });
    definitions.value = data.items;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "数据面板加载失败";
  } finally {
    loading.value = false;
  }
}

async function loadPanel(id: string): Promise<void> {
  expandedPanels.value.add(id); expandedPanels.value = new Set(expandedPanels.value);
  if (payloads.value[id]) return;
  try { payloads.value[id] = await apiGet<DashboardPayload>(`/api/dashboard/${encodeURIComponent(id)}`, undefined, { ttlMs: 10 * 60_000, persist: true }); }
  catch { payloads.value[id] = { available: false }; }
}

async function loadRanking(): Promise<void> {
  try {
    ranking.value = await apiGet<RankingPayload>("/api/metrics/ranking", { category: rankingCategory.value, limit: 20 }, { ttlMs: 5 * 60_000, persist: true });
  } catch {
    ranking.value = { category: rankingCategory.value, available: false, frames: [] };
  }
}

async function loadHeatmap(): Promise<void> {
  try {
    heatmap.value = await apiGet<HeatmapPayload>("/api/metrics/heatmap", { category: heatmapCategory.value, limit: 20 }, { ttlMs: 5 * 60_000, persist: true });
  } catch {
    heatmap.value = { category: heatmapCategory.value, available: false, x: [], y: [], cells: [] };
  }
}

async function loadR4Sections(): Promise<void> {
  try {
    // Market/macro availability changes when a local collector writes Gold.
    // Do not render a stale IndexedDB snapshot while a background refresh is
    // in flight: it would leave a newly imported series marked unavailable.
    const payload = await apiGet<{ sections: R4Section[] }>(
      "/api/data/sections",
      undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
    r4Sections.value = payload.sections;
  } catch { r4Sections.value = []; }
}
async function loadMacroCatalog(): Promise<void> {
  try {
    const payload = await apiGet<{ items: MacroCatalogItem[] }>(
      "/api/data/macro/catalog",
      { country: macroCountry.value },
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
    macroCatalog.value = payload.items;
    const firstAvailable = payload.items.find(item => item.available);
    if (!selectedMacroId.value || !payload.items.some(item => item.seriesId === selectedMacroId.value)) selectedMacroId.value = firstAvailable?.seriesId || "";
    if (selectedMacroId.value) await loadMacroSeries(); else macroSeries.value = undefined;
  } catch { macroCatalog.value = []; macroSeries.value = undefined; }
}
async function loadMacroSeries(): Promise<void> {
  if (!selectedMacroId.value) return;
  macroLoading.value = true;
  try {
    macroSeries.value = await apiGet<MacroSeriesPayload>(
      "/api/data/macro/series",
      { seriesId: selectedMacroId.value, view: macroView.value },
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
  }
  catch { macroSeries.value = undefined; }
  finally { macroLoading.value = false; }
}
const macroTimeline = computed<NamedSeries[]>(() => {
  if (!macroSeries.value?.available || macroSeries.value.view !== "timeline") return [];
  const observations = macroSeries.value.observations as MacroTimelineObservation[];
  return [{ name: macroSeries.value.series.name, points: observations.map(item => ({ t: item.observationPeriod, value: item.value })) }];
});
const macroSeasonal = computed<NamedSeries[]>(() => {
  if (!macroSeries.value?.available || macroSeries.value.view !== "seasonal") return [];
  const observations = macroSeries.value.observations as MacroSeasonalObservation[];
  return observations.map(item => ({
    name: item.year,
    points: item.months.map((value, index) => ({ t: `${index + 1}月`, value })),
  }));
});
const equityMarketCapTimeline = computed<NamedSeries[]>(() => {
  if (!equityOverview.value?.available) return [];
  const points = equityOverview.value.points
    .filter(item => typeof item.totalMarketCapYi === "number")
    .map(item => ({ t: item.tradingDay, value: item.totalMarketCapYi! }));
  return points.length ? [{ name: "总市值", points }] : [];
});
const equityTurnoverTimeline = computed<NamedSeries[]>(() => {
  if (!equityOverview.value?.available) return [];
  const points = equityOverview.value.points
    .filter(item => typeof item.turnoverYi === "number")
    .map(item => ({ t: item.tradingDay, value: item.turnoverYi! }));
  return points.length ? [{ name: "成交额", points }] : [];
});
const equityTurnoverUnit = computed(() => equityOverview.value?.market === "HK" ? "亿港元" : "亿元");

async function loadEquityOverview(): Promise<void> {
  const market = r4Section.value === "hk-equities" ? "hk" : "cn";
  if (r4Section.value === "macro") { equityOverview.value = undefined; return; }
  equityOverviewLoading.value = true;
  try {
    equityOverview.value = await apiGet<EquityOverviewPayload>(
      `/api/data/equities/${market}/overview`,
      undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
    if (market === "hk") {
      // HK history is intentionally derived on demand.  Reload the compact
      // section cards after that local calculation populates the backend cache.
      invalidateQuery("/api/data/sections");
      await loadR4Sections();
    }
  } catch { equityOverview.value = undefined; }
  finally { equityOverviewLoading.value = false; }
}
async function loadEquityStatusList(): Promise<void> {
  if (r4Section.value !== "cn-equities") { equityStatusList.value = undefined; return; }
  equityStatusListLoading.value = true;
  try {
    equityStatusList.value = await apiGet<EquityStatusListPayload>(
      "/api/data/equities/cn/lists",
      {
        type: equityListType.value,
        asOfDay: equityListAsOfDay.value,
        page: equityListPage.value,
        pageSize: equityListPageSize,
      },
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
  } catch { equityStatusList.value = undefined; }
  finally { equityStatusListLoading.value = false; }
}

function refreshEquityStatusListFromFirstPage(): void {
  equityListPage.value = 1;
  void loadEquityStatusList();
}

function changeEquityStatusListPage(page: number): void {
  equityListPage.value = page;
  void loadEquityStatusList();
}

// ---- 数据浏览器（受控只读预览 ≤500 行） ----
const view = ref("market");
const query = ref("");
const rows = ref<Record<string, unknown>[]>([]);
const total = ref(0);
const browserLoading = ref(false);
const browserError = ref("");
const chartElement = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;

const columns = computed(() => Object.keys(rows.value[0] || {}).slice(0, 8));

async function loadBrowser(): Promise<void> {
  browserLoading.value = true;
  browserError.value = "";
  try {
    const data = await apiGet<{ items: Record<string, unknown>[]; total: number }>(`/api/data/${encodeURIComponent(view.value)}`, { page_size: 500, q: query.value.trim() || undefined }, { ttlMs: 60_000, persist: true });
    rows.value = data.items;
    total.value = data.total;
    await nextTick();
    renderChart();
  } catch (reason) {
    browserError.value = reason instanceof Error ? reason.message : "数据浏览器加载失败";
    rows.value = [];
    total.value = 0;
  } finally {
    browserLoading.value = false;
  }
}

function renderChart(): void {
  if (!chartElement.value) return;
  chart ??= echarts.init(chartElement.value);
  const labels = rows.value.slice(0, 20).map((row, index) =>
    String(row.partition_id || row.provider || row.chain || row.instrumentKey || row.area || index + 1),
  );
  const values = rows.value
    .slice(0, 20)
    .map((row) => Number(row.row_count || row.rows || row.bytes || row.issue_count || row.value || 1));
  chart.setOption({
    backgroundColor: "transparent",
    grid: { top: 22, left: 55, right: 16, bottom: 58 },
    xAxis: {
      type: "category",
      data: labels,
      axisLabel: { color: "var(--ml-chart-axis)", rotate: 28 },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "var(--ml-chart-axis)" },
      splitLine: { lineStyle: { color: "var(--ml-chart-grid)" } },
    },
    series: [{ type: "bar", data: values, itemStyle: { color: "var(--ml-accent)" } }],
  });
}

function refreshAll(): void {
  invalidateQuery("/api/dashboard/definitions");
  for (const id of expandedPanels.value) { invalidateQuery(`/api/dashboard/${id}`); delete payloads.value[id]; }
  void loadDefinitions();
  invalidateQuery("/api/data/sections");
  invalidateQuery(`/api/data/equities/${r4Section.value === "hk-equities" ? "hk" : "cn"}/overview`);
  invalidateQuery("/api/data/macro/catalog");
  invalidateQuery("/api/data/equities/cn/lists");
  void Promise.all([loadR4Sections(), loadMacroCatalog(), loadEquityOverview(), loadEquityStatusList()]);
}

watch(view, () => void loadBrowser());
watch(rankingCategory, () => void loadRanking());
watch(heatmapCategory, () => void loadHeatmap());
watch(macroCountry, () => void loadMacroCatalog());
watch(selectedMacroId, () => {
  if (selectedMacroId.value !== "M2_MONEY_SUPPLY") macroView.value = "timeline";
  void loadMacroSeries();
});
watch(macroView, () => void loadMacroSeries());
watch(equityListType, refreshEquityStatusListFromFirstPage);
watch(() => route.query.section, value => {
  const section = typeof value === "string" ? { cn: "cn-equities", hk: "hk-equities", other: "macro" }[value] : undefined;
  if (section && r4Section.value !== section) r4Section.value = section;
});
watch(r4Section, value => {
  const section = { "cn-equities": "cn", "hk-equities": "hk", macro: "other" }[value];
  if (section && route.query.section !== section) void router.replace({ query: { ...route.query, section } });
  void loadEquityOverview();
  void loadEquityStatusList();
});

onMounted(() => {
  const section = typeof route.query.section === "string"
    ? { cn: "cn-equities", hk: "hk-equities", other: "macro" }[route.query.section]
    : undefined;
  if (section) r4Section.value = section;
  void Promise.all([loadDefinitions(), loadLayout(), loadR4Sections(), loadMacroCatalog(), loadEquityOverview(), loadEquityStatusList()]);
  window.addEventListener("resize", renderChart);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", renderChart);
  chart?.dispose();
});
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1 class="page-title">数据</h1>
        <p class="page-note">Grafana 风格本地只读监查：只展示真实可用的面板、排行与热力图；数据浏览器不暴露任意 SQL。</p>
      </div>
      <el-button :loading="loading" data-test="data-refresh" @click="refreshAll">刷新</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />

    <section class="panel r4-data-sections" data-test="r4-data-sections">
      <div class="panel-title"><h2>市场数据分区</h2><span class="muted">没有真实来源或口径证据的面板保持不可用，不显示演示数值。</span></div>
      <el-tabs v-model="r4Section">
        <el-tab-pane v-for="section in r4Sections" :key="section.id" :label="section.title" :name="section.id">
          <template v-if="section.id !== 'macro'">
            <div class="r4-panel-grid">
              <article v-for="panel in section.panels" :key="panel.id" class="r4-panel-card">
                <strong>{{ panel.title }}</strong>
                <el-tag :type="panel.status === 'PASS' ? 'success' : panel.status === 'PARTIAL' ? 'warning' : 'info'">{{ panel.status }}</el-tag>
                <dl v-if="panel.values?.length" class="r4-panel-values">
                  <template v-for="metric in panel.values" :key="metric.metricKey">
                    <dt>{{ metric.metricName }}（{{ metric.tradingDate }}）</dt>
                    <dd :title="metric.definition">{{ metric.value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }}</dd>
                  </template>
                </dl>
                <p v-else>{{ panel.status === 'UNAVAILABLE' ? '等待真实来源、口径或历史覆盖验证。' : '已有部分本地数据，仍需按 R4 口径完成验证。' }}</p>
                <ul v-if="panel.limitations?.length"><li v-for="item in panel.limitations" :key="item">{{ item }}</li></ul>
              </article>
            </div>
            <template v-if="r4Section === section.id && equityOverview?.available">
              <div class="r4-equity-charts">
                <SeriesChart v-if="equityMarketCapTimeline.length" :title="`${section.title}总市值走势`" :series="equityMarketCapTimeline" :unit="equityTurnoverUnit" :height="260" />
                <SeriesChart v-if="equityTurnoverTimeline.length" :title="`${section.title}成交额走势`" :series="equityTurnoverTimeline" :unit="equityTurnoverUnit" :height="260" />
              </div>
              <p class="muted">{{ equityOverview.limitations[0] }}</p>
            </template>
            <p v-else-if="r4Section === section.id && section.id === 'hk-equities' && !equityOverviewLoading" class="muted">{{ equityOverview?.limitations[0] || '港股总览等待真实来源与统计范围验证。' }}</p>
            <section v-if="section.id === 'cn-equities'" class="r4-status-list" data-test="r4-equity-status-list">
              <div class="panel-title">
                <div>
                  <h3>风险与状态名单</h3>
                  <p class="muted">{{ equityStatusList?.asOfDay ? `截至 ${equityStatusList.asOfDay}` : '截至最新已采集的权威名单日期' }}</p>
                </div>
                <div class="r4-status-list-controls">
                  <el-radio-group v-model="equityListType" size="small" aria-label="A股状态名单类型">
                    <el-radio-button v-for="option in equityListTypeOptions" :key="option.value" :value="option.value">{{ option.label }}</el-radio-button>
                  </el-radio-group>
                  <el-date-picker
                    v-model="equityListAsOfDay"
                    type="date"
                    value-format="YYYY-MM-DD"
                    placeholder="按日期查看"
                    clearable
                    size="small"
                    aria-label="A股状态名单日期"
                    @change="refreshEquityStatusListFromFirstPage"
                  />
                </div>
              </div>
              <el-table v-if="equityStatusList?.available" :data="equityStatusList.items" max-height="340" empty-text="当前日期没有名单记录">
                <el-table-column prop="symbol" label="代码" min-width="100" />
                <el-table-column prop="name" label="名称" min-width="140" />
                <el-table-column prop="assetType" label="证券类型" min-width="100" />
                <el-table-column prop="status" label="状态" min-width="120" />
                <el-table-column prop="effectiveFrom" label="开始日期" min-width="120" />
                <el-table-column prop="expectedEnd" label="结束/恢复日期" min-width="140" />
                <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
                <el-table-column prop="source" label="来源" min-width="160" show-overflow-tooltip />
              </el-table>
              <el-pagination
                v-if="equityStatusList?.available && typeof equityStatusList.total === 'number' && equityStatusList.total > equityListPageSize"
                v-model:current-page="equityListPage"
                :page-size="equityListPageSize"
                :total="equityStatusList.total"
                layout="total, prev, pager, next"
                small
                class="r4-status-list-pagination"
                @current-change="changeEquityStatusListPage"
              />
              <p v-else-if="!equityStatusListLoading" class="muted">{{ equityStatusList?.limitations[0] || '状态名单尚未加载。' }}</p>
            </section>
          </template>
          <template v-else>
            <div class="macro-controls"><el-radio-group v-model="macroCountry" size="small" aria-label="宏观国家地区"><el-radio-button value="CN">中国</el-radio-button><el-radio-button value="US">美国</el-radio-button></el-radio-group><el-select v-model="selectedMacroId" placeholder="选择已登记序列" :loading="macroLoading"><el-option v-for="item in macroCatalog" :key="item.seriesId" :label="`${item.name}${item.available ? '' : '（暂无数据）'}`" :value="item.seriesId" :disabled="!item.available" /></el-select><el-radio-group v-if="selectedMacroId === 'M2_MONEY_SUPPLY'" v-model="macroView" size="small" aria-label="M2图表视图"><el-radio-button value="timeline">时间序列</el-radio-button><el-radio-button value="seasonal">季节图</el-radio-button></el-radio-group></div>
            <div class="macro-catalog"><article v-for="item in macroCatalog" :key="item.seriesId"><strong>{{ item.name }}</strong><span>{{ item.frequency }} · {{ item.unit }} · {{ item.source }}</span><small v-if="item.latestObservationPeriod">{{ item.timeBasis === 'SOURCE_DATE' ? '来源日期' : '观测期' }} {{ item.latestObservationPeriod }} · 本机取得 {{ item.latestFetchedAt }}</small><el-tag :type="item.available ? 'success' : 'info'">{{ item.available ? '可用' : 'UNAVAILABLE' }}</el-tag></article></div>
            <SeriesChart v-if="macroTimeline.length" :title="macroSeries?.series.name || ''" :series="macroTimeline" :unit="macroSeries?.series.unit" :height="300" /><SeriesChart v-else-if="macroSeasonal.length" :title="`${macroSeries?.series.name || ''}季节图`" :series="macroSeasonal" :unit="macroSeries?.series.unit" :height="300" /><p v-else class="muted">当前地区没有可展示的本地宏观 Gold 序列。</p>
          </template>
        </el-tab-pane>
      </el-tabs>
    </section>

    <section class="panel category-filter" data-test="dashboard-categories">
      <el-radio-group v-model="categoryFilter">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button v-for="category in categories" :key="category" :value="category">{{ category }}</el-radio-button>
      </el-radio-group>
      <span class="muted">{{ availablePanels.length }} 个可用面板（无数据的面板自动隐藏）</span>
    </section>

    <section class="panel">
      <div class="panel-title"><h2>我的仪表盘</h2><div><el-select v-model="newPanelMetric" size="small"><el-option v-for="item in definitions" :key="item.id" :label="item.title" :value="item.id" /></el-select><el-button size="small" type="primary" @click="void addPanel()">添加面板</el-button></div></div>
      <p v-if="!personalPanels.length" class="muted">尚未添加自定义面板。布局仅保存到本机个人配置，不会改变业务指标定义。</p>
      <div v-else class="dashboard-grid">
        <div v-for="panel in personalPanels.filter(item => !item.hidden)" :key="panel.id" class="panel dashboard-panel" :class="{ 'dashboard-panel-full': panel.width === 'full' }">
          <div class="panel-title"><h3>{{ panel.title }}</h3><div><el-button text size="small" @click="void movePanel(panel.id, -1)">上移</el-button><el-button text size="small" @click="void movePanel(panel.id, 1)">下移</el-button><el-button text size="small" @click="void togglePanelHidden(panel)">隐藏</el-button><el-button text size="small" @click="void loadPanel(panel.metricId)">加载</el-button><el-button text type="danger" size="small" @click="void removePanel(panel.id)">删除</el-button></div></div>
          <div class="data-controls personal-panel-settings"><el-input v-model="panel.title" size="small" aria-label="面板标题" @change="void saveLayout()" /><el-select v-model="panel.width" size="small" aria-label="面板宽度" @change="void saveLayout()"><el-option label="半宽" value="half" /><el-option label="整行" value="full" /></el-select><el-select v-model="panel.chartType" size="small" aria-label="图表类型" @change="void saveLayout()"><el-option label="折线图" value="line" /><el-option label="柱状图" value="bar" /></el-select><el-select v-model="panel.rangeDays" size="small" aria-label="时间范围" @change="void saveLayout()"><el-option label="全部时间" :value="0" /><el-option label="近30天" :value="30" /><el-option label="近90天" :value="90" /><el-option label="近1年" :value="365" /></el-select><input v-model="panel.color" type="color" aria-label="图表颜色" @change="void saveLayout()" /><el-slider v-model="panel.opacity" :min="0" :max="1" :step="0.05" aria-label="面积透明度" @change="void saveLayout()" /></div>
          <SeriesChart v-if="payloads[panel.metricId]?.series?.length" :title="panel.title" :series="payloads[panel.metricId]?.series ?? []" :height="240" :color="panel.color" :chart-type="panel.chartType" :opacity="panel.opacity" :range-days="panel.rangeDays" /><div v-else class="chart-empty-panel">点击“加载”读取指标</div>
        </div>
      </div>
      <div v-if="personalPanels.some(item => item.hidden)" class="hidden-panels"><span class="muted">已隐藏面板：</span><el-button v-for="panel in personalPanels.filter(item => item.hidden)" :key="panel.id" text size="small" @click="void togglePanelHidden(panel)">恢复 {{ panel.title }}</el-button></div>
    </section>

    <section v-if="availablePanels.length" class="dashboard-grid">
      <div v-for="panel in availablePanels" :key="panel.id" class="panel dashboard-panel" :data-test="`dashboard-${panel.id}`">
        <div class="panel-title">
          <h2>{{ panel.title }}</h2>
          <div><el-tag size="small">{{ panel.category }}</el-tag><el-button text size="small" @click="void loadPanel(panel.id)">{{ expandedPanels.has(panel.id) ? "已加载" : "加载面板" }}</el-button></div>
        </div>
        <SeriesChart
          v-if="payloads[panel.id]?.series?.length"
          :title="panel.title"
          :series="payloads[panel.id]?.series ?? []"
          :unit="payloads[panel.id]?.unit"
          :height="260"
        />
        <div v-else class="chart-empty-panel">暂无该指标数据</div>
      </div>
    </section>
    <section v-else-if="!loading" class="panel empty-state" data-test="dashboard-empty">
      <h2>暂无可用数据面板</h2>
      <p class="muted">本地数据库尚无对应数据；只读页面不会生成假序列。</p>
    </section>

    <div class="metrics-layout">
      <section class="panel">
        <div class="panel-title">
          <h2>动态排行</h2>
          <el-select v-model="rankingCategory" size="small" data-test="ranking-category">
            <el-option label="期货净持仓" value="futures" />
            <el-option label="Gold 指标" value="gold" />
            <el-option label="市场广度" value="breadth" />
          </el-select>
        </div>
        <el-button v-if="!ranking.frames.length" text @click="void loadRanking()">加载排行</el-button><RankingChart v-else :frames="ranking.frames" :height="300" data-test="ranking-chart" />
      </section>
      <section class="panel">
        <div class="panel-title">
          <h2>热力图</h2>
          <el-select v-model="heatmapCategory" size="small" data-test="heatmap-category">
            <el-option label="市场广度" value="breadth" />
            <el-option label="Gold 指标" value="gold" />
            <el-option label="存储占用" value="storage" />
          </el-select>
        </div>
        <el-button v-if="!heatmap.cells.length" text @click="void loadHeatmap()">加载热力图</el-button><HeatmapChart v-else :x="heatmap.x" :y="heatmap.y" :cells="heatmap.cells" :height="300" data-test="heatmap-chart" />
      </section>
    </div>

    <section class="panel data-browser">
      <div class="panel-title">
        <h2>数据浏览器</h2>
        <span class="muted">预览最多 500 行；服务端筛选、排序、分页</span>
      </div>
      <div class="data-controls">
        <el-select v-model="view" data-test="browser-view">
          <el-option v-for="[key, label] in browserViews" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input v-model="query" placeholder="服务端筛选" clearable @keyup.enter="void loadBrowser()" />
        <el-button type="primary" :loading="browserLoading" data-test="browser-query" @click="void loadBrowser()">查询</el-button>
        <span class="muted">{{ total }} 条</span>
      </div>
      <el-alert v-if="browserError" :title="browserError" type="warning" :closable="false" class="page-alert" />
      <el-button v-if="!rows.length && !browserLoading" text @click="void loadBrowser()">加载数据浏览器</el-button><div v-if="rows.length || browserLoading" ref="chartElement" class="data-chart" />
      <el-table :data="rows" v-loading="browserLoading" max-height="520" empty-text="暂无数据">
        <el-table-column v-for="column in columns" :key="column" :prop="column" :label="column" min-width="150" show-overflow-tooltip />
      </el-table>
    </section>
  </section>
</template>

<style scoped>
.r4-panel-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
.r4-panel-card { min-height: 104px; padding: 14px; border: 1px solid var(--ml-border); border-radius: 8px; display: grid; gap: 7px; align-content: start; }
.r4-panel-card p, .macro-catalog span { margin: 0; color: var(--ml-text-secondary); font-size: 12px; }
.r4-panel-card ul { margin: 0; padding-left: 18px; color: var(--ml-text-disabled); font-size: 11px; }
.r4-panel-values { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; margin: 0; font-size: 12px; }
.r4-panel-values dt { color: var(--ml-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.r4-panel-values dd { margin: 0; color: var(--ml-text-primary); font-variant-numeric: tabular-nums; }
.r4-equity-charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
.r4-status-list { margin-top: 12px; padding: 12px; border: 1px solid var(--ml-border); border-radius: 8px; }
.r4-status-list .panel-title { flex-wrap: wrap; }
.r4-status-list .muted { margin: 0; }
.r4-status-list-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.r4-status-list-pagination { margin-top: 12px; justify-content: flex-end; }
.macro-controls { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.macro-controls .el-select { min-width: 270px; }
.macro-catalog { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 8px; margin-bottom: 12px; }
.macro-catalog article { display: grid; grid-template-columns: 1fr auto; gap: 5px 10px; padding: 9px 11px; border: 1px solid var(--ml-border); border-radius: 6px; }
.macro-catalog span, .macro-catalog small { grid-column: 1 / -1; }
.macro-catalog small { color: var(--ml-text-disabled); font-size: 11px; }
@media (max-width: 760px) { .r4-equity-charts { grid-template-columns: 1fr; } }
</style>
