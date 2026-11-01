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
  totalDepositCny?: number | null;
  depositCoverage?: number;
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

export interface ContractTurnoverHeatPoint {
  tradeDate: string;
  totalContractCount: number;
  validContractCount: number;
  missingDirectionCount: number;
  upContractCount: number;
  downContractCount: number;
  flatContractCount: number;
  turnoverValidContractCount: number;
  turnoverMissingContractCount: number;
  upTurnoverCny: number;
  downTurnoverCny: number;
  flatTurnoverCny: number;
  totalDepositCny: number | null;
  countScore10: number | null;
  turnoverScore10: number | null;
  divergence: number | null;
  isWarmup: boolean;
  dataQualityStatus: string;
  coverage: {
    direction: number;
    turnover: number;
    deposit: number;
    missingExchanges: string[];
  };
}

export interface ContractTurnoverHeatResponse {
  available: boolean;
  config: {
    defaultUserWeight: { contractWeight: number; turnoverWeight: number };
    userWeight: LongShortHeatWeightRange;
    score: { min: number; max: number };
    stateBands: LongShortHeatStateBand[];
    lookbackTradingDays: number;
    halfLife: number;
    divergenceThreshold: number;
    minTurnoverCoverage: number;
    turnoverUnit: string;
    depositUnit: string;
  };
  points: ContractTurnoverHeatPoint[];
  latest?: ContractTurnoverHeatPoint | null;
  generatedAt?: string | null;
  source?: string | null;
  formulaVersion?: string | null;
  limitations?: string[];
}

export type FuturesStructureRange = "1y" | "3y" | "5y" | "all";

export interface RankingSeries { key: string; name: string; values: Array<number | null> }
export interface FuturesRankingDirectory {
  available: boolean;
  snapshotStatus?: "CURRENT" | "CACHED_DURING_UPDATE" | "CACHED_AWAITING_REFRESH";
  latestDay: string;
  baselineDay: string;
  dataVersion: string;
  members: Array<{ key: string; name: string; position: number | null }>;
  products: Array<{ key: string; name: string; contracts: string[]; position?: number | null }>;
}
export type FuturesDistributionMetric = "open_interest" | "notional" | "turnover" | "deposit" | "volume" | "return";
export interface FuturesContractInfoSpec {
  sourceUrl: string;
  payloadSha256: string;
  retrievedAt: string;
  publishedOn: string | null;
  effectiveFrom: string | null;
  fields: Record<string, string>;
  sourceKind: string;
  parserVersion: string;
  qualityStatus: string;
}
export interface FuturesContractInfoProduct {
  key: string;
  exchange: string;
  productCode: string;
  name: string;
  qualityStatus: string;
  lastCheckedAt: string;
  error: string | null;
  sourceUrl: string | null;
  listing: { date: string; precision: string; status: string; sourceUrl: string; payloadSha256: string; note: string | null } | null;
  firstObservedDay: string | null;
  monthContractListings: Array<{ contractCode: string; listedOn: string; lastTradingDay: string | null; lastDeliveryDay: string | null; sourceUrl: string }>;
  currentSpec: FuturesContractInfoSpec | null;
  versions: FuturesContractInfoSpec[];
}
export interface FuturesContractInfoResponse {
  products: FuturesContractInfoProduct[];
  coverage: Record<string, number>;
  limitations: string[];
}
export interface FuturesDistributionSeries extends RankingSeries {
  xValues: Array<number | null>;
  yValues: Array<number | null>;
  sizeValues: Array<number | null>;
  tenDayReturnValues?: Array<number | null>;
  tenDayCoverageValues?: Array<number | null>;
  tenDayWindowMinValues?: Array<number | null>;
  tenDayWindowMaxValues?: Array<number | null>;
  directionSumValues?: Array<number | null>;
  directionUpCountValues?: Array<number | null>;
  directionDownCountValues?: Array<number | null>;
  directionFlatCountValues?: Array<number | null>;
  directionWindowCountValues?: Array<number | null>;
  directionStatusValues?: Array<string | null>;
  directionReasonValues?: Array<string | null>;
  shortMove3Values?: Array<number | null>;
  shortRankValues?: Array<number | null>;
  shortSampleCountValues?: Array<number | null>;
  shortCompareStatusValues?: Array<string | null>;
  shortWindowContractCountValues?: Array<number | null>;
  depositReasonValues?: Array<string | null>;
  missingMarginContractCountValues?: Array<number | null>;
  marginPublicationDayValues?: Array<string | null>;
  weeklyStreakValues?: Array<number | null>;
}
export interface FuturesDistributionResponse {
  available: boolean;
  snapshotStatus?: "CURRENT" | "CACHED_DURING_UPDATE" | "CACHED_AWAITING_REFRESH";
  dataVersion?: string;
  metric: FuturesDistributionMetric;
  unit: string;
  dates: string[];
  totals: Array<number | null>;
  series: FuturesDistributionSeries[];
  baselineDay: string | null;
  formulaVersion: string;
  directionFormulaVersion?: string;
  shortMoveFormulaVersion?: string;
  shortRankFormulaVersion?: string;
  windowTradingDays?: number;
  coverage: Array<{ tradeDate: string; dataQualityStatus?: string; missingExchanges?: string[]; reason?: string }>;
  limitations: string[];
}
export interface FuturesPairedBubbleSeries {
  key: string;
  name: string;
  horizontalValues: Array<number | null>;
  verticalValues: Array<number | null>;
  sizeValues: Array<number | null>;
  dailyReturnValues?: Array<number | null>;
  tenDayReturnValues?: Array<number | null>;
  directionUpCountValues?: Array<number | null>;
  directionDownCountValues?: Array<number | null>;
  directionFlatCountValues?: Array<number | null>;
  directionWindowCountValues?: Array<number | null>;
  directionStatusValues?: Array<string | null>;
  directionReasonValues?: Array<string | null>;
  shortMove3Values?: Array<number | null>;
  shortRankValues?: Array<number | null>;
  shortSampleCountValues?: Array<number | null>;
  shortCompareStatusValues?: Array<string | null>;
  coverageValues?: Array<number | null>;
  windowMinValues?: Array<number | null>;
  windowMaxValues?: Array<number | null>;
  shortContractCountValues?: Array<number | null>;
  depositReasonValues?: Array<string | null>;
  missingMarginContractCountValues?: Array<number | null>;
  marginPublicationDayValues?: Array<string | null>;
}
export interface FuturesPairedBubblePayload {
  dates: string[];
  series: FuturesPairedBubbleSeries[];
  xLabel: string;
  yLabel: string;
  sizeLabel: string;
  xUnit: "张" | "亿元";
  yUnit: "亿元" | "%" | "百分点";
  sizeUnit: "张" | "亿元";
  ySigned: boolean;
}
export interface FuturesWeeklyHeatPoint {
  tradeDate: string;
  weekEndDate: string;
  totalScore: number | null;
  breadthScore: number | null;
  streakScore: number | null;
  magnitudeScore: number | null;
  upCount?: number | null;
  downCount?: number | null;
  flatCount?: number | null;
  streakUpCount?: number | null;
  streakDownCount?: number | null;
  upWeightedReturnPct?: number | null;
  downWeightedReturnPct?: number | null;
  upTurnoverCny?: number | null;
  downTurnoverCny?: number | null;
  dataQualityStatus: string;
  pendingWeek?: string | null;
  coverage: { direction: number; turnover: number; missingExchanges: string[];
    dailyMissingExchanges?: string[]; invalidContracts?: string[]; newContractSeeds?: string[] };
}
export interface FuturesWeeklyHeatResponse {
  available: boolean;
  formulaVersion: string;
  config: {
    weights: { breadth: number; streak: number; magnitude: number };
    score: { min: number; max: number };
    stateBands: LongShortHeatStateBand[];
  };
  points: FuturesWeeklyHeatPoint[];
  latest?: FuturesWeeklyHeatPoint | null;
  limitations: string[];
}
export interface FuturesRankingSeriesResponse {
  available: boolean;
  snapshotStatus?: "CURRENT" | "CACHED_DURING_UPDATE" | "CACHED_AWAITING_REFRESH";
  baselineDay: string;
  latestDay: string;
  dataVersion: string;
  dates: string[];
  metrics: Record<string, RankingSeries[]>;
  members: Array<{ key: string; name: string; position: number | null }>;
  products: Array<{ key: string; name: string; contracts: string[]; position?: number | null }>;
  coverage: Array<{ tradeDate: string; missingExchanges: string[] }>;
}

export interface FuturesStructureMember {
  memberKey: string;
  memberName: string;
}

export interface FuturesStructureSeries extends FuturesStructureMember {
  memberCount?: number;
  values: Array<number | null>;
  lineValues?: Array<number | null>;
}

export interface FuturesStructureCoverage {
  tradeDate: string;
  inputRowCount: number;
  missingRowCount: number;
  dataQualityStatus: "PASS" | "PARTIAL";
  officialInputRowCount?: number;
  missingSettlementCount?: number;
  missingOpenInterestCount?: number;
  missingMultiplierCount?: number;
  missingVolumeCount?: number;
  missingExchangeCount?: number;
  missingExchanges?: string[];
  unverifiedVolumeCount?: number;
  unverifiedOpenInterestCount?: number;
  missingProducts?: string[];
  missingProductKeys?: string[];
  missingContracts?: string[];
}

export interface FuturesStructureResponse {
  available: boolean;
  chartId: string;
  metric: string | null;
  direction: string;
  unit: string | null;
  baselineDay: string | null;
  baselineQualityStatus?: "PASS" | "PARTIAL" | null;
  baselineVersion: string | null;
  threshold: number | null;
  rankLimit?: number | null;
  stackOrder: string[];
  primaryMembers: FuturesStructureMember[];
  otherMembers: FuturesStructureMember[];
  unclassifiedMembers: FuturesStructureMember[];
  dates: string[];
  series: FuturesStructureSeries[];
  lineSeries?: FuturesStructureSeries[];
  totals: Array<number | null>;
  unclassifiedTotals: Array<number | null>;
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

export interface FuturesMemberPositionMember {
  exchange: string;
  memberKey: string;
  memberName: string;
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
  netLongPositionChange: number | null;
  netShortPositionChange: number | null;
  sources: string[];
  publishers?: string[];
  sourceUrls?: string[];
  adapters?: string[];
  legacyAdapters?: string[];
  payloadSha256s?: string[];
  qualityStatuses?: string[];
  retrievedAt?: string | null;
}

export interface FuturesMemberPositionExchangeCoverage {
  exchange: string;
  status: "PASS" | "PARTIAL" | "FAILED" | "UNSUPPORTED" | "NO_PUBLICATION" | "DEFERRED" | "LEGACY_UNVERIFIED" | "NOT_COLLECTED";
  contractCount: number;
  recordCount: number;
  sources: string[];
  publishers?: string[];
  adapters?: string[];
  sourceUrls?: string[];
  payloadSha256s?: string[];
  qualityStatuses?: string[];
  qualityStatus?: string;
  retrievedAt?: string | null;
  error: string | null;
  collectedAt: string | null;
}

export interface FuturesMemberPositionResponse {
  available: boolean;
  tradingDay: string | null;
  latestPublishedDay: string | null;
  filters: {
    exchange: string | null;
    contractCode: string | null;
    productCode: string | null;
    memberKey: string | null;
    commodityOnly: boolean;
  };
  contracts: FuturesMemberPositionContract[];
  members: FuturesMemberPositionMember[];
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

export interface FuturesMemberPositionHistoryRow extends FuturesMemberPositionRow {
  tradingDay: string;
  productKey: string;
  productName: string;
  productDisplayName: string;
}

export interface FuturesMemberPositionHistoryResponse {
  available: boolean;
  exchange: string | null;
  productKey: string | null;
  filters: {
    productCode: string | null;
    contractCode: string | null;
    memberKey: string | null;
  };
  dates: string[];
  rows: FuturesMemberPositionHistoryRow[];
  sourceStatusByDay: Record<string, string>;
  sourceStatusByExchange: Record<string, Record<string, string>>;
  limitations: string[];
}

export interface FuturesMemberOverviewProduct {
  productKey: string;
  exchange: string;
  productCode: string;
  displayName: string;
  grossPublishedPosition: number;
  contracts: string[];
}

export interface FuturesMemberOverviewMember {
  memberKey: string;
  memberName: string;
  grossPublishedPosition: number;
  exchanges: string[];
}

export interface FuturesMemberOverviewResponse {
  available: boolean;
  tradingDay: string | null;
  latestPublishedDay: string | null;
  latestObservationDay: string | null;
  latestObservationCoverage: {
    exchangeCoverage: FuturesMemberPositionExchangeCoverage[];
    missingExchanges: string[];
    isComplete: boolean;
    dataQualityStatus: "PASS" | "PARTIAL" | "UNAVAILABLE";
  } | null;
  defaultProductKey: string | null;
  defaultMemberKey: string | null;
  products: FuturesMemberOverviewProduct[];
  members: FuturesMemberOverviewMember[];
  coverage: {
    exchangeCoverage: FuturesMemberPositionExchangeCoverage[];
    missingExchanges: string[];
    isComplete: boolean;
    dataQualityStatus: "PASS" | "PARTIAL" | "UNAVAILABLE";
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
  contractMultiplier: number | null;
  notionalRmb: number | null;
  basisRmb: number | null;
  basisPercent: number | null;
  unavailable: {
    notional: string | null;
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
