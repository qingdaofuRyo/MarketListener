export type StrategyResourceKind =
  "strategy_function" | "indicator" | "strategy";
export type StrategyResourceOrigin =
  "builtin" | "custom" | "community" | "plugin";
export type StrategyResourceStatus =
  "draft" | "active" | "deprecated" | "disabled";
export type StrategyRunMode = "backtest" | "paper" | "live";
export type StrategyUnavailableCode =
  | "INSUFFICIENT_DATA"
  | "UNSUPPORTED_ASSET"
  | "MISSING_FIELD"
  | "MISSING_DATASOURCE"
  | "NO_VOLUME"
  | "INVALID_PARAMETER"
  | "DISABLED";
export type StrategyCapability =
  | "market_data_input"
  | "plot_create"
  | "account_read"
  | "position_read"
  | "order_intent_create";

export interface StrategyResourceReference {
  resourceKind: StrategyResourceKind;
  id: string;
  version: number;
}

export interface StrategyResourceDefinition<
  TDefinition extends object = Record<string, unknown>,
> {
  schemaVersion: 1;
  resourceKind: StrategyResourceKind;
  id: string;
  version: number;
  displayName: string;
  origin: StrategyResourceOrigin;
  supportedAssetTypes: string[];
  status: StrategyResourceStatus;
  createdAt: string;
  updatedAt: string;
  dependencies: StrategyResourceReference[];
  capabilities: StrategyCapability[];
  definition: TDefinition;
}

export interface StrategyAvailability {
  available: boolean;
  code?: StrategyUnavailableCode;
  reason?: string;
}

export interface IndicatorParameterDefinition {
  name: string;
  type: "integer" | "number";
  default: number;
  minimum?: number;
  maximum?: number;
}

export interface IndicatorPlotDefinition {
  id: string;
  type: "line" | "histogram" | "profile";
}

export interface IndicatorPlotStyle {
  color?: string;
  lineWidth?: number;
  lineType?: "solid" | "dashed" | "dotted";
}

export interface IndicatorInstanceStyle extends IndicatorPlotStyle {
  plotStyles?: Record<string, IndicatorPlotStyle>;
}

export interface VolumeProfileBucket {
  bucketLow: number;
  bucketHigh: number;
  volume: number;
  share: number;
  isPoc: boolean;
  isValueArea: boolean;
}

export interface VolumeProfile {
  algorithmVersion: string;
  range: {
    startIndex: number;
    endIndex: number;
    inputBarCount: number;
    rangeSemantics: string;
    firstBarAt?: string | null;
    lastBarAt?: string | null;
  };
  requestedBinCount: number;
  effectiveBinCount: number;
  priceRepresentative: "HLC3";
  allocation: "full_bar_volume_to_hlc3_bucket";
  valueAreaPercent: number;
  totalVolume: number;
  pocBucketIndex: number;
  valueAreaVolume: number;
  buckets: VolumeProfileBucket[];
}

export interface ExternalMarketPoint {
  barIndex: number;
  tradingDate: string;
  value: number;
}

export interface ExternalMarketIndicator {
  externalInstrumentId: string;
  source?: string | null;
  asOfDate?: string | null;
  sourcePointCount: number;
  status: "ready" | "unavailable";
  reason?: string | null;
  coverage: {
    inputBarCount: number;
    matchedBarCount: number;
    sourcePointCount: number;
    firstInputDate?: string | null;
    lastInputDate?: string | null;
  };
  points: ExternalMarketPoint[];
}

export interface IndicatorInstance {
  /** Browser presentation preference; absent older instances span all periods. */
  crossPeriod?: boolean;
  period?: string;
  instanceId: string;
  definitionId: string;
  version: number;
  displayName?: string;
  englishName?: string;
  parameters: Record<string, number>;
  style: IndicatorInstanceStyle;
  visible: boolean;
  placement: "overlay" | "pane";
  plots?: IndicatorPlotDefinition[];
  status?: "pending" | "ready" | "unavailable";
  unavailableCode?: string;
  unavailableReason?: string;
  series?: Record<string, Array<number | null>>;
  profile?: VolumeProfile;
  external?: ExternalMarketIndicator;
}

export interface OrderIntent {
  intentId: string;
  strategyId: string;
  strategyVersion: number;
  instrumentId: string;
  side: "buy" | "sell";
  positionEffect: "open" | "close";
  quantity: number;
  createdAt: string;
}

export type StrategyComparator = "gte" | "lte" | "gt" | "eq" | "lt" | "ne";
export type StrategyOperand =
  | {
      kind: "series";
    field:
        | "open"
        | "high"
        | "low"
        | "close"
        | "volume"
        | "amount"
        | "open_interest"
        | "settlement";
    offset?: number;
    }
  | { kind: "literal"; value: number | boolean | string }
  | { kind: "parameter"; name: string }
  | { kind: "function"; call: StrategyFunctionCall };

export interface StrategyFunctionCall {
  functionId: string;
  version: number;
  arguments: StrategyOperand[];
}

export interface StrategyConditionRule {
  nodeType: "condition";
  left: StrategyFunctionCall;
  comparator?: StrategyComparator;
  right?: StrategyOperand;
}

export interface StrategyGroupRule {
  nodeType: "group";
  operator: "AND" | "OR" | "NOT";
  children: StrategyRule[];
}

export type StrategyRule = StrategyConditionRule | StrategyGroupRule;

export interface VersionedStrategyDefinition {
  schemaVersion: 1;
  id: string;
  version: number;
  displayName: string;
  description?: string;
  origin: "builtin" | "custom";
  supportedAssetTypes: string[];
  status: StrategyResourceStatus;
  baseTimeframe: string;
  universe: {
    instrumentIds?: string[];
    marketTypes?: string[];
    excludeSt?: boolean;
  };
  parameters: Record<
    string,
    {
      type: "integer" | "number" | "boolean" | "string";
      default: unknown;
      minimum?: number;
      maximum?: number;
    }
  >;
  entryRules: StrategyRule;
  exitRules: StrategyRule;
  direction?: "long" | "short";
  positionSizing: {
    kind: "fixed_quantity" | "equity_percent" | "risk_percent";
    value: number;
  };
  stopLoss: {
    enabled: boolean;
    kind: "percent" | "atr_multiple" | "fixed_price";
    value: number;
  };
  takeProfit: {
    enabled: boolean;
    kind: "percent" | "atr_multiple" | "fixed_price";
    value: number;
  };
  scaleOut?: { enabled: boolean; ratioPercent: number };
  pyramiding: { enabled: boolean; maxEntries: number };
  reentry: { enabled: boolean; cooldownBars: number };
  risk: { maxPositionPercent: number; maxDrawdownPercent: number };
  execution: {
    runMode: StrategyRunMode;
    signalTiming: "bar_close";
    fillPrice: "next_open" | "next_close";
  };
  backtest: {
    initialCash: number;
    commissionRate: number;
    slippageRate: number;
  };
  createdAt: string;
  updatedAt: string;
}
