<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { apiGet } from "../domain/api";

type Sector = { code: string; name: string };
type Row = { id: string; name: string; sector: string; status: string; relationCount: number };
type Node = { id: string; type: string; name: string; instrumentKey?: string };
type Edge = { id: string; fromNode: string; toNode: string; type: string; summary: string; stage: string };
type Catalog = { sectors: Sector[]; chains: Row[]; status?: string };
type ClassificationItem = { code: string; name: string; parentCode: string | null; level: number; sector: string };
type Classification = { standard: string; counts: Record<string, number>; items: ClassificationItem[] };
type CSRCClassification = { standard: string; manufacturingSubcategories: { code: string; name: string; nbsDivisions: { code: string; name: string }[] }[]; companyAssignmentPolicy: string };
type Chain = { id: string; name: string; status: string; nodes: Node[]; edges: Edge[]; sections: Record<string, string[]> };
const route = useRoute();
const router = useRouter();
const catalog = ref<Catalog>();
const classification = ref<Classification>();
const csrcClassification = ref<CSRCClassification>();
const classificationQuery = ref("");
const expandedClassifications = ref<Set<string>>(new Set());
const selectedClassification = ref<ClassificationItem>();
const chain = ref<Chain>();
const activeSector = ref("all");
const query = ref("");
const selectedNode = ref<Node>();
const expanded = ref(false);
const catalogLoading = ref(false);
const detailLoading = ref(false);
const loading = computed(() => catalogLoading.value || detailLoading.value);
const error = ref("");
const classificationError = ref("");
const graphElement = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
let detailRequest = 0;
let catalogRequest = 0;
const labels: Record<string, string> = { RAW_MATERIAL: "原材料", PRODUCT: "产品", PROCESS: "加工", EQUIPMENT: "设备", APPLICATION: "应用", BRAND: "品牌", COMPANY: "企业", SERVICE: "服务" };
const colors: Record<string, string> = { RAW_MATERIAL: "#8ab6df", PRODUCT: "#51c7aa", PROCESS: "#d6a56a", EQUIPMENT: "#a990d8", APPLICATION: "#d18f9a", BRAND: "#e4c66c", COMPANY: "#86a8a0", SERVICE: "#9fb7d8" };
const relationLabels: Record<string, string> = { INPUT_TO: "投入", PROCESSES_TO: "加工为", PRODUCES: "生产", USED_IN: "应用于", BRANDS: "品牌", SUPPLIES: "供应", EQUIPS: "提供设备" };
function escapeTooltip(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character] || character);
}
const visibleChains = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase();
  const sectors = new Map((catalog.value?.sectors || []).map((item) => [item.code, item.name]));
  return (catalog.value?.chains || []).filter((item) =>
    (activeSector.value === "all" || item.sector === activeSector.value)
    && `${item.name} ${sectors.get(item.sector) || ""}`.toLocaleLowerCase().includes(needle));
});
const classificationChildren = computed(() => new Set((classification.value?.items || []).map((item) => item.parentCode).filter(Boolean)));
const visibleClassification = computed(() => {
  const items = classification.value?.items || [];
  const needle = classificationQuery.value.trim().toLocaleLowerCase();
  if (needle) return items.filter((item) => `${item.code} ${item.name}`.toLocaleLowerCase().includes(needle)).slice(0, 120);
  const byCode = new Map(items.map((item) => [item.code, item]));
  return items.filter((item) => {
    let parent = item.parentCode;
    while (parent) {
      if (!expandedClassifications.value.has(parent)) return false;
      parent = byCode.get(parent)?.parentCode || null;
    }
    return true;
  });
});
const classificationPath = computed(() => {
  const byCode = new Map((classification.value?.items || []).map(item => [item.code, item]));
  const path: ClassificationItem[] = [];
  let item = selectedClassification.value;
  const visited = new Set<string>();
  while (item && !visited.has(item.code)) {
    visited.add(item.code);
    path.unshift(item);
    item = item.parentCode ? byCode.get(item.parentCode) : undefined;
  }
  return path;
});
const explorerItems = computed(() => {
  const items = classification.value?.items || [];
  const needle = classificationQuery.value.trim().toLocaleLowerCase();
  if (needle) return items.filter(item => `${item.code} ${item.name}`.toLocaleLowerCase().includes(needle)).slice(0, 120);
  return items.filter(item => selectedClassification.value ? item.parentCode === selectedClassification.value.code : item.parentCode === null);
});
function exploreClassification(item?: ClassificationItem): void {
  selectedClassification.value = item;
  classificationQuery.value = "";
}
function toggleClassification(item: ClassificationItem): void {
  selectedClassification.value = item;
  if (!classificationChildren.value.has(item.code)) return;
  const next = new Set(expandedClassifications.value);
  if (next.has(item.code)) next.delete(item.code); else next.add(item.code);
  expandedClassifications.value = next;
}
const related = computed(() => selectedNode.value ? (chain.value?.edges || []).filter((edge) => edge.fromNode === selectedNode.value?.id || edge.toNode === selectedNode.value?.id) : []);
const graphNodes = computed(() => (chain.value?.nodes || []).slice(0, expanded.value ? 160 : 45));
const nodesById = computed(() => new Map((chain.value?.nodes || []).map((node) => [node.id, node])));
function nodeName(id: string): string { return nodesById.value.get(id)?.name || "未知节点"; }
function chooseNode(node: Node): void {
  selectedNode.value = node;
  const index = chain.value?.nodes.findIndex((item) => item.id === node.id) ?? -1;
  if (index >= 45 && index < 160) expanded.value = true;
}

async function selectChain(id: string, updateRoute = true): Promise<void> {
  const request = ++detailRequest;
  if (chain.value?.id === id) {
    detailLoading.value = false;
    error.value = "";
    if (updateRoute) void router.replace({ path: "/industry/", query: { chain: id } });
    return;
  }
  detailLoading.value = true;
  error.value = "";
  try {
    const result = await apiGet<Chain>(`/api/industry/chains/${encodeURIComponent(id)}`, undefined, { force: true });
    if (request !== detailRequest) return;
    chain.value = result;
    selectedNode.value = undefined;
    expanded.value = false;
    if (updateRoute) void router.replace({ path: "/industry/", query: { chain: id } });
    await nextTick();
    drawGraph();
  } catch (failure) {
    if (request === detailRequest) error.value = failure instanceof Error ? failure.message : "产业链详情读取失败";
  } finally { if (request === detailRequest) detailLoading.value = false; }
}
async function loadCatalog(): Promise<void> {
  const request = ++catalogRequest;
  const initialDetailRequest = detailRequest;
  catalogLoading.value = true;
  error.value = "";
  classificationError.value = "";
  try {
    const [catalogResult, classificationResult, csrcResult] = await Promise.allSettled([
      apiGet<Catalog>("/api/industry/catalog", undefined, { force: true }),
      apiGet<Classification>("/api/industry/classification", undefined, { force: true }),
      apiGet<CSRCClassification>("/api/industry/csrc-classification", undefined, { force: true }),
    ]);
    if (request !== catalogRequest) return;
    if (classificationResult.status === "fulfilled") classification.value = classificationResult.value;
    if (csrcResult.status === "fulfilled") csrcClassification.value = csrcResult.value;
    if (classificationResult.status === "rejected" || csrcResult.status === "rejected") classificationError.value = "部分行业分类暂时不可用，可稍后重试。";
    if (catalogResult.status === "rejected") throw catalogResult.reason;
    catalog.value = catalogResult.value;
    const wanted = typeof route.query.chain === "string" ? route.query.chain : "";
    const initial = catalog.value.chains.find((item) => item.id === wanted) || catalog.value.chains.find((item) => item.relationCount > 0) || catalog.value.chains[0];
    if (initial && detailRequest === initialDetailRequest) await selectChain(initial.id, false);
  } catch (failure) { if (request === catalogRequest) error.value = failure instanceof Error ? failure.message : "产业目录读取失败"; }
  finally { if (request === catalogRequest) catalogLoading.value = false; }
}
function drawGraph(): void {
  if (!graphElement.value) return;
  chart ??= echarts.init(graphElement.value);
  const nodes = graphNodes.value;
  const ids = new Set(nodes.map((item) => item.id));
  const edges = (chain.value?.edges || []).filter((item) => ids.has(item.fromNode) && ids.has(item.toNode));
  chart.setOption({
    tooltip: { trigger: "item", formatter: (params: any) => params.dataType === "edge" ?
      `${escapeTooltip(relationLabels[params.data.type] || params.data.type)}<br/>${escapeTooltip(params.data.summary)}` :
      `${escapeTooltip(labels[params.data.type] || params.data.type)} · ${escapeTooltip(params.data.name)}` },
    series: [{ type: "graph", layout: "force", roam: true, draggable: true, force: { repulsion: 220, gravity: .06, edgeLength: 110 },
      label: { show: true, position: "right", color: "#aebbc4", fontSize: 11 }, lineStyle: { color: "source", curveness: .12, opacity: .6 },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 7,
      data: nodes.map((item) => ({ ...item, symbolSize: selectedNode.value?.id === item.id ? 29 : 20, itemStyle: { color: colors[item.type] || "#8da7b4" } })),
      links: edges.map((item) => ({ ...item, source: item.fromNode, target: item.toNode })) }],
  }, true);
  chart.off("click");
  chart.on("click", (params: any) => { if (params.dataType === "node") { const node = nodes.find((item) => item.id === params.data.id); if (node) chooseNode(node); } });
}
watch(expanded, () => void nextTick(drawGraph));
watch(graphElement, (element) => {
  observer?.disconnect();
  chart?.dispose();
  chart = undefined;
  if (element) { observer?.observe(element); drawGraph(); }
}, { flush: "post" });
watch(selectedNode, (node) => {
  chart?.dispatchAction({ type: "downplay", seriesIndex: 0 });
  const index = graphNodes.value.findIndex((item) => item.id === node?.id);
  if (index >= 0) chart?.dispatchAction({ type: "highlight", seriesIndex: 0, dataIndex: index });
});
onMounted(() => { observer = new ResizeObserver(() => chart?.resize()); void loadCatalog(); });
onBeforeUnmount(() => { catalogRequest++; detailRequest++; observer?.disconnect(); chart?.dispose(); chart = undefined; });
</script>

<template>
  <section class="industry-page">
    <header class="industry-header"><div><span class="eyebrow">INDUSTRY KNOWLEDGE</span><h1>产业链</h1><p>从产品、原材料、加工到品牌与企业，查看当前已核验的产业关系。</p></div><span class="pill">{{ catalog?.status === "NOT_PUBLISHED" ? "尚未发布" : "本机知识快照" }}</span></header>
    <p v-if="error" class="error" role="alert">{{ error }} <button type="button" @click="loadCatalog">重试</button></p>
    <div class="layout" :aria-busy="loading">
      <aside class="catalog panel" aria-label="产业目录">
        <details class="classification"><summary>国民经济行业分类 · 完整目录</summary>
          <p>20 门类、97 大类、473 中类、1,382 小类。标准分类与产业关系分别核验。</p>
          <p v-if="classificationError" class="classification-note" role="status">{{ classificationError }} <button type="button" @click="loadCatalog">重试</button></p>
          <input v-model="classificationQuery" type="search" aria-label="搜索国标行业分类" placeholder="搜索代码或行业名称" />
          <div class="classification-list">
            <button v-for="item in visibleClassification" :key="item.code" type="button"
              :style="{ paddingLeft: `${10 + (item.level - 1) * 13}px` }"
              :class="{ active: item.code === selectedClassification?.code }" @click="toggleClassification(item)">
              <span>{{ classificationChildren.has(item.code) ? (expandedClassifications.has(item.code) ? "▾" : "▸") : "·" }} {{ item.code }} {{ item.name }}</span>
            </button>
          </div>
          <p v-if="selectedClassification" class="classification-note">{{ selectedClassification.code }} {{ selectedClassification.name }}：与具体产业链的对应关系仍需独立核验。</p>
          <details class="csrc-classification"><summary>上市公司制造业辅助分类 · {{ csrcClassification?.standard }}</summary>
            <p>{{ csrcClassification?.companyAssignmentPolicy }}</p>
            <div v-for="group in csrcClassification?.manufacturingSubcategories || []" :key="group.code" class="csrc-group">
              <strong>{{ group.code }} {{ group.name }}</strong><small>{{ group.nbsDivisions.map((item) => `${item.code} ${item.name}`).join("、") }}</small>
            </div>
          </details>
        </details>
        <label for="chain-search">搜索产业链</label><input id="chain-search" v-model="query" type="search" placeholder="产业链名称或行业" />
        <label for="sector-select">行业分类</label><select id="sector-select" v-model="activeSector"><option value="all">全部行业</option><option v-for="sector in catalog?.sectors || []" :key="sector.code" :value="sector.code">{{ sector.code }} · {{ sector.name }}</option></select>
        <p v-if="catalog?.chains.length" class="catalog-count" aria-live="polite">显示 {{ visibleChains.length }} / {{ catalog.chains.length }} 条产业链</p>
        <div class="chain-list"><button v-for="item in visibleChains" :key="item.id" type="button" :aria-pressed="item.id === chain?.id" :class="{ active: item.id === chain?.id }" @click="selectChain(item.id)"><span>{{ item.name }}</span><small>{{ item.status }}<template v-if="item.relationCount"> · {{ item.relationCount }} 条关系</template></small></button><p v-if="!visibleChains.length && !catalogLoading" class="empty">{{ catalog?.status === 'NOT_PUBLISHED' ? '知识快照尚未发布，可先浏览上方行业分类。' : '没有匹配的产业链。' }}</p></div>
      </aside>
      <main class="main-column">
        <div v-if="chain" class="chain-heading panel"><div><h2>{{ chain.name }}</h2><p>{{ chain.status === "已核验" ? "以下关系已通过本机审核。" : chain.status === "部分已核验" ? "展示已核验关系，其余内容仍待核验。" : "该产业链已列入目录，详细关系仍待核验。" }}</p></div><span class="pill">{{ chain.status }}</span></div>
        <section v-if="chain" class="panel content-panel" aria-label="产业关系图"><div class="panel-heading"><div><h3>产品与流程关系</h3><p v-if="chain.nodes.length" class="graph-count">图中 {{ graphNodes.length }} / {{ chain.nodes.length }} 个节点 · {{ chain.edges.length }} 条已核验关系</p></div><button v-if="chain.nodes.length > 45" type="button" @click="expanded = !expanded">{{ expanded ? "收起节点" : "展开更多节点" }}</button></div><div v-if="chain.nodes.length" ref="graphElement" class="graph" role="img" :aria-label="`${chain.name}产业关系图`"></div><div v-else class="graph empty-graph">暂无经过核验的产品关系。可从左侧切换其他产业链。</div>
          <div v-if="chain.nodes.length" class="node-directory" aria-label="产业节点列表"><div class="node-directory-heading"><strong>查看节点</strong><span>点击节点查看方向和说明</span></div><div class="node-list"><button v-for="node in chain.nodes" :key="node.id" type="button" :aria-pressed="selectedNode?.id === node.id" :class="{ active: selectedNode?.id === node.id }" @click="chooseNode(node)"><i :style="{ backgroundColor: colors[node.type] || '#8da7b4' }" aria-hidden="true"></i>{{ node.name }} <small>{{ labels[node.type] || node.type }}</small></button></div><p v-if="chain.nodes.length > graphNodes.length" class="graph-count">图谱仅显示前 {{ graphNodes.length }} 个节点，列表可查看全部节点。</p></div>
        </section>
        <section v-if="chain" class="panel content-panel" aria-label="产业解释"><div class="panel-heading"><h3>{{ selectedNode ? selectedNode.name : "产业说明" }}</h3><div v-if="selectedNode" class="node-actions"><span class="pill">{{ labels[selectedNode.type] || selectedNode.type }}</span><button type="button" @click="selectedNode = undefined">返回产业说明</button></div></div>
          <template v-if="selectedNode"><article v-for="edge in related" :key="edge.id" class="relation"><div class="relation-path"><strong>{{ nodeName(edge.fromNode) }}</strong><span aria-hidden="true">→</span><strong>{{ nodeName(edge.toNode) }}</strong><small>{{ relationLabels[edge.type] || edge.type }}</small></div><p class="explanation">{{ edge.summary }}</p></article><p v-if="!related.length" class="empty">该节点暂无更多已核验的说明。</p><router-link v-if="selectedNode.instrumentKey" :to="`/f10/company/${encodeURIComponent(selectedNode.instrumentKey)}`" class="f10-link">查看本机 F10 →</router-link></template>
          <template v-else><div v-for="(items, title) in chain?.sections || {}" :key="title" class="group"><h4>{{ title }}</h4><p v-for="(item, index) in items" :key="index">{{ item }}</p></div><p v-if="!chain?.edges.length" class="empty">原材料、加工、品牌和企业等内容正在核验，尚不作关系推断。</p></template>
        </section>
        <section v-else-if="!loading && !error" class="panel unpublished" data-test="industry-unpublished"><h2>产业知识尚未发布</h2><p>当前还没有经过审核发布的产业关系；审核发布后，这里会显示产业链、节点与关系。</p>
          <div class="classification-explorer" role="region" aria-label="行业分类浏览">
            <div class="explorer-heading"><div><h3>浏览国民经济行业分类</h3><p>分类用于导航，与上下游供应关系需分别核验。</p></div><label class="explorer-search">搜索分类 <input v-model="classificationQuery" type="search" aria-label="搜索行业代码或名称" placeholder="代码或名称" /></label></div>
            <nav class="classification-path" aria-label="分类路径"><button type="button" @click="exploreClassification()">全部门类</button><template v-for="item in classificationPath" :key="item.code"><span aria-hidden="true">/</span><button type="button" @click="exploreClassification(item)">{{ item.code }} {{ item.name }}</button></template></nav>
            <p v-if="classificationError" class="empty" role="status">{{ classificationError }}</p>
            <div v-if="explorerItems.length" class="explorer-grid"><button v-for="item in explorerItems" :key="item.code" type="button" @click="exploreClassification(item)"><strong>{{ item.code }}</strong><span>{{ item.name }}</span><small>{{ classificationChildren.has(item.code) ? '查看下级' : '末级分类' }}</small></button></div>
            <p v-else-if="classification" class="empty">{{ classificationQuery ? '没有匹配的行业分类。' : '当前分类没有下级目录；具体产业关系仍待核验。' }}</p>
          </div>
        </section>
      </main>
    </div>
  </section>
</template>

<style scoped>
.industry-page{max-width:1700px;margin:0 auto;padding:8px 10px 40px}.industry-header{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:24px;border-bottom:1px solid var(--ml-divider);padding-bottom:20px}.eyebrow{font-size:10px;font-weight:700;letter-spacing:.16em;color:var(--ml-accent)}h1{font-size:30px;margin:5px 0 7px}h2{font-size:22px;margin:6px 0}h3{font-size:16px;margin:0}h4{font-size:13px;margin:0 0 8px}p{color:var(--ml-text-secondary);line-height:1.7}.industry-header p,.chain-heading p{font-size:13px;margin:0}.pill{font-size:11px;color:var(--ml-text-secondary);border:1px solid var(--ml-divider);padding:6px 11px;border-radius:20px;white-space:nowrap}.layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:18px;align-items:start}.panel{background:var(--ml-surface);border:1px solid var(--ml-divider);border-radius:var(--ml-radius-panel);box-shadow:0 4px 18px var(--ml-shadow-soft,var(--ml-divider))}.catalog{padding:17px;position:sticky;top:12px;max-height:calc(100vh - 135px);display:flex;flex-direction:column}.catalog label{font-size:11px;color:var(--ml-text-secondary);margin:0 0 6px}.catalog input,.catalog select{width:100%;color:var(--ml-text-primary);background:var(--ml-surface);border:1px solid var(--ml-divider);border-radius:8px;padding:9px 10px;margin-bottom:14px}.classification{margin-bottom:14px;border-bottom:1px solid var(--ml-divider);padding-bottom:12px}.classification summary{cursor:pointer;font-size:12px;font-weight:650}.classification p{font-size:10px;line-height:1.5}.classification-list{max-height:260px;overflow:auto;display:grid;gap:1px}.classification-list button{font-size:11px;line-height:1.5}.classification-note{color:var(--ml-accent)!important}.chain-list{overflow:auto;display:grid;gap:3px}.chain-list button,.classification-list button{border:0;background:transparent;color:var(--ml-text-primary);text-align:left;padding:10px;border-radius:8px;display:flex;justify-content:space-between;gap:8px;cursor:pointer}.chain-list button:hover,.chain-list button.active,.classification-list button:hover,.classification-list button.active{background:color-mix(in srgb,var(--ml-accent) 12%,var(--ml-surface))}.chain-list small{white-space:nowrap;color:var(--ml-text-secondary);font-size:10px}.main-column{display:grid;gap:16px;min-width:0}.chain-heading{padding:18px 22px;display:flex;justify-content:space-between;gap:16px;align-items:start}.content-panel{padding:18px 22px}.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:10px}.panel-heading button,.error button{background:transparent;border:0;color:var(--ml-accent);cursor:pointer}.graph{width:100%;height:480px}.empty-graph{display:grid;place-items:center;color:var(--ml-text-secondary);font-size:13px}.explanation{font-size:13px}.explanation strong{color:var(--ml-text-primary)}.f10-link{font-size:12px;color:var(--ml-accent);display:inline-block;margin:8px 0}.group{margin:18px 0;border-top:1px solid var(--ml-divider);padding-top:14px}.group p{font-size:13px;margin:7px 0}.empty{font-size:12px}.error{padding:12px 16px;border:1px solid #d77;border-radius:8px}@media(max-width:900px){.layout{grid-template-columns:1fr}.catalog{position:static;max-height:290px}.graph{height:400px}}
.csrc-classification{margin-top:12px;padding-top:10px;border-top:1px solid var(--ml-divider)}.csrc-classification summary{font-size:11px}.csrc-group{padding:7px 0;border-bottom:1px solid var(--ml-divider)}.csrc-group strong{font-size:11px}.csrc-group small{display:block;color:var(--ml-text-secondary);font-size:10px;line-height:1.5}
.catalog-count{margin:0 0 6px;font-size:11px;color:var(--ml-text-secondary);font-variant-numeric:tabular-nums}.chain-list button[aria-pressed="true"]{box-shadow:inset 3px 0 var(--ml-accent)}
.classification-explorer{margin-top:24px;border-top:1px solid var(--ml-divider);padding-top:20px}.explorer-heading{display:flex;justify-content:space-between;align-items:end;gap:16px}.explorer-heading h3{font-size:15px}.explorer-heading p{margin:5px 0 0;font-size:11px}.explorer-search{display:flex;align-items:center;gap:8px;color:var(--ml-text-secondary);font-size:12px;white-space:nowrap}.explorer-search input{width:190px;padding:8px 10px;border:1px solid var(--ml-divider);border-radius:6px;background:var(--ml-surface-elevated);color:var(--ml-text-primary)}.classification-path{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:18px 0 12px;font-size:11px;color:var(--ml-text-secondary)}.classification-path button{border:0;padding:0;background:transparent;color:var(--ml-accent);font:inherit;cursor:pointer}.explorer-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.explorer-grid button{min-width:0;min-height:74px;display:grid;grid-template-columns:auto 1fr;gap:4px 9px;align-content:start;text-align:left;padding:11px;border:1px solid var(--ml-divider);border-radius:7px;background:var(--ml-surface-elevated);color:var(--ml-text-primary);cursor:pointer}.explorer-grid button:hover{border-color:var(--ml-accent)}.explorer-grid button strong{color:var(--ml-accent);font-size:12px}.explorer-grid button span{font-size:12px;line-height:1.4;overflow-wrap:anywhere}.explorer-grid button small{grid-column:2;color:var(--ml-text-secondary);font-size:10px}.explorer-grid button:focus-visible,.classification-path button:focus-visible,.explorer-search input:focus-visible{outline:2px solid var(--ml-accent);outline-offset:2px}@media(max-width:1100px){.explorer-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:650px){.explorer-heading{align-items:start;flex-direction:column}.explorer-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.graph-count{margin:5px 0 0;font-size:11px;color:var(--ml-text-secondary)}.node-directory{border-top:1px solid var(--ml-divider);padding-top:14px}.node-directory-heading{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}.node-directory-heading strong{font-size:12px}.node-directory-heading span{font-size:11px;color:var(--ml-text-secondary)}.node-list{display:flex;flex-wrap:wrap;gap:7px;max-height:150px;overflow:auto;margin-top:10px}.node-list button{display:inline-flex;align-items:center;gap:6px;padding:6px 9px;border:1px solid var(--ml-divider);border-radius:6px;color:var(--ml-text-primary);background:var(--ml-surface-elevated);font-size:12px;cursor:pointer}.node-list button.active,.node-list button:hover{border-color:var(--ml-accent);background:var(--ml-surface-selected)}.node-list i{width:7px;height:7px;border-radius:50%;flex:none}.node-list small{color:var(--ml-text-secondary);font-size:10px}.node-actions{display:flex;align-items:center;gap:10px}.relation{border-top:1px solid var(--ml-divider);padding:12px 0}.relation:first-of-type{margin-top:12px}.relation-path{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:13px}.relation-path span{color:var(--ml-accent)}.relation-path small{color:var(--ml-text-secondary);font-size:11px}.relation .explanation{margin:6px 0 0}.unpublished{padding:36px 26px;min-height:180px}.unpublished h2{font-size:18px;margin:0 0 8px}.unpublished p{max-width:540px;font-size:13px}.node-list button:focus-visible,.chain-list button:focus-visible,.classification-list button:focus-visible,.panel-heading button:focus-visible{outline:2px solid var(--ml-accent);outline-offset:2px}
@media(max-width:900px){.catalog{max-height:none}.chain-list{max-height:240px}.classification-list{max-height:220px}}
</style>
