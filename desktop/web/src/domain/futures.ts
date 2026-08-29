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
