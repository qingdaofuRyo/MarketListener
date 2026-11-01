<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import CompanyDetailPanel from "../components/CompanyDetailPanel.vue";
import { formatMoney, type CompanyDetail, type CompanyPage, type CompanySummary } from "../domain/company";
import { apiGet, formatTime } from "../domain/api";
import ResearchLoadingSkeleton from "./ResearchLoadingSkeleton.vue";

const route = useRoute();
const router = useRouter();
const search = ref("");
const market = ref("");
const loading = ref(false);
const error = ref("");
const page = ref<CompanyPage>({ items: [], total: 0, page: 1, pageSize: 50 });
const detail = ref<CompanyDetail | null>(null);
const detailLoading = ref(false);
const currentPage = ref(1);
type F10Job = {kind:string; status:string; completedAt:string | null; batchMatchesLatest?: boolean; lastBatch?: {status:string; updated:number; remaining:number; failed:number; completedAt:string} | null};
const updateStatus = ref<{ autoEnabled: boolean; completedRevision: string; revenuePublicationPending?: boolean; cnUniverseCache?: {status:string; count:number | null; savedAt:string | null}; hkUniverseFile?: {status:string; savedAt:string | null}; jobs: F10Job[] }>();
const statusError = ref(false);
let listRequest = 0;
let detailRequest = 0;
let statusRequest = 0;
let statusTimer: number | undefined;

const selectedKey = computed(() => String(route.params.instrumentKey || ""));

function jobStatus(job: F10Job): string {
  if (job.status === "RUNNING") return "更新中";
  if (job.status === "QUEUED") return "排队中";
  if (job.status === "NOT_RUN") return "尚未运行";
  if (job.status === "PASS") return "本批完成";
  if (job.status === "FAILED") return "任务失败";
  if (job.status === "CANCELLED") return "任务已取消";
  if (job.status === "PARTIAL_FAILURE" && job.batchMatchesLatest && job.lastBatch) {
    if (job.lastBatch.failed > 0) return "部分失败，待重试";
    if (job.lastBatch.remaining > 0) return "本批完成，待继续";
  }
  return "任务未完成，请查看任务记录";
}

async function retryDetail(key: string, force = false): Promise<CompanyDetail> {
  try {
    return await apiGet<CompanyDetail>(`/api/f10/companies/${encodeURIComponent(key)}`, undefined, { ttlMs: 5 * 60_000, persist: true, force });
  } catch {
    // 本地服务在并行页面巡检时可能短暂重启连接；只对安全的只读详情重试一次。
    await new Promise((resolve) => window.setTimeout(resolve, 180));
    return apiGet<CompanyDetail>(`/api/f10/companies/${encodeURIComponent(key)}`, undefined, { ttlMs: 5 * 60_000, persist: true, force: true });
  }
}

async function loadCompanies(requestedPage = 1) {
  const request = ++listRequest;
  loading.value = true;
  error.value = "";
  try {
    const result = await apiGet<CompanyPage>("/api/f10/companies", { page: requestedPage, page_size: 50, sort: "name", q: search.value.trim() || undefined, market: market.value || undefined }, { ttlMs: 60_000, persist: true, force: true });
    if (request !== listRequest) return;
    page.value = result;
    currentPage.value = result.page;
  } catch (reason) {
    if (request === listRequest) error.value = reason instanceof Error ? reason.message : "企业列表加载失败";
  } finally {
    if (request === listRequest) loading.value = false;
  }
}

async function loadDetail(key = selectedKey.value, force = false) {
  const request = ++detailRequest;
  if (!key) {
    detail.value = null;
    detailLoading.value = false;
    return;
  }
  detailLoading.value = true;
  if (detail.value?.instrumentKey !== key) detail.value = null;
  try {
    const result = await retryDetail(key, force);
    if (request === detailRequest && key === selectedKey.value) detail.value = result;
  } catch (reason) {
    if (request === detailRequest && key === selectedKey.value) error.value = reason instanceof Error ? reason.message : "企业详情加载失败";
  } finally {
    if (request === detailRequest && key === selectedKey.value) detailLoading.value = false;
  }
}

function selectCompany(company: CompanySummary) {
  router.push(`/f10/company/${encodeURIComponent(company.instrumentKey)}`);
}

async function checkUpdateStatus(): Promise<void> {
  const request = ++statusRequest;
  try {
    const current = await apiGet<NonNullable<typeof updateStatus.value>>("/api/f10/update-status", undefined, {force:true});
    if (request !== statusRequest) return;
    const previous = updateStatus.value?.completedRevision;
    updateStatus.value = current; statusError.value = false;
    if (previous !== undefined && previous !== current.completedRevision) {
      void loadCompanies(currentPage.value);
      if (selectedKey.value) void loadDetail(selectedKey.value, true);
    }
  } catch { if (request === statusRequest) statusError.value = true; }
}
function checkVisible(): void { if (document.visibilityState === "visible") void checkUpdateStatus(); }

watch(selectedKey, () => void loadDetail());
onMounted(() => {
  void loadCompanies();
  void loadDetail();
  void checkUpdateStatus();
  statusTimer = window.setInterval(checkVisible, 60_000);
  document.addEventListener("visibilitychange", checkVisible);
});
onBeforeUnmount(() => { statusRequest++; if (statusTimer !== undefined) window.clearInterval(statusTimer); document.removeEventListener("visibilitychange", checkVisible); });
</script>

<template>
  <section class="f10-page">
    <header class="f10-heading"><div><span class="eyebrow">COMPANY RESEARCH</span><h1 class="page-title">F10 企业资料</h1><p class="page-note">查看本机已收录公司资料；资料更新和行情观测可能属于不同日期。</p></div><div class="f10-header-actions"><span class="f10-count">{{ page.total }} 家企业</span><el-button :loading="loading || detailLoading" @click="error = ''; void loadCompanies(currentPage); if (selectedKey) void loadDetail(selectedKey, true)">重新读取本机资料</el-button></div></header>
    <el-alert v-if="error" :title="error" type="warning" show-icon :closable="false" class="page-alert" />
    <p v-if="statusError" class="f10-status" role="status">更新状态暂不可用，已保留本机资料。</p>
    <p v-else-if="updateStatus" class="f10-status" data-test="f10-update-status">{{ updateStatus.autoEnabled ? '后台分批检查公司概况与收入构成' : '后台自动更新已关闭' }}；<span v-for="job in updateStatus.jobs" :key="job.kind">{{ job.kind === 'F10_UPDATE_CN' ? 'A股概况' : job.kind === 'F10_UPDATE_HK' ? '港股概况' : 'A股收入构成' }}：{{ jobStatus(job) }}<template v-if="job.completedAt">（{{ formatTime(job.completedAt) }}）</template>　</span></p>
    <div v-if="updateStatus && !statusError" class="f10-batches" aria-label="最近批次记录"><p v-for="job in updateStatus.jobs.filter(item => item.lastBatch)" :key="job.kind" :data-test="'f10-batch-' + job.kind">{{ job.kind === 'F10_UPDATE_CN' ? 'A股概况' : job.kind === 'F10_UPDATE_HK' ? '港股概况' : 'A股收入构成' }}最近批次：更新 {{ job.lastBatch!.updated }} 家，剩余 {{ job.lastBatch!.remaining }} 家，失败 {{ job.lastBatch!.failed }} 项（{{ formatTime(job.lastBatch!.completedAt) }}）。</p></div>
    <p v-if="updateStatus?.revenuePublicationPending" class="f10-status" role="status" data-test="f10-revenue-publication">收入构成快照发布待检查；后台将尝试恢复本机发布，恢复期间暂停收入采集。</p>
    <p v-if="updateStatus" class="f10-status">每类每轮最多 25 家，优先补缺，再复查超过 90 天的采集记录；90 天是复查门槛，不保证全市场在 90 天内更新。</p>
    <p v-if="updateStatus" class="f10-status">收入构成有待补缺口且上批有进展时，8:00 至 22:00 每两小时最多安排一批；无进展或连续失败会延后重试，服务离线期间不累计批次。</p>
    <p v-if="updateStatus" class="f10-status">收入构成缺少报告期的旧记录将分批复查；最近批次的剩余数量反映当时的检查规则。</p>
    <p v-if="updateStatus?.cnUniverseCache" class="f10-status" data-test="f10-cn-universe">A股名单缓存：<template v-if="updateStatus.cnUniverseCache.count !== null">{{ updateStatus.cnUniverseCache.count }} 条，本机保存于 {{ formatTime(updateStatus.cnUniverseCache.savedAt!) }}{{ updateStatus.cnUniverseCache.status === 'STALE_CACHE' ? '，超过 7 天未更新缓存' : '' }}；缓存时间不代表来源名单的实际发布日期。</template><template v-else>缺失或未通过完整性检查，等待获取可用名单。</template></p>
    <p v-if="updateStatus?.hkUniverseFile" class="f10-status" data-test="f10-hk-universe">港股名单：<template v-if="updateStatus.hkUniverseFile.savedAt">本机文件保存于 {{ formatTime(updateStatus.hkUniverseFile.savedAt) }}；文件时间不代表名单发布日期，也不能证明名单完整，可能不含新上市公司。</template><template v-else>本机名单文件缺失，港股自动更新暂不可用。</template></p>
    <div class="f10-layout">
      <section class="panel company-list-panel">
        <header class="panel-heading"><h2>企业目录</h2><small>搜索、筛选与服务端分页</small></header>
        <form class="company-search" @submit.prevent="loadCompanies(1)">
          <el-input v-model="search" placeholder="搜索公司名称或证券代码" aria-label="搜索企业" clearable />
          <el-select v-model="market" placeholder="全部市场" aria-label="筛选市场" clearable>
            <el-option label="A 股" value="CN" />
            <el-option label="港股" value="HK" />
          </el-select>
          <el-button native-type="submit" type="primary" :loading="loading">搜索</el-button>
        </form>
        <ResearchLoadingSkeleton v-if="loading && !page.items.length" kind="table" :rows="6" />
        <el-table v-else :data="page.items" height="560" :aria-busy="loading" @row-click="selectCompany">
          <el-table-column prop="name" label="企业" min-width="138"><template #default="scope"><button type="button" class="company-open" :aria-label="`查看${scope.row.name}资料`" @click.stop="selectCompany(scope.row)">{{ scope.row.name }}</button><small>{{ scope.row.code }} · {{ scope.row.market }}</small></template></el-table-column>
          <el-table-column prop="industry" label="行业" min-width="128" />
          <el-table-column label="总市值" min-width="175"><template #default="scope">{{ formatMoney(scope.row.totalMarketCap) }}</template></el-table-column>
          <el-table-column label="更新时间" min-width="170"><template #default="scope">{{ formatTime(scope.row.updatedAt) }}</template></el-table-column>
          <template #empty><div class="f10-empty"><strong>没有匹配的企业</strong><p>可调整名称、代码或市场条件，再次搜索。</p><el-button text type="primary" @click="search = ''; market = ''; void loadCompanies(1)">清除筛选</el-button></div></template>
        </el-table>
        <el-pagination v-if="page.total > page.pageSize" class="f10-pagination" background layout="prev, pager, next" :current-page="currentPage" :page-size="page.pageSize" :total="page.total" aria-label="企业目录分页" @current-change="loadCompanies" />
      </section>
      <section class="panel detail-panel" :aria-busy="detailLoading">
        <ResearchLoadingSkeleton v-if="detailLoading && !detail" kind="cards" :rows="2" />
        <CompanyDetailPanel v-if="detail" :company="detail" />
        <div v-else-if="!detailLoading" class="empty-detail"><strong>{{ selectedKey ? '企业资料暂时不可用' : '选择一家企业查看详情' }}</strong><p>{{ selectedKey ? '可重新读取当前企业资料。' : '从左侧目录选择，或通过产业链卡片进入完整 F10。' }}</p><el-button v-if="selectedKey" type="primary" plain @click="loadDetail(selectedKey, true)">重新读取</el-button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.f10-page{max-width:1500px;margin:0 auto;padding:4px 8px 30px}
.f10-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:3px 0 18px;border-bottom:1px solid var(--ml-divider);margin-bottom:18px}
.f10-heading .eyebrow{color:var(--ml-accent);font-size:10px;letter-spacing:.12em;font-weight:700}
.f10-heading h1{margin:4px 0;font-size:clamp(21px,2vw,28px)}
.f10-heading p{margin:0;color:var(--ml-text-secondary);font-size:12px;line-height:1.6}
.f10-count{margin-top:7px;padding:5px 10px;border:1px solid var(--ml-divider);border-radius:999px;background:var(--ml-surface-elevated);color:var(--ml-text-secondary);white-space:nowrap;font-size:11px;font-variant-numeric:tabular-nums}
.f10-header-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.f10-status{margin:0 0 14px;color:var(--ml-text-secondary);font-size:12px;line-height:1.6}
.f10-batches{display:flex;flex-wrap:wrap;gap:4px 18px;margin:-7px 0 12px;color:var(--ml-text-secondary);font-size:11px}.f10-batches p{margin:0;line-height:1.6}
.f10-layout{gap:16px;align-items:start}
.company-list-panel,.detail-panel{border:1px solid var(--ml-divider);border-radius:var(--ml-radius-panel);background:var(--ml-surface);min-width:0}
.panel-heading{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding-bottom:10px;margin-bottom:12px;border-bottom:1px solid var(--ml-divider)}
.panel-heading h2{margin:0;font-size:15px}.panel-heading small{color:var(--ml-text-disabled);font-size:11px}
.company-search{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.company-search .el-input{flex:1 1 210px;min-width:0}.company-search .el-select{flex:0 1 125px}
.company-open{padding:0;border:0;background:transparent;color:var(--ml-text-primary);font:inherit;font-weight:650;text-align:left;cursor:pointer}
.company-open:hover{color:var(--ml-accent);text-decoration:underline;text-underline-offset:3px}.company-open:focus-visible{outline:2px solid var(--ml-accent);outline-offset:3px}
.company-list-panel :deep(.el-table small){display:block;color:var(--ml-text-secondary);font-size:11px}
.company-list-panel :deep(.el-table__row){cursor:pointer}.company-list-panel :deep(.el-table__row:hover){background:var(--ml-surface-selected)}
.f10-pagination{justify-content:flex-end;margin-top:13px}
.f10-empty,.empty-detail{display:grid;justify-items:start;gap:6px;padding:28px;color:var(--ml-text-secondary);font-size:12px}
.f10-empty strong,.empty-detail strong{font-size:15px;color:var(--ml-text-primary)}
.f10-empty p,.empty-detail p{margin:0 0 8px;line-height:1.6}
.detail-panel :deep(.research-skeleton){padding:18px}
@media(max-width:820px){.f10-layout{grid-template-columns:1fr}.f10-heading{flex-wrap:wrap}.f10-count{margin-top:0}}
@media(max-width:480px){.f10-page{padding:0 2px 22px}.company-search .el-select,.company-search .el-button{flex:1 1 42%}.panel-heading small{display:none}}
</style>
