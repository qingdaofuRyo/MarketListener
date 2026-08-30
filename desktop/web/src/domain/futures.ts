export type HeatTimeRange = "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "all";

export interface LongShortHeatWeight {
  breadthWeight: number;
  fundWeight: number;
}

export interface LongShortHeatStateBand {
  min: number;
  max: number;
  label: string;
}

export interface LongShortHeatWeightRange {
  min: number;
  max: number;
  step: number;
}

export interface LongShortHeatCoverage {
  variety?: number;
  fund?: number;
  validVarietyCount?: number;
  missingVarietyCount?: number;
  [key: string]: number | string | undefined;
}

export interface LongShortHeatConfig {
  defaultUserWeight: LongShortHeatWeight;
  userWeight: LongShortHeatWeightRange;
  score: { min: number; max: number };
  stateBands: LongShortHeatStateBand[];
  divergenceThreshold: number;
  fundUnit: string;
  lookbackTradingDays: number;
  halfLife: number;
}

export interface LongShortHeatPoint {
  tradeDate: string;
  upVarietyCount: number;
  downVarietyCount: number;
  flatVarietyCount: number;
  upFund: number | null;
  downFund: number | null;
  flatFund: number | null;
  breadthScore10: number | null;
  fundScore10: number | null;
  divergence: number | null;
  isWarmup: boolean;
  dataQualityStatus: string;
  coverage?: number | LongShortHeatCoverage | null;
}

export interface LongShortHeatResponse {
  available: boolean;
  config: LongShortHeatConfig;
  points: LongShortHeatPoint[];
  latest?: LongShortHeatPoint | null;
  generatedAt?: string | null;
  source?: string | null;
  formulaVersion?: string | null;
  limitations?: string[];
}

export interface LongShortHeatDisplayPoint extends LongShortHeatPoint {
  totalScore10: number | null;
}

export type FuturesStructureRange = "1y" | "3y" | "5y" | "all";

export interface FuturesStructureMember {
  memberKey: string;
  memberName: string;
}

export interface FuturesStructureSeries extends FuturesStructureMember {
  memberCount?: number;
  values: Array<number | null>;
}

export interface FuturesStructureCoverage {
  tradeDate: string;
  inputRowCount: number;
  missingRowCount: number;
  dataQualityStatus: "PASS" | "PARTIAL";
}

export interface FuturesStructureResponse {
  available: boolean;
  chartId: string;
  metric: string | null;
  direction: string;
  unit: string | null;
  baselineDay: string | null;
  baselineVersion: string | null;
  threshold: number | null;
  stackOrder: string[];
  primaryMembers: FuturesStructureMember[];
  otherMembers: FuturesStructureMember[];
  unclassifiedMembers: FuturesStructureMember[];
  dates: string[];
  series: FuturesStructureSeries[];
  totals: number[];
  unclassifiedTotals: number[];
  coverage: FuturesStructureCoverage[];
  formulaVersion: string | null;
  priceBasis: string | null;
  source: string | null;
  updatedAt: string | null;
  limitations: string[];
}

export interface FuturesMemberPositionContract {
  exchange: string;
  productCode: string;
  contractCode: string;
}

export interface FuturesMemberPositionRow {
  exchange: string;
  contractCode: string;
  productCode: string;
  memberKey: string;
  memberName: string;
  longPosition: number | null;
  longPositionChange: number | null;
  longRank: number | null;
  shortPosition: number | null;
  shortPositionChange: number | null;
  shortRank: number | null;
  netPosition: number | null;
  netLongPosition: number | null;
  netShortPosition: number | null;
  sources: string[];
}

export interface FuturesMemberPositionExchangeCoverage {
  exchange: string;
  status: "PASS" | "FAILED" | "UNSUPPORTED" | "LEGACY_UNVERIFIED" | "NOT_COLLECTED";
  contractCount: number;
  recordCount: number;
  sources: string[];
  error: string | null;
  collectedAt: string | null;
}

export interface FuturesMemberPositionResponse {
  available: boolean;
  tradingDay: string | null;
  filters: {
    exchange: string | null;
    contractCode: string | null;
    productCode: string | null;
    commodityOnly: boolean;
  };
  contracts: FuturesMemberPositionContract[];
  rows: FuturesMemberPositionRow[];
  coverage: {
    publishedDirectionRankCount: number;
    memberCount: number;
    exchangeCount: number;
    exchanges: string[];
    exchangeCoverage?: FuturesMemberPositionExchangeCoverage[];
    missingExchanges: string[];
    isComplete: boolean;
    dataQualityStatus?: "PASS" | "PARTIAL" | "UNAVAILABLE";
  };
  limitations: string[];
}

export type FuturesContractSeriesKind = "CONTRACT" | "WEIGHTED";

export interface FuturesContractOption {
  instrumentId: string;
  exchange: string;
  productCode: string;
  contractCode: string;
  name: string;
  seriesKind: FuturesContractSeriesKind;
  lastTradingDay: string | null;
  actualSource: string | null;
  qualityStatus: string | null;
}

export interface FuturesContractListResponse {
  available: boolean;
  tradingDay: string | null;
  filters: {
    exchange: string | null;
    product: string | null;
    seriesKind: FuturesContractSeriesKind | null;
  };
  items: FuturesContractOption[];
  limitations: string[];
}

export interface FuturesContractSeriesPoint {
  tradingDay: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  settlement: number | null;
  openInterest: number | null;
  notionalRmb: number | null;
  basisRmb: number | null;
  basisPercent: number | null;
  unavailable: {
    notional: string;
    basis: string;
  };
}

export interface FuturesContractSeriesResponse {
  available: boolean;
  instrument: FuturesContractOption;
  seriesKind: FuturesContractSeriesKind;
  priceBasis: string | null;
  units: {
    price: string;
    openInterest: string;
    notional: string;
    basis: string | null;
  };
  availability: Record<string, { available: boolean; render: string; reason: string | null }>;
  points: FuturesContractSeriesPoint[];
  source: string;
  updatedAt: string | null;
  limitations: string[];
}

export function finiteScore(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(-100, Math.min(100, value)) : null;
}

export function combineHeatScores(
  breadth: number | null | undefined,
  fund: number | null | undefined,
  breadthWeight: number,
  fundWeight: number,
): number | null {
  const validBreadth = finiteScore(breadth);
  const validFund = finiteScore(fund);
  if (validBreadth === null || validFund === null) return null;
  return Math.max(-100, Math.min(100, validBreadth * breadthWeight + validFund * fundWeight));
}

export function heatStateLabel(
  value: number | null | undefined,
  bands: LongShortHeatStateBand[],
): string {
  const score = finiteScore(value);
  if (score === null) return "暂无数据";
  const match = bands.find((band, index) =>
    score >= band.min && (score < band.max || (index === bands.length - 1 && score <= band.max)),
  );
  return match?.label || "暂无分级";
}

export function signedHeat(value: number | null | undefined, digits = 1): string {
  const score = finiteScore(value);
  if (score === null) return "暂无数据";
  const rounded = Math.abs(score) < 0.5 * 10 ** -digits ? 0 : score;
  return `${rounded > 0 ? "+" : ""}${rounded.toFixed(digits)}`;
}
