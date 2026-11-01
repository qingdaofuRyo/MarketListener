export interface MoneySnapshot {
  value: number;
  currency: string;
  asOf: string;
  source: string;
}

export interface RevenueSegment {
  name: string;
  amount?: MoneySnapshot;
  amountWithoutTimestamp?: {value: number; currency: string};
  ratio?: number;
  revenueSharePct?: number;
  segmentType?: string;
  classification?: string;
  classificationLabel?: string;
  period?: string;
  fetchedAt?: string;
}

export interface CompanySummary {
  instrumentKey: string;
  name: string;
  code: string;
  market: "CN" | "HK";
  companyHighlight?: string;
  totalMarketCap?: MoneySnapshot;
  floatMarketCap?: MoneySnapshot;
  companyIntro?: string;
  industry?: string;
  csrcIndustry?: string;
  mainBusiness?: string;
  topRevenueSegment?: RevenueSegment;
  products?: string[];
  source?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface CompanyDetail extends CompanySummary {
  businessScope?: string;
  revenueSegments?: RevenueSegment[];
  chainLocations?: Array<Record<string, string>>;
  sources?: string[];
  status?: string;
}

export interface CompanyPage {
  items: CompanySummary[];
  total: number;
  page: number;
  pageSize: number;
}

export function textOrNone(value?: string | null): string {
  return value?.trim() || "暂无数据";
}

export function formatMoney(snapshot?: MoneySnapshot): string {
  if (!snapshot || !Number.isFinite(snapshot.value) || snapshot.value <= 0 || !snapshot.currency) {
    return "暂无数据";
  }
  const scale = snapshot.value >= 100_000_000 ? 100_000_000 : 1;
  const amount = snapshot.value / scale;
  const unit = scale === 100_000_000 ? "亿" : "";
  return `${amount.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${unit} ${snapshot.currency}`;
}

export function formatRevenue(segment?: RevenueSegment): string {
  if (!segment?.name) return "暂无数据";
  const percentage = formatRevenueShare(segment);
  const ratio = percentage === "暂无数据" ? "" : ` · ${percentage}`;
  return `${segment.name}${ratio}`;
}

export function formatRevenueShare(segment?: RevenueSegment): string {
  if (typeof segment?.revenueSharePct === "number" && Number.isFinite(segment.revenueSharePct)) {
    return `${segment.revenueSharePct.toFixed(1)}%`;
  }
  if (typeof segment?.ratio === "number" && Number.isFinite(segment.ratio)) {
    const percentage = segment.ratio * 100;
    if (Number.isFinite(percentage)) return `${percentage.toFixed(1)}%`;
  }
  return "暂无数据";
}

export function formatRevenueMoney(snapshot?: Pick<MoneySnapshot, "value" | "currency">): string {
  if (!snapshot || !Number.isFinite(snapshot.value) || !snapshot.currency) return "暂无数据";
  const scale = Math.abs(snapshot.value) >= 100_000_000 ? 100_000_000 : 1;
  const unit = scale === 100_000_000 ? "亿" : "";
  const currency = snapshot.currency === "UNKNOWN" ? "币种未知" : snapshot.currency;
  return `${(snapshot.value / scale).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${unit} ${currency}`;
}
