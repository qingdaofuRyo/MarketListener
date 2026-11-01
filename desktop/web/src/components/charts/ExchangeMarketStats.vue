<script setup lang="ts">
import { computed } from "vue";
import SeriesChart, { type NamedSeries } from "./SeriesChart.vue";
import SparklineChart from "./SparklineChart.vue";

export interface ExchangeStatPoint { day: string; value: number | null }
export interface ExchangeStatSeries {
  venue: string;
  category: string;
  metric: string;
  unit: string;
  frequency: "DAILY" | "MONTHLY";
  scope: string;
  points: ExchangeStatPoint[];
}
export interface ExchangeCoverage {
  day: string;
  asset: string;
  requiredVenues: string[];
  presentVenues: string[];
  missingVenues: string[];
  failedVenues?: string[];
  status: string;
}
export interface ExchangeOverview {
  available: boolean;
  series: ExchangeStatSeries[];
  coverage: ExchangeCoverage[];
}

const props = defineProps<{ overview?: ExchangeOverview; loading: boolean; error: string }>();
const emit = defineEmits<{ retry: [] }>();

type SeriesRef = [venue: string, category: string, name: string];
interface Headline {
  id: string;
  title: string;
  description: string;
  metric: string;
  unit: string;
  asset: string;
  members: SeriesRef[];
}

const stockHeadlines: Headline[] = [
  { id: "listed-companies", title: "上市公司家数", description: "三所挂牌股票；沪深两所含 B 股公司", metric: "COMPANY_COUNT", unit: "家", asset: "STOCK_LISTED",
    members: [["ALL", "STOCK_LISTED", "三所合计"], ["SSE", "STOCK_ALL", "上交所"], ["SZSE", "STOCK_ALL", "深交所"], ["BSE", "STOCK_A", "北交所"]] },
  { id: "total-shares", title: "股票总股本", description: "三所挂牌股票；沪深两所含 B 股", metric: "TOTAL_SHARES_YI", unit: "亿股", asset: "STOCK_LISTED",
    members: [["ALL", "STOCK_LISTED", "三所合计"], ["SSE", "STOCK_ALL", "上交所"], ["SZSE", "STOCK_ALL", "深交所"], ["BSE", "STOCK_A", "北交所"]] },
  { id: "float-shares", title: "股票流通股本", description: "三所挂牌股票；沪深两所含 B 股", metric: "FLOAT_SHARES_YI", unit: "亿股", asset: "STOCK_LISTED",
    members: [["ALL", "STOCK_LISTED", "三所合计"], ["SSE", "STOCK_ALL", "上交所"], ["SZSE", "STOCK_ALL", "深交所"], ["BSE", "STOCK_A", "北交所"]] },
  { id: "stock-volume", title: "股票成交股数", description: "三所挂牌股票；沪深两所含 B 股", metric: "VOLUME_YI", unit: "亿股", asset: "STOCK_LISTED",
    members: [["ALL", "STOCK_LISTED", "三所合计"], ["SSE", "STOCK_ALL", "上交所"], ["SZSE", "STOCK_ALL", "深交所"], ["BSE", "STOCK_A", "北交所"]] },
];
const fundHeadlines: Headline[] = [
  { id: "fund-cap", title: "沪深基金总市值", description: "上交所＋深交所；北交所日度基金未覆盖", metric: "TOTAL_MARKET_CAP_YI", unit: "亿元", asset: "FUND",
    members: [["SSE_SZSE", "FUND", "沪深合计"], ["SSE", "FUND", "上交所"], ["SZSE", "FUND", "深交所"]] },
  { id: "fund-float-cap", title: "沪深基金流通市值", description: "不重复计入 ETF、LOF、REITs 子类", metric: "FLOAT_MARKET_CAP_YI", unit: "亿元", asset: "FUND",
    members: [["SSE_SZSE", "FUND", "沪深合计"], ["SSE", "FUND", "上交所"], ["SZSE", "FUND", "深交所"]] },
  { id: "fund-turnover", title: "沪深基金成交额", description: "基金父类合计；子类仅用于分解", metric: "TOTAL_AMOUNT_YI", unit: "亿元", asset: "FUND",
    members: [["SSE_SZSE", "FUND", "沪深合计"], ["SSE", "FUND", "上交所"], ["SZSE", "FUND", "深交所"]] },
  { id: "fund-volume", title: "沪深基金成交份数", description: "基金份数，不与股票成交股数相加", metric: "VOLUME_YI", unit: "亿份", asset: "FUND",
    members: [["SSE_SZSE", "FUND", "沪深合计"], ["SSE", "FUND", "上交所"], ["SZSE", "FUND", "深交所"]] },
  { id: "fund-count", title: "沪深基金证券只数", description: "证券数量，不是基金公司家数", metric: "SECURITY_COUNT", unit: "只", asset: "FUND",
    members: [["SSE_SZSE", "FUND", "沪深合计"], ["SSE", "FUND", "上交所"], ["SZSE", "FUND", "深交所"]] },
];
const bondHeadlines: Headline[] = [
  { id: "bond-cash-turnover", title: "三所债券及 ABS 现券成交额", description: "沪市债券＋深市现券及 ABS＋北所公司债现券；不含回购", metric: "TOTAL_AMOUNT_YI", unit: "亿元", asset: "BOND_CASH",
    members: [["ALL", "BOND_CASH", "三所合计"], ["SSE", "BOND", "上交所"], ["SZSE", "BOND_CASH", "深交所现券"], ["SZSE", "BOND_ABS", "深交所 ABS"], ["BSE", "BOND_CASH", "北交所公司债"]] },
  { id: "bond-repo-turnover", title: "沪深债券回购成交额", description: "回购与现券分别统计；北交所未纳入", metric: "TOTAL_AMOUNT_YI", unit: "亿元", asset: "BOND_REPO",
    members: [["SSE_SZSE", "BOND_REPO", "沪深合计"], ["SSE", "BOND_REPO", "上交所"], ["SZSE", "BOND_REPO", "深交所"]] },
  { id: "sse-bond-custody-value", title: "沪市债券托管市值", description: "按市价计量；仅上交所，不与深市债券市价总值相加", metric: "CUSTODY_MARKET_VALUE_YI", unit: "亿元", asset: "BOND_CUSTODY",
    members: [["SSE", "BOND_CUSTODY_ALL", "上交所"]] },
  { id: "sse-bond-custody-face", title: "沪市债券托管面值", description: "面值与市价为两个不同指标", metric: "CUSTODY_FACE_VALUE_YI", unit: "亿元", asset: "BOND_CUSTODY",
    members: [["SSE", "BOND_CUSTODY_ALL", "上交所"]] },
];

interface Detail {
  id?: string;
  venue: string;
  category: string;
  label: string;
  note?: string;
  metrics: string[];
  trend: string;
  onlyWhenAvailable?: boolean;
}
const stockDetails: Detail[] = [
  { venue: "SSE", category: "STOCK_A", label: "上交所 A 股", note: "主板 A＋科创板", metrics: ["TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_MARKET_CAP_YI" },
  { venue: "SZSE", category: "STOCK_A", label: "深交所 A 股", note: "主板 A＋创业板 A", metrics: ["TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_MARKET_CAP_YI" },
  { venue: "BSE", category: "STOCK_A", label: "北交所股票", note: "官网股票日报", metrics: ["TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_MARKET_CAP_YI" },
  { venue: "SSE", category: "STOCK_ALL", label: "沪市挂牌股票", note: "含主板 B 股", metrics: ["COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI"], trend: "COMPANY_COUNT" },
  { venue: "SZSE", category: "STOCK_ALL", label: "深市挂牌股票", note: "含主板 B 股", metrics: ["COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI"], trend: "COMPANY_COUNT" },
  { id: "bse-listed", venue: "BSE", category: "STOCK_A", label: "北所上市公司", note: "北交所股票", metrics: ["COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI"], trend: "COMPANY_COUNT" },
];
const fundDetails: Detail[] = [
  { venue: "SSE", category: "FUND_ETF", label: "沪市 ETF", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SSE", category: "FUND_MONEY", label: "沪市交易型货币基金", note: "2022年9月5日起包含在 ETF 内，展示时不重复相加", metrics: ["VOLUME_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI", onlyWhenAvailable: true },
  { venue: "SSE", category: "FUND_REPO", label: "沪市基金回购", note: "2019年7月22日起不计入基金现货合计；数值按官网显示精度", metrics: ["VOLUME_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI", onlyWhenAvailable: true },
  { venue: "SSE", category: "FUND_LOF", label: "沪市 LOF", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SSE", category: "FUND_REIT", label: "沪市公募 REITs", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "FUND_ETF", label: "深市 ETF", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "FUND_LOF", label: "深市 LOF", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "FUND_REIT", label: "深市不动产基金", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
];
const bondDetails: Detail[] = [
  { venue: "SSE", category: "BOND_GOV", label: "沪市政府债", note: "市场总貌市值为托管面值", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SSE", category: "BOND_CREDIT", label: "沪市信用债", note: "市场总貌市值为托管面值", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "BOND_CASH", label: "深市债券现券", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "BOND_ABS", label: "深市 ABS", metrics: ["SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "SZSE", category: "BOND_REPO", label: "深市债券回购", metrics: ["SECURITY_COUNT", "TOTAL_AMOUNT_YI"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "BSE", category: "BOND_PUBLIC_CORP", label: "北所公开公司债", metrics: ["SECURITY_COUNT", "TOTAL_AMOUNT_YI", "TRADE_COUNT"], trend: "TOTAL_AMOUNT_YI" },
  { venue: "BSE", category: "BOND_PRIVATE_CORP", label: "北所非公开公司债", metrics: ["SECURITY_COUNT", "TOTAL_AMOUNT_YI", "TRADE_COUNT"], trend: "TOTAL_AMOUNT_YI" },
];
const custodyDetails: Detail[] = [
  ["BOND_TREASURY", "国债"], ["BOND_LOCAL_GOV", "地方政府债"], ["BOND_FINANCIAL", "金融债"],
  ["BOND_PUBLIC_CORP", "公开公司债"], ["BOND_PRIVATE_CORP", "非公开公司债"],
  ["BOND_EXCHANGEABLE", "可交换债"], ["BOND_CONVERTIBLE", "可转债"], ["BOND_ABS", "资产支持证券"],
].map(([category, name]) => ({ venue: "SSE", category, label: `沪市${name}`,
  metrics: ["SECURITY_COUNT", "CUSTODY_MARKET_VALUE_YI", "CUSTODY_FACE_VALUE_YI"], trend: "CUSTODY_MARKET_VALUE_YI" }));

const seriesIndex = computed(() => new Map((props.overview?.series || []).map(item => [key(item.venue, item.category, item.metric), item])));
const monthly = computed(() => (props.overview?.series || []).filter(item => item.frequency === "MONTHLY" && item.venue === "BSE"));
const dailyDays = computed(() => [...new Set([
  ...(props.overview?.coverage || []).map(item => item.day),
  ...(props.overview?.series || []).filter(item => item.frequency === "DAILY").flatMap(item => item.points.map(point => point.day)),
])].sort());
function key(venue: string, category: string, metric: string): string { return `${venue}/${category}/${metric}`; }
function get(venue: string, category: string, metric: string): ExchangeStatSeries | undefined {
  return seriesIndex.value.get(key(venue, category, metric));
}
function latest(item?: ExchangeStatSeries): ExchangeStatPoint | undefined {
  return (item?.points || []).reduce<ExchangeStatPoint | undefined>((last, point) =>
    typeof point.value === "number" && Number.isFinite(point.value) && (!last || point.day > last.day)
      ? point : last, undefined);
}
function value(venue: string, category: string, metric: string): number | null {
  return latest(get(venue, category, metric))?.value ?? null;
}
function day(venue: string, category: string, metric: string): string | null {
  return latest(get(venue, category, metric))?.day ?? null;
}
function format(value: number | null, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}
const metricLabels: Record<string, string> = {
  TOTAL_MARKET_CAP_YI: "总市值", FLOAT_MARKET_CAP_YI: "流通市值", TOTAL_AMOUNT_YI: "成交额",
  COMPANY_COUNT: "公司", SECURITY_COUNT: "证券", TOTAL_SHARES_YI: "总股本", FLOAT_SHARES_YI: "流通股本",
  VOLUME_YI: "成交数量", TRADE_COUNT: "成交笔数",
  CUSTODY_MARKET_VALUE_YI: "托管市值", CUSTODY_FACE_VALUE_YI: "托管面值",
  ISSUANCE_YI: "当月发行额", ISSUANCE_YTD_YI: "年内累计发行额",
};
function unit(metric: string, source?: ExchangeStatSeries): string {
  return source?.unit || ({ COMPANY_COUNT: "家", SECURITY_COUNT: "只", TRADE_COUNT: "笔",
    TOTAL_SHARES_YI: "亿股", FLOAT_SHARES_YI: "亿股", VOLUME_YI: "亿股" }[metric] || "亿元");
}
function detailMetricLabel(detail: Detail, metric: string): string {
  if (detail.venue === "SSE" && detail.category.startsWith("BOND_") &&
      metric === "TOTAL_MARKET_CAP_YI") return "托管面值";
  return metricLabels[metric] || metric;
}
function coverage(asset: string): ExchangeCoverage | undefined {
  return (props.overview?.coverage || []).reduce<ExchangeCoverage | undefined>((last, item) =>
    item.asset === asset && (!last || item.day > last.day) ? item : last, undefined);
}
function observedOn(venue: string, category: string, metric: string, targetDay: string): boolean {
  return (get(venue, category, metric)?.points || []).some(point =>
    point.day === targetDay && typeof point.value === "number" && Number.isFinite(point.value));
}
function missingMembers(headline: Headline, targetDay: string): string[] {
  return [...new Set(headline.members.slice(1)
    .filter(([venue, category]) => !observedOn(venue, category, headline.metric, targetDay))
    .map(([venue]) => venue))];
}
function coverageText(headline: Headline): string {
  const latestCoverage = coverage(headline.asset);
  if (!latestCoverage) return "暂无完整来源记录";
  if (latestCoverage.status === "NOT_DUE") return `${latestCoverage.day} 尚待收盘发布`;
  if (headline.members.length < 2) return `${latestCoverage.day} ${latestCoverage.presentVenues.join("＋")}已核验`;
  const missing = missingMembers(headline, latestCoverage.day);
  const failed = (latestCoverage.failedVenues || []).filter(venue => missing.includes(venue));
  if (failed.length) return `${latestCoverage.day} ${failed.join("、")}来源失败；合计待补`;
  if (missing.length) return `${latestCoverage.day} 缺 ${missing.join("、")}；合计待补`;
  const [venue, category] = headline.members[0];
  if (!observedOn(venue, category, headline.metric, latestCoverage.day))
    return `${latestCoverage.day} 同日合计待核`;
  return `${latestCoverage.day} ${latestCoverage.requiredVenues.join("＋")}已核验`;
}
function trend(venue: string, category: string, metric: string): Array<{ t: string; value: number | null }> {
  const source = get(venue, category, metric);
  if (!source) return [];
  const indexed = new Map(source.points.map(point => [point.day, point.value]));
  return dailyDays.value.map(day => ({ t: day, value: indexed.get(day) ?? null }));
}
function chartSeries(headline: Headline): NamedSeries[] {
  const members = headline.members.map(([venue, category, name]) => ({ name, item: get(venue, category, headline.metric) }));
  const days = new Set(members.flatMap(member => member.item?.points.map(point => point.day) || []));
  for (const item of props.overview?.coverage || []) if (item.asset === headline.asset) days.add(item.day);
  const ordered = [...days].sort();
  return members.map(member => {
    const indexed = new Map(member.item?.points.map(point => [point.day, point.value]) || []);
    return { name: member.name, points: ordered.map(day => ({ t: day, value: indexed.get(day) ?? null })) };
  });
}
function headValue(headline: Headline): number | null {
  const [venue, category] = headline.members[0];
  return value(venue, category, headline.metric);
}
function headDay(headline: Headline): string | null {
  const [venue, category] = headline.members[0];
  return day(venue, category, headline.metric);
}
const headlineGroups = computed(() => [
  { id: "data-exchange-stocks", title: "三所股票规模与交易", description: "公司家数、股本和成交股数按三所挂牌股票统计；沪深两所含 B 股。A 股市值与成交额见上方 A 股图表。", headlines: stockHeadlines, details: stockDetails },
  { id: "data-exchange-funds", title: "沪深基金", description: "基金父类含 ETF、LOF、REITs 等；明细不重复计入合计。北交所日度基金暂未覆盖。", headlines: fundHeadlines, details: fundDetails.filter(detail => !detail.onlyWhenAvailable || get(detail.venue, detail.category, detail.trend)) },
  { id: "data-exchange-bonds", title: "债券现券与回购", description: "现券、ABS 和回购分开统计；沪市债券市场总貌“市值”指托管面值，专栏另有托管市价。深市现券市价总值不与其相加。", headlines: bondHeadlines, details: bondDetails },
]);
</script>

<template>
  <div class="exchange-market-stats" data-test="exchange-market-stats">
    <section v-for="group in headlineGroups" :id="group.id" :key="group.id" class="exchange-section" :data-test="group.id">
      <header class="exchange-heading"><div><h2>{{ group.title }}</h2><p>{{ group.description }}</p></div>
        <span v-if="loading" class="exchange-status">正在读取官网统计</span></header>
      <p v-if="error" class="exchange-error" role="status">{{ error }} <button type="button" @click="emit('retry')">重试</button></p>
      <div class="exchange-grid">
        <article v-for="headline in group.headlines" :key="headline.id" class="exchange-card" :data-test="'exchange-headline-' + headline.id">
          <header><h3>{{ headline.title }}</h3><span>{{ headDay(headline) || '暂无观测' }}</span></header>
          <strong>{{ format(headValue(headline)) }}<small>{{ headline.unit }}</small></strong>
          <p>{{ headline.description }}</p>
          <SeriesChart :series="chartSeries(headline)" :unit="headline.unit" :height="190" :area="false" :smooth="false" />
          <p class="exchange-coverage">{{ coverageText(headline) }}</p>
        </article>
      </div>
      <h3 class="exchange-subhead">交易所与类别明细</h3>
      <div class="exchange-details">
        <article v-for="detail in group.details" :key="detail.id || detail.venue + detail.category" class="exchange-detail"
          :data-test="'exchange-detail-' + (detail.id || detail.venue.toLowerCase() + '-' + detail.category.toLowerCase())">
          <div class="detail-name"><strong>{{ detail.label }}</strong><span>趋势：{{ day(detail.venue, detail.category, detail.trend) || '暂无观测' }}</span></div>
          <p v-if="detail.note">{{ detail.note }}</p>
          <div class="detail-metrics"><div v-for="metric in detail.metrics" :key="metric" :data-test="'exchange-metric-' + metric">
            <span>{{ detailMetricLabel(detail, metric) }}</span><strong>{{ format(value(detail.venue, detail.category, metric)) }}<small>{{ unit(metric, get(detail.venue, detail.category, metric)) }}</small></strong>
            <span>{{ day(detail.venue, detail.category, metric) || '暂无观测' }}</span>
          </div></div>
          <SparklineChart :points="trend(detail.venue, detail.category, detail.trend)" :height="56" />
        </article>
      </div>
      <template v-if="group.id === 'data-exchange-bonds'">
        <h3 class="exchange-subhead">上交所债券托管分类</h3>
        <div class="exchange-details" data-test="exchange-sse-bond-custody">
          <article v-for="detail in custodyDetails" :key="detail.category" class="exchange-detail">
            <div class="detail-name"><strong>{{ detail.label }}</strong><span>趋势：{{ day(detail.venue, detail.category, detail.trend) || '暂无观测' }}</span></div>
            <div class="detail-metrics"><div v-for="metric in detail.metrics" :key="metric" :data-test="'exchange-metric-' + metric">
              <span>{{ detailMetricLabel(detail, metric) }}</span><strong>{{ format(value(detail.venue, detail.category, metric)) }}<small>{{ unit(metric, get(detail.venue, detail.category, metric)) }}</small></strong>
              <span>{{ day(detail.venue, detail.category, metric) || '暂无观测' }}</span>
            </div></div>
            <SparklineChart :points="trend(detail.venue, detail.category, detail.trend)" :height="56" />
          </article>
        </div>
      </template>
      <div v-if="group.id === 'data-exchange-bonds'" class="exchange-monthly" data-test="exchange-bond-monthly">
        <h3>北交所债券月度发行</h3><p>国债、地方政府债与公司债发行额按实际公布月份记录，不填入日度现券或债券余额。</p>
        <p v-if="!monthly.length" class="exchange-empty">尚无可核验的月度发行序列。</p>
        <div v-else class="exchange-details">
          <article v-for="item in monthly" :key="key(item.venue, item.category, item.metric)" class="exchange-detail">
            <div class="detail-name"><strong>{{ item.scope || item.category }}</strong><span>{{ latest(item)?.day || '暂无观测' }}</span></div>
            <div class="detail-metrics"><div><span>{{ metricLabels[item.metric] || item.metric }}</span><strong>{{ format(latest(item)?.value ?? null) }}<small>{{ item.unit }}</small></strong></div></div>
            <SparklineChart :points="item.points.map(point => ({ t: point.day, value: point.value }))" :height="56" />
          </article>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.exchange-market-stats { display: grid; gap: 32px; }
.exchange-section { scroll-margin-top: 20px; }
.exchange-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 14px; margin-bottom: 15px; }
.exchange-heading h2 { margin: 0; font-size: 22px; color: var(--ml-text-primary); }
.exchange-heading p, .exchange-card p, .exchange-detail p, .exchange-monthly p { margin: 5px 0 0; color: var(--ml-text-secondary); font-size: 12px; line-height: 1.5; }
.exchange-status, .exchange-coverage { color: var(--ml-text-secondary); font-size: 12px; }
.exchange-error { margin: 0 0 10px; color: var(--ml-warning); }
.exchange-error button { color: var(--ml-accent); background: none; border: 0; cursor: pointer; }
.exchange-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; }
.exchange-card, .exchange-detail, .exchange-monthly { border: 1px solid var(--ml-divider); border-radius: 12px; background: var(--ml-surface); padding: 16px; min-width: 0; }
.exchange-card header, .detail-name { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.exchange-card h3, .exchange-subhead, .exchange-monthly h3 { margin: 0; font-size: 15px; color: var(--ml-text-primary); }
.exchange-card header span, .detail-name span { color: var(--ml-text-secondary); font-size: 12px; white-space: nowrap; }
.exchange-card > strong { display: block; margin: 12px 0 2px; color: var(--ml-text-primary); font-size: 24px; font-variant-numeric: tabular-nums; }
.exchange-card strong small, .detail-metrics small { margin-left: 5px; color: var(--ml-text-secondary); font-size: 11px; font-weight: 400; }
.exchange-card :deep(.series-chart) { margin-top: 10px; }
.exchange-subhead { margin: 22px 0 12px; }
.exchange-details { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.exchange-detail { padding: 13px; }
.detail-name strong { color: var(--ml-text-primary); font-size: 14px; }
.detail-metrics { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 11px; }
.detail-metrics > div { display: grid; gap: 2px; }
.detail-metrics span { color: var(--ml-text-secondary); font-size: 11px; }
.detail-metrics strong { color: var(--ml-text-primary); font-size: 13px; font-variant-numeric: tabular-nums; }
.exchange-detail :deep(.sparkline-chart) { margin-top: 8px; }
.exchange-monthly { margin-top: 18px; }
.exchange-monthly .exchange-details { margin-top: 12px; }
.exchange-empty { padding: 12px 0; }
@media (max-width: 1080px) { .exchange-details { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .exchange-grid, .exchange-details { grid-template-columns: 1fr; } .exchange-heading { display: block; } }
</style>
