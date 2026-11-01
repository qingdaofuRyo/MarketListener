<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { formatMoney, formatRevenue, formatRevenueMoney, formatRevenueShare, textOrNone, type CompanyDetail, type RevenueSegment } from "../domain/company";
import { formatTime } from "../domain/api";

const props = defineProps<{ company: CompanyDetail }>();
const selectedPeriod = ref("latest");
watch(() => props.company.instrumentKey, () => { selectedPeriod.value = "latest"; });

function reportPeriod(segment: RevenueSegment): string | null {
  const value = segment.period?.slice(0, 10);
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null;
}
const periods = computed(() => [...new Set((props.company.revenueSegments || []).map(reportPeriod).filter((x): x is string => x !== null))].sort().reverse());
const shownSegments = computed(() => {
  const rows = props.company.revenueSegments || [];
  const choice = selectedPeriod.value === "latest" ? (periods.value[0] || "unknown") : selectedPeriod.value;
  if (choice === "all") return rows;
  return rows.filter(row => choice === "unknown" ? reportPeriod(row) === null : reportPeriod(row) === choice);
});
function classification(segment: RevenueSegment): string {
  const labels: Record<string, string> = { product: "产品", industry: "行业", region: "地区", business: "业务", project: "项目" };
  return segment.classificationLabel || labels[segment.classification || ""] || "未分类";
}
</script>

<template>
  <section class="company-detail" :aria-label="`${company.name} F10`">
    <header class="company-heading">
      <div>
        <h2>{{ company.name }}</h2>
        <p>{{ company.code }} · {{ company.market }} · {{ company.instrumentKey }}</p>
      </div>
      <el-tag effect="dark">本地 F10</el-tag>
    </header>
    <p v-if="company.companyHighlight" class="company-highlight">{{ company.companyHighlight }}</p>
    <el-descriptions :column="1" border>
      <el-descriptions-item label="总市值">{{ formatMoney(company.totalMarketCap) }}<small v-if="company.totalMarketCap" class="value-source">观测 {{ formatTime(company.totalMarketCap.asOf) }} · {{ company.totalMarketCap.source }}</small></el-descriptions-item>
      <el-descriptions-item label="流通市值">{{ formatMoney(company.floatMarketCap) }}<small v-if="company.floatMarketCap" class="value-source">观测 {{ formatTime(company.floatMarketCap.asOf) }} · {{ company.floatMarketCap.source }}</small></el-descriptions-item>
      <el-descriptions-item label="公司简介">{{ textOrNone(company.companyIntro) }}</el-descriptions-item>
      <el-descriptions-item label="所属行业">{{ textOrNone(company.industry) }}</el-descriptions-item>
      <el-descriptions-item label="证监会行业">{{ textOrNone(company.csrcIndustry) }}</el-descriptions-item>
      <el-descriptions-item label="主营业务">{{ textOrNone(company.mainBusiness) }}</el-descriptions-item>
      <el-descriptions-item label="经营范围">{{ textOrNone(company.businessScope) }}</el-descriptions-item>
      <el-descriptions-item label="收入最高业务">{{ formatRevenue(company.topRevenueSegment) }}<small class="value-source">按最近可核报告期，在币种可比的同类业务中比较；不代表利润最高。</small></el-descriptions-item>
      <el-descriptions-item label="主要产品">
        <template v-if="company.products?.length"><el-tag v-for="product in company.products" :key="product" class="product-tag">{{ product }}</el-tag></template>
        <template v-else>暂无数据</template>
      </el-descriptions-item>
      <el-descriptions-item label="F10 来源">{{ textOrNone(company.source) }}</el-descriptions-item>
      <el-descriptions-item label="公司资料首次记录">{{ formatTime(company.createdAt) }}</el-descriptions-item>
      <el-descriptions-item label="公司资料记录更新时间">{{ formatTime(company.updatedAt) }}</el-descriptions-item>
      <el-descriptions-item label="产业链定位"><template v-if="company.chainLocations?.length"><el-tag v-for="location in company.chainLocations" :key="`${location.chain}-${location.stage}-${location.node}`" class="product-tag">{{ [location.chain, location.stage, location.node].filter(Boolean).join(' / ') }}</el-tag></template><template v-else>暂无数据</template></el-descriptions-item>
    </el-descriptions>
    <section class="revenue-section">
      <div class="revenue-heading"><h3>收入构成</h3><el-select v-if="company.revenueSegments?.length" v-model="selectedPeriod" aria-label="收入构成报告期" class="period-select">
        <el-option label="最新可核报告期" value="latest" />
        <el-option v-for="period in periods" :key="period" :label="period" :value="period" />
        <el-option label="报告期未知" value="unknown" />
        <el-option label="全部报告期" value="all" />
      </el-select></div>
      <p v-if="company.revenueSegments?.length && !periods.length" class="muted">报告期未知，数据正在等待复查。</p>
      <el-table v-if="company.revenueSegments?.length" :data="shownSegments" size="small">
        <el-table-column label="报告期" min-width="122"><template #default="scope">{{ reportPeriod(scope.row) || '未知' }}</template></el-table-column>
        <el-table-column label="分类" width="80"><template #default="scope">{{ classification(scope.row) }}</template></el-table-column>
        <el-table-column prop="name" label="业务" min-width="150" />
        <el-table-column label="采集时间" min-width="168"><template #default="scope">{{ scope.row.fetchedAt ? formatTime(scope.row.fetchedAt) : '未知' }}</template></el-table-column>
        <el-table-column label="占比" width="100"><template #default="scope">{{ formatRevenueShare(scope.row) }}</template></el-table-column>
        <el-table-column label="金额" min-width="180"><template #default="scope">{{ formatRevenueMoney(scope.row.amount || scope.row.amountWithoutTimestamp) }}</template></el-table-column>
      </el-table>
      <p v-else class="muted">暂无结构化收入构成。</p>
    </section>
  </section>
</template>

<style scoped>
.value-source { display: block; color: var(--ml-text-secondary); font-size: 11px; line-height: 1.5; }
.revenue-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.period-select { width: 180px; }
</style>
