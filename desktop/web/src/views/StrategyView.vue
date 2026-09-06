<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  formatTime,
} from "../domain/api";
import SignalStrategyManager from "../components/strategy/SignalStrategyManager.vue";
const signalManager = ref<InstanceType<typeof SignalStrategyManager>>();
import StrategyRuleTreeEditor from "../components/strategy/StrategyRuleTreeEditor.vue";
import type { StrategyFunctionOption } from "../components/strategy/StrategyOperandEditor.vue";
import type {
  StrategyFunctionCall,
  StrategyOperand,
  StrategyRule,
  VersionedStrategyDefinition,
} from "../domain/strategyTypes";

interface CatalogItem {
  resourceKind?: "indicator" | "drawing_tool" | "strategy_function";
  id: string;
  version?: number;
  versionedId?: string;
  name: string;
  englishName?: string;
  displayName?: string;
  definition?: string;
  description?: string;
  mathFormula?: string;
  formulaSource?: string | null;
  category?: string;
  categoryLabel?: string;
  placement?: string;
  inputs?: Array<{
    name: string;
    type: string;
    default?: unknown;
    minimum?: number;
    maximum?: number;
  }>;
  parameters?: Array<{
    name?: string;
    type?: string;
    default?: unknown;
    minimum?: number;
    maximum?: number;
  }>;
  plots?: Array<{ id?: string; type?: string }>;
  requiredFields?: string[];
  warmupBars?: string;
  limitations?: string[];
  returnType?: string;
  output?: { type?: string };
  supportedAssetTypes?: string[];
  origin?: string;
  status?: string;
  unavailableReason?: string;
  calculationId?: string;
  createdAt?: string;
  updatedAt?: string;
  dependencies?: Array<{ id: string; version: number }>;
  referencedBy?: { indicators?: string[]; strategies?: string[] };
}
interface Strategy {
  strategyId: string;
  version?: number;
  strategyVersion?: string;
  versionedId?: string;
  displayName: string;
  description?: string;
  category?: string;
  origin?: string;
  status?: string;
  enabled?: boolean;
  runMode?: string;
  backtestStatus?: string;
  baseTimeframe?: string;
  supportedAssetTypes?: string[];
  scriptKind: string;
  createdAt?: string;
  updatedAt: string;
  inputs: string[];
  parameters?: Record<string, { default?: unknown }>;
  script?: Record<string, unknown>;
}
interface StrategyVersionHistory {
  id: string;
  version: number;
  displayName: string;
  status: string;
  updatedAt: string;
  baseTimeframe: string;
  supportedAssetTypes: string[];
  parameters: Record<string, { default?: unknown }>;
}
interface StrategyTransferPreview {
  manifest: { packageId: string; signature: Record<string, unknown> | null };
  definition: { id: string; version: number; displayName: string; supportedAssetTypes: string[] };
  dependencyLock: {
    strategy: { id: string; version: number; definitionHash: string };
    strategyFunctions: Array<{ id: string; version: number; definitionHash: string }>;
    indicators: [];
  };
  signatureStatus: "UNSIGNED" | "VALID";
  signatureVerified: boolean;
  androidCompatible: false;
  androidReason: "DECLARATIVE_ANDROID_DSL_REQUIRED";
  conflict: { exists: boolean; id: string; versions: number[] };
}
interface StrategyTemplate {
  templateId: string;
  version: number;
  displayName: string;
  description?: string;
  sourceStrategyId: string | null;
  sourceStrategyVersion: number | null;
  defaultOverrides: Record<string, unknown>;
  disclaimer: string;
  supportedAssetTypes: string[];
  dependencies: Array<{ id: string; version: number }>;
  preview: {
    parameters: Record<string, { default?: unknown }>;
    risk: { maxPositionPercent?: number; maxDrawdownPercent?: number };
    positionSizing: { kind?: string; value?: number };
    stopLoss: { enabled?: boolean; kind?: string; value?: number };
  };
}
interface ExecutionCapabilityMode {
  runMode: "backtest" | "paper" | "live";
  enabled: boolean;
  executionStatus: string;
  adapterId: string | null;
  reason: string;
}
interface ExecutionCapabilities {
  capability: string;
  platform: string;
  androidOrderIntentEnabled: boolean;
  modes: ExecutionCapabilityMode[];
}
interface Condition {
  functionId: string;
  period: string;
  args: number[];
  operator?: string;
  value?: number;
}
interface Group {
  operator: "and" | "or";
  conditions: Condition[];
}

const indicators = ref<CatalogItem[]>([]);
const functions = ref<CatalogItem[]>([]);
const definitions = ref<Strategy[]>([]);
const executionCapabilities = ref<ExecutionCapabilities | null>(null);
const loading = ref(false);
const error = ref("");
const route = useRoute();
const router = useRouter();
const tabs = ["indicator", "function", "strategy"] as const;
const supportedAssetTypes = ["STOCK", "B_SHARE", "INDEX", "FUTURE", "ETF", "LOF", "REIT", "FUND"] as const;
type ResourceTab = (typeof tabs)[number];
const activeTab = ref<ResourceTab>(
  tabs.includes(String(route.query.section) as ResourceTab)
    ? (String(route.query.section) as ResourceTab)
    : "indicator",
);
const loaded = ref<Record<ResourceTab, boolean>>({
  indicator: false,
  function: false,
  strategy: false,
});
const search = ref("");
const category = ref("");
const assetType = ref("");
const sourceFilter = ref("");
const strategySearch = ref("");
const strategyStatus = ref("");
const strategyCategory = ref("");
const strategyAssetType = ref("");
const strategySourceFilter = ref("");
const strategyRunMode = ref("");
const selected = ref<CatalogItem | null>(null);
const detailOpen = ref(false);
const customIndicatorEditorOpen = ref(false);
const customIndicatorEditorMode = ref<"create" | "edit">("create");
const customIndicatorTarget = ref<CatalogItem | null>(null);
const customIndicatorTemplateId = ref("");
const customIndicatorName = ref("");
const customIndicatorDescription = ref("");
const customIndicatorAssetTypes = ref<string[]>([]);
const customIndicatorStatus = ref<"active" | "disabled" | "deprecated">("active");
const customIndicatorDefaults = ref<Record<string, number>>({});
const customIndicatorSaving = ref(false);
function storedFavorites(): string[] {
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem("marketlistener.strategyFavorites") || "[]",
    );
    return Array.isArray(value) &&
      value.every((item) => typeof item === "string")
      ? value
      : [];
  } catch {
    return [];
  }
}
const favoriteIds = ref<string[]>(storedFavorites());
const dialog = ref(false);
const versionHistoryOpen = ref(false);
const versionHistory = ref<StrategyVersionHistory[]>([]);
const versionHistoryStrategy = ref<Strategy | null>(null);
const strategyPackageInput = ref<HTMLInputElement | null>(null);
const packageImportOpen = ref(false);
const packageImportBusy = ref(false);
const packageImportFileName = ref("");
const packageImportBase64 = ref("");
const packagePreview = ref<StrategyTransferPreview | null>(null);
const packageConflict = ref<"cancel" | "rename" | "new_version">("cancel");
const packageNewStrategyId = ref("");
const packageImportError = ref("");
const templateWizardOpen = ref(false);
const templates = ref<StrategyTemplate[]>([]);
const templateCreating = ref("");
const templateDisplayName = ref("");
const mode = ref<"create" | "edit">("create");
const editingId = ref("");
const submitting = ref(false);
const scriptKind = ref<"structured_v1" | "builder_v1" | "python_safe_v1">(
  "structured_v1",
);
const name = ref("");
const period = ref("1d");
const rootOperator = ref<"and" | "or">("and");
const marketTypes = ref<string[]>(["a_share"]);
const excludeSt = ref(false);
const capField = ref<"" | "total_market_cap_yi" | "float_market_cap_yi">("");
const capOperator = ref<"gt" | "lt">("gt");
const capValue = ref<number | undefined>();
const source = ref("value = 1\nsignal = close > ma(close, 20)");
const structuredId = ref("");
const structuredVersion = ref(1);
const structuredCreatedAt = ref("");
const structuredOriginal = ref<VersionedStrategyDefinition | null>(null);
const strategyDirection = ref<"long" | "short">("long");
const positionPercent = ref(20);
const positionSizingKind = ref<"fixed_quantity" | "equity_percent" | "risk_percent">("equity_percent");
const stopLossEnabled = ref(true);
const stopLossValue = ref(5);
const stopLossKind = ref<"percent" | "atr_multiple" | "fixed_price">("percent");
const takeProfitEnabled = ref(true);
const takeProfitValue = ref(10);
const takeProfitKind = ref<"percent" | "atr_multiple" | "fixed_price">("percent");
const scaleOutRatioPercent = ref(0);
const maxDrawdownPercent = ref(20);
const pyramidingEnabled = ref(false);
const pyramidingMaxEntries = ref(1);
const reentryEnabled = ref(true);
const reentryCooldownBars = ref(1);
const commissionRate = ref(0.0003);
const slippageRate = ref(0.0001);
const initialCash = ref(100000);
function series(field: Extract<StrategyOperand, { kind: "series" }>["field"]): StrategyOperand {
  return { kind: "series", field };
}
function literal(value: number): StrategyOperand {
  return { kind: "literal", value };
}
function call(functionId: string, arguments_: StrategyOperand[]): StrategyFunctionCall {
  return { functionId, version: 1, arguments: arguments_ };
}
function defaultStructuredRule(exit: boolean): StrategyRule {
  const average = (lookback: number): StrategyOperand => ({
    kind: "function",
    call: call("technical.sma", [series("close"), literal(lookback)]),
  });
  return {
    nodeType: "group",
    operator: "AND",
    children: [
      {
        nodeType: "condition",
        left: call(exit ? "condition.crossunder" : "condition.crossover", [
          average(20),
          average(60),
        ]),
      },
    ],
  };
}
function cloneRule(rule: StrategyRule): StrategyRule {
  return JSON.parse(JSON.stringify(rule)) as StrategyRule;
}
const entryRules = ref<StrategyRule>(defaultStructuredRule(false));
const exitRules = ref<StrategyRule>(defaultStructuredRule(true));
const ruleValidationErrors = ref<Record<string, string>>({});
const ruleValidationSummary = ref("");
const ruleHistory = ref<Array<{ entry: StrategyRule; exit: StrategyRule }>>([]);
const groups = ref<Group[]>([
  {
    operator: "and",
    conditions: [
      {
        functionId: "period_return",
        period: "1d",
        args: [20],
        operator: "gt",
        value: 0.05,
      },
    ],
  },
]);
const marketOptions = [
  ["a_share", "A 股"],
  ["hk_stock", "港股"],
  ["main_board", "沪深主板"],
  ["chinext", "创业板"],
  ["star", "科创板"],
  ["etf", "ETF"],
  ["bse", "北证"],
  ["cn_future", "国内期货"],
  ["cn_commodity_index", "国内商品指数"],
  ["global_future", "国外期货"],
] as const;
const arity: Record<string, number> = {
  period_return: 1,
  no_limit_up: 1,
  no_limit_down: 1,
  limit_up_count: 1,
  limit_down_count: 1,
  close_new_high: 1,
  close_new_low: 1,
  up_count: 1,
  down_count: 1,
  up_down_ratio: 1,
  down_up_ratio: 1,
  range_high_low_ratio: 1,
  range_low_high_ratio: 1,
  volume_slope: 2,
  gann_rising_rate: 1,
  gann_falling_rate: 1,
  hsar_resistance: 2,
  hsar_support: 2,
};
const booleanFunctions = new Set([
  "no_limit_up",
  "no_limit_down",
  "close_new_high",
  "close_new_low",
]);
const functionChoices = computed(() =>
  functions.value.filter((item) => item.id in arity),
);
const activeCatalog = computed(() =>
  activeTab.value === "indicator" ? indicators.value : functions.value,
);
const copyableIndicatorTemplates = computed(() =>
  indicators.value.filter(
    (item) => item.resourceKind === "indicator" && item.status === "active",
  ),
);
const categories = computed<Array<[string, string]>>(() =>
  Array.from(
    new Map<string, string>(
      activeCatalog.value
        .filter((item) => item.category)
        .map((item) => [
          item.category as string,
          item.categoryLabel || (item.category as string),
        ]),
    ).entries(),
  ),
);
const strategyCategories = computed(() =>
  Array.from(
    new Set(
      definitions.value
        .map((item) => item.category)
        .filter((value): value is string => Boolean(value)),
    ),
  ).sort(),
);
const filteredCatalog = computed(() =>
  activeCatalog.value.filter((item) => {
    const term = search.value.trim().toLocaleLowerCase();
    return (
      (!term ||
        `${item.name} ${item.englishName || ""} ${item.description || item.definition || ""}`
          .toLocaleLowerCase()
          .includes(term)) &&
      (!category.value || item.category === category.value) &&
      (!assetType.value ||
        item.supportedAssetTypes?.includes(assetType.value)) &&
      (!sourceFilter.value ||
        sourceFilter.value === "favorite" ||
        item.origin === sourceFilter.value)
    );
  }),
);
const filteredDefinitions = computed(() =>
  definitions.value.filter((item) => {
    const term = strategySearch.value.trim().toLocaleLowerCase();
    return (
      (!term ||
        `${item.displayName} ${item.description || ""}`
          .toLocaleLowerCase()
          .includes(term)) &&
      (!strategyStatus.value || item.status === strategyStatus.value) &&
      (!strategyCategory.value || item.category === strategyCategory.value) &&
      (!strategyAssetType.value ||
        item.supportedAssetTypes?.includes(strategyAssetType.value)) &&
      (!strategyRunMode.value || item.runMode === strategyRunMode.value) &&
      (!strategySourceFilter.value ||
        (strategySourceFilter.value === "favorite"
          ? isFavorite(item)
          : item.origin === strategySourceFilter.value))
    );
  }),
);

function reset(): void {
  name.value = "";
  period.value = "1d";
  scriptKind.value = "structured_v1";
  structuredId.value = "";
  structuredVersion.value = 1;
  structuredCreatedAt.value = "";
  structuredOriginal.value = null;
  strategyDirection.value = "long";
  entryRules.value = defaultStructuredRule(false);
  exitRules.value = defaultStructuredRule(true);
  ruleValidationErrors.value = {};
  ruleValidationSummary.value = "";
  ruleHistory.value = [];
  positionPercent.value = 20;
  positionSizingKind.value = "equity_percent";
  stopLossEnabled.value = true;
  stopLossValue.value = 5;
  stopLossKind.value = "percent";
  takeProfitEnabled.value = true;
  takeProfitValue.value = 10;
  takeProfitKind.value = "percent";
  scaleOutRatioPercent.value = 0;
  maxDrawdownPercent.value = 20;
  pyramidingEnabled.value = false;
  pyramidingMaxEntries.value = 1;
  reentryEnabled.value = true;
  reentryCooldownBars.value = 1;
  commissionRate.value = 0.0003;
  slippageRate.value = 0.0001;
  initialCash.value = 100000;
  rootOperator.value = "and";
  marketTypes.value = ["a_share"];
  excludeSt.value = false;
  capField.value = "";
  capOperator.value = "gt";
  capValue.value = undefined;
  source.value = "value = 1\nsignal = close > ma(close, 20)";
  groups.value = [
    {
      operator: "and",
      conditions: [
        {
          functionId: "period_return",
          period: "1d",
          args: [20],
          operator: "gt",
          value: 0.05,
        },
      ],
    },
  ];
  editingId.value = "";
}
function condition(functionId = "period_return"): Condition {
  return {
    functionId,
    period: "1d",
    args: Array.from({ length: arity[functionId] ?? 1 }, () =>
      functionId.includes("hsar") ? 20 : 10,
    ),
    operator: booleanFunctions.has(functionId) ? undefined : "gt",
    value: 0,
  };
}
function updateFunction(row: Condition): void {
  row.args = Array.from(
    { length: arity[row.functionId] ?? 1 },
    (_item, index) =>
      index === 1 && row.functionId.includes("hsar") ? 20 : 10,
  );
  row.operator = booleanFunctions.has(row.functionId) ? undefined : "gt";
  row.value = booleanFunctions.has(row.functionId) ? undefined : 0;
}
function addGroup(): void {
  groups.value.push({ operator: "and", conditions: [condition()] });
}
async function openCreate(): Promise<void> {
  mode.value = "create";
  reset();
  await loadTab("function");
  dialog.value = true;
}
async function openTemplateWizard(): Promise<void> {
  templateCreating.value = "";
  templateDisplayName.value = "";
  try {
    templates.value = (await apiGet<{ items: StrategyTemplate[] }>("/api/strategy/templates", undefined, { force: true })).items;
    templateWizardOpen.value = true;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略模板加载失败";
  }
}
async function createFromTemplate(template: StrategyTemplate): Promise<void> {
  templateCreating.value = template.templateId;
  error.value = "";
  try {
    const result = await apiPost<{ definition: VersionedStrategyDefinition }>(
      `/api/strategy/templates/${encodeURIComponent(template.templateId)}/create`,
      templateDisplayName.value.trim() ? { displayName: templateDisplayName.value.trim() } : {},
    );
    templateWizardOpen.value = false;
    await load(true);
    await openEdit({
      strategyId: result.definition.id,
      version: result.definition.version,
      strategyVersion: String(result.definition.version),
      versionedId: `${result.definition.id}@${result.definition.version}`,
      displayName: result.definition.displayName,
      description: result.definition.description,
      origin: "custom",
      status: result.definition.status,
      baseTimeframe: result.definition.baseTimeframe,
      supportedAssetTypes: result.definition.supportedAssetTypes,
      scriptKind: "structured_v1",
      createdAt: result.definition.createdAt,
      updatedAt: result.definition.updatedAt,
      inputs: [],
      parameters: result.definition.parameters,
    });
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "模板创建失败";
  } finally {
    templateCreating.value = "";
  }
}
async function openEdit(item: Strategy): Promise<void> {
  mode.value = "edit";
  reset();
  editingId.value = item.strategyId;
  name.value = item.displayName;
  await loadTab("function");
  if (item.scriptKind === "structured_v1") {
    try {
      const [data, versions] = await Promise.all([
        apiGet<VersionedStrategyDefinition>(
          `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}`,
          { version: item.version },
          { force: true },
        ),
        apiGet<{ items: StrategyVersionHistory[] }>(
          `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}/versions`,
          undefined,
          { force: true },
        ),
      ]);
      scriptKind.value = "structured_v1";
      structuredId.value = item.strategyId;
      structuredVersion.value = Math.max(...versions.items.map((value) => value.version)) + 1;
      structuredCreatedAt.value = data.createdAt;
      structuredOriginal.value = data;
      period.value = String(data.baseTimeframe || "1d");
      strategyDirection.value = data.direction || "long";
      marketTypes.value = data.universe?.marketTypes || [];
      excludeSt.value = Boolean(data.universe?.excludeSt);
      positionPercent.value = Number(data.positionSizing?.value || 20);
      positionSizingKind.value = data.positionSizing.kind;
      stopLossEnabled.value = Boolean(data.stopLoss?.enabled);
      stopLossValue.value = Number(data.stopLoss?.value || 5);
      stopLossKind.value = data.stopLoss.kind;
      takeProfitEnabled.value = Boolean(data.takeProfit?.enabled);
      takeProfitValue.value = Number(data.takeProfit?.value || 10);
      takeProfitKind.value = data.takeProfit.kind;
      scaleOutRatioPercent.value = data.scaleOut?.enabled
        ? Number(data.scaleOut.ratioPercent)
        : 0;
      maxDrawdownPercent.value = Number(data.risk.maxDrawdownPercent || 20);
      pyramidingEnabled.value = Boolean(data.pyramiding.enabled);
      pyramidingMaxEntries.value = Number(data.pyramiding.maxEntries || 1);
      reentryEnabled.value = Boolean(data.reentry.enabled);
      reentryCooldownBars.value = Number(data.reentry.cooldownBars || 1);
      commissionRate.value = Number(data.backtest?.commissionRate || 0);
      slippageRate.value = Number(data.backtest?.slippageRate || 0);
      initialCash.value = Number(data.backtest?.initialCash || 100000);
      entryRules.value = cloneRule(data.entryRules);
      exitRules.value = cloneRule(data.exitRules);
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : "策略加载失败";
    }
    dialog.value = true;
    return;
  }
  try {
    const data = await apiGet<Strategy>(
      `/api/strategy/definitions/${encodeURIComponent(item.strategyId)}`,
      undefined,
      { force: true },
    );
    const script = data.script ?? {};
    if (data.scriptKind === "builder_v1") {
      scriptKind.value = "builder_v1";
      period.value = String(script.period || "1d");
      const universe = script.universe as
        | {
            market_types?: string[];
            exclude_st?: boolean;
            total_market_cap_yi?: { operator?: string; value?: number };
            float_market_cap_yi?: { operator?: string; value?: number };
          }
        | undefined;
      marketTypes.value = universe?.market_types ?? [];
      excludeSt.value = Boolean(universe?.exclude_st);
      const cap =
        universe?.total_market_cap_yi ?? universe?.float_market_cap_yi;
      capField.value = universe?.total_market_cap_yi
        ? "total_market_cap_yi"
        : universe?.float_market_cap_yi
          ? "float_market_cap_yi"
          : "";
      capOperator.value = cap?.operator === "lt" ? "lt" : "gt";
      capValue.value = typeof cap?.value === "number" ? cap.value : undefined;
      const tree = script.condition_tree as
        | {
            operator?: "and" | "or";
            children?: Array<{
              operator?: "and" | "or";
              children?: Condition[];
            }>;
          }
        | undefined;
      rootOperator.value = tree?.operator ?? "and";
      groups.value = (tree?.children ?? [])
        .filter((group) => group.children)
        .map((group) => ({
          operator: group.operator ?? "and",
          conditions: (group.children ?? []).map((row) => ({
            ...row,
            period: row.period || String(script.period || "1d"),
          })),
        }));
      if (!groups.value.length) addGroup();
    } else {
      scriptKind.value = "python_safe_v1";
      period.value = String(script.period || "1d");
      source.value = String(script.source || script.expression || source.value);
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略加载失败";
  }
  dialog.value = true;
}
async function openVersionHistory(item: Strategy): Promise<void> {
  if (item.scriptKind !== "structured_v1") return;
  try {
    const result = await apiGet<{ items: StrategyVersionHistory[] }>(
      `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}/versions`,
      undefined,
      { force: true },
    );
    versionHistoryStrategy.value = item;
    versionHistory.value = result.items;
    versionHistoryOpen.value = true;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "历史版本加载失败";
  }
}
function openPackageChooser(): void {
  strategyPackageInput.value?.click();
}
function readPackageBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("策略包读取失败"));
    reader.onload = () => {
      const dataUrl = typeof reader.result === "string" ? reader.result : "";
      const separator = dataUrl.indexOf(",");
      if (separator < 0) reject(new Error("策略包编码失败"));
      else resolve(dataUrl.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}
async function previewStrategyPackage(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    error.value = "策略包超过 5 MiB 限制";
    return;
  }
  packageImportBusy.value = true;
  packageImportError.value = "";
  packagePreview.value = null;
  try {
    packageImportFileName.value = file.name;
    packageImportBase64.value = await readPackageBase64(file);
    packagePreview.value = await apiPost<StrategyTransferPreview>("/api/strategy/packages/preview", {
      packageBase64: packageImportBase64.value,
    });
    packageConflict.value = packagePreview.value.conflict.exists ? "cancel" : "new_version";
    packageNewStrategyId.value = "";
    packageImportOpen.value = true;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略包验证失败";
  } finally {
    packageImportBusy.value = false;
  }
}
async function importStrategyPackage(): Promise<void> {
  if (!packagePreview.value || !packageImportBase64.value) return;
  if (packagePreview.value.conflict.exists && packageConflict.value === "cancel") {
    packageImportError.value = "目标已有同 ID 策略；请选择“改名导入”或“作为新版本”。";
    return;
  }
  if (packageConflict.value === "rename" && !packageNewStrategyId.value.trim()) {
    packageImportError.value = "请填写新的策略 ID";
    return;
  }
  packageImportBusy.value = true;
  packageImportError.value = "";
  try {
    await apiPost("/api/strategy/packages/import", {
      packageBase64: packageImportBase64.value,
      conflict: packageConflict.value,
      ...(packageConflict.value === "rename" ? { newStrategyId: packageNewStrategyId.value.trim() } : {}),
    });
    packageImportOpen.value = false;
    await load(true);
  } catch (reason) {
    packageImportError.value = reason instanceof Error ? reason.message : "策略包导入失败";
  } finally {
    packageImportBusy.value = false;
  }
}
function exportStrategyPackage(item: Strategy): void {
  const version = item.version || Number(item.strategyVersion || 1);
  const link = document.createElement("a");
  link.href = `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}/export?version=${encodeURIComponent(String(version))}`;
  link.download = `${item.strategyId}@${version}.strategy.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}
function historyStrategy(version: StrategyVersionHistory): Strategy | null {
  const base = versionHistoryStrategy.value;
  if (!base) return null;
  return {
    ...base,
    version: version.version,
    strategyVersion: String(version.version),
    versionedId: `${version.id}@${version.version}`,
    displayName: version.displayName,
    status: version.status,
    baseTimeframe: version.baseTimeframe,
    supportedAssetTypes: version.supportedAssetTypes,
    parameters: version.parameters,
    scriptKind: "structured_v1",
  };
}
function loadHistoricalVersionToChart(version: StrategyVersionHistory): void {
  const item = historyStrategy(version);
  if (!item) return;
  versionHistoryOpen.value = false;
  loadStrategyToCurrentChart(item);
}
async function editHistoricalVersion(version: StrategyVersionHistory): Promise<void> {
  const item = historyStrategy(version);
  if (!item) return;
  versionHistoryOpen.value = false;
  await openEdit(item);
}
function tree(): object {
  const children: object[] = [];
  if (marketTypes.value.length)
    children.push({
      functionId: "market_scope",
      marketTypes: marketTypes.value,
    });
  for (const group of groups.value)
    children.push({
      operator: group.operator,
      children: group.conditions.map((row) => ({
        functionId: row.functionId,
        period: row.period,
        args: row.args,
        ...(row.operator ? { operator: row.operator, value: row.value } : {}),
      })),
    });
  return { operator: rootOperator.value, children };
}
function script(): Record<string, unknown> {
  const universe = {
    market_types: marketTypes.value,
    ...(excludeSt.value ? { exclude_st: true } : {}),
    ...(capField.value && capValue.value != null
      ? {
          [capField.value]: {
            operator: capOperator.value,
            value: capValue.value,
          },
        }
      : {}),
  };
  return scriptKind.value === "builder_v1"
    ? { period: "1d", universe, conditionTree: tree() }
    : { period: period.value, universe, source: source.value };
}
const visualFunctionCatalog = computed<StrategyFunctionOption[]>(() =>
  functions.value.map((item) => ({
    id: item.id,
    version: item.version,
    name: item.name,
    inputs: item.inputs,
    output: item.output,
    supportedAssetTypes: item.supportedAssetTypes,
  })),
);
const strategySupportedAssetTypes = computed(() => {
  const assets = new Set<string>();
  for (const marketType of marketTypes.value) {
    if (["cn_future", "global_future"].includes(marketType)) assets.add("FUTURE");
    else if (marketType === "etf") assets.add("ETF");
    else if (marketType === "cn_commodity_index") assets.add("INDEX");
    else assets.add("STOCK");
  }
  return assets.size ? [...assets] : ["STOCK"];
});
function pushRuleHistory(): void {
  ruleHistory.value = [
    ...ruleHistory.value.slice(-49),
    { entry: cloneRule(entryRules.value), exit: cloneRule(exitRules.value) },
  ];
}
function updateEntryRules(value: StrategyRule): void {
  pushRuleHistory();
  entryRules.value = cloneRule(value);
  ruleValidationErrors.value = {};
  ruleValidationSummary.value = "";
}
function updateExitRules(value: StrategyRule): void {
  pushRuleHistory();
  exitRules.value = cloneRule(value);
  ruleValidationErrors.value = {};
  ruleValidationSummary.value = "";
}
function undoRuleEdit(): void {
  const previous = ruleHistory.value.at(-1);
  if (!previous) return;
  ruleHistory.value = ruleHistory.value.slice(0, -1);
  entryRules.value = cloneRule(previous.entry);
  exitRules.value = cloneRule(previous.exit);
  ruleValidationErrors.value = {};
  ruleValidationSummary.value = "";
}
function ruleDependencies(): string[] {
  const result = new Set<string>();
  const visitOperand = (operand: StrategyOperand): void => {
    if (operand.kind !== "function") return;
    visitCall(operand.call);
  };
  const visitCall = (value: StrategyFunctionCall): void => {
    result.add(`${value.functionId}@${value.version}`);
    value.arguments.forEach(visitOperand);
  };
  const visitRule = (rule: StrategyRule): void => {
    if (rule.nodeType === "group") rule.children.forEach(visitRule);
    else {
      visitCall(rule.left);
      if (rule.right) visitOperand(rule.right);
    }
  };
  visitRule(entryRules.value);
  visitRule(exitRules.value);
  return [...result].sort();
}
const structuredDependencies = computed(() => ruleDependencies());
function validateRuleTrees(): boolean {
  const errors: Record<string, string> = {};
  const definitions = new Map(
    visualFunctionCatalog.value.map((item) => [item.id, item]),
  );
  const supportsAssets = (id: string, path: string): void => {
    const definition = definitions.get(id);
    if (!definition) {
      errors[path] = `未在函数注册表中找到 ${id}`;
      return;
    }
    if (
      !strategySupportedAssetTypes.value.every((asset) =>
        definition.supportedAssetTypes?.includes(asset),
      )
    )
      errors[path] = `${definition.name} 不适用于当前选择的资产类型`;
  };
  const visitOperand = (operand: StrategyOperand, path: string): void => {
    if (operand.kind === "function") visitCall(operand.call, path);
  };
  const visitCall = (value: StrategyFunctionCall, path: string): void => {
    supportsAssets(value.functionId, path);
    const definition = definitions.get(value.functionId);
    if (definition && value.arguments.length !== (definition.inputs || []).length)
      errors[path] = `${definition.name} 的参数数量与已发布签名不一致`;
    value.arguments.forEach((argument, index) =>
      visitOperand(argument, `${path}.arguments.${index}`),
    );
  };
  const visitRule = (rule: StrategyRule, path: string): void => {
    if (rule.nodeType === "group") {
      if (!rule.children.length) errors[path] = "规则组至少需要一个子节点";
      if (rule.operator === "NOT" && rule.children.length !== 1)
        errors[path] = "NOT 规则组必须且只能包含一个子节点";
      rule.children.forEach((child, index) => visitRule(child, `${path}.${index}`));
      return;
    }
    visitCall(rule.left, path);
    const output = definitions.get(rule.left.functionId)?.output?.type;
    const booleanOutput = output === "boolean" || output === "series<boolean>";
    if (!booleanOutput && (!rule.comparator || !rule.right))
      errors[path] = "数值函数必须选择比较运算和右侧操作数";
    if (booleanOutput && (rule.comparator || rule.right))
      errors[path] = "布尔函数不应再添加数值比较";
    if (rule.right) visitOperand(rule.right, `${path}.right`);
  };
  visitRule(entryRules.value, "entryRules");
  visitRule(exitRules.value, "exitRules");
  ruleValidationErrors.value = errors;
  ruleValidationSummary.value = Object.keys(errors).length
    ? `发现 ${Object.keys(errors).length} 个规则节点错误`
    : "规则树本地校验通过；保存时仍由服务端按精确版本复核。";
  return !Object.keys(errors).length;
}
function wireOperand(operand: StrategyOperand): object {
  if (operand.kind === "function")
    return { kind: "function", call: wireCall(operand.call) };
  if (operand.kind === "series")
    return { kind: "series", field: operand.field, ...(operand.offset ? { offset: operand.offset } : {}) };
  if (operand.kind === "parameter") return { kind: "parameter", name: operand.name };
  return { kind: "literal", value: operand.value };
}
function wireCall(value: StrategyFunctionCall): object {
  return {
    function_id: value.functionId,
    version: value.version,
    arguments: value.arguments.map(wireOperand),
  };
}
function wireRule(rule: StrategyRule): object {
  if (rule.nodeType === "group")
    return {
      node_type: "group",
      operator: rule.operator,
      children: rule.children.map(wireRule),
    };
  return {
    node_type: "condition",
    left: wireCall(rule.left),
    ...(rule.comparator ? { comparator: rule.comparator } : {}),
    ...(rule.right ? { right: wireOperand(rule.right) } : {}),
  };
}
async function validateStructuredDefinition(): Promise<boolean> {
  if (!validateRuleTrees()) return false;
  try {
    await apiPost("/api/strategy/definition/validate", structuredDocument());
    ruleValidationSummary.value = "规则树已通过服务端 AST/版本/资产校验。";
    return true;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "规则校验失败";
    return false;
  }
}
function structuredDocument(): Record<string, unknown> {
  const now = new Date().toISOString();
  const id = structuredId.value || `strategy.user_${Date.now().toString(36)}`;
  structuredId.value = id;
  const original = structuredOriginal.value;
  return {
    schema_version: 1,
    id,
    version: structuredVersion.value,
    display_name: name.value.trim(),
    description: original?.description || "结构化规则策略",
    origin: "custom",
    supported_asset_types: strategySupportedAssetTypes.value,
    status: original?.status || "active",
    base_timeframe: period.value,
    universe: {
      ...(original?.universe.instrumentIds?.length
        ? { instrument_ids: original.universe.instrumentIds }
        : {}),
      market_types: marketTypes.value,
      exclude_st: excludeSt.value,
    },
    parameters: original?.parameters || {},
    entry_rules: wireRule(entryRules.value),
    exit_rules: wireRule(exitRules.value),
    direction: strategyDirection.value,
    position_sizing: { kind: positionSizingKind.value, value: positionPercent.value },
    stop_loss: {
      enabled: stopLossEnabled.value,
      kind: stopLossKind.value,
      value: stopLossValue.value,
    },
    take_profit: {
      enabled: takeProfitEnabled.value,
      kind: takeProfitKind.value,
      value: takeProfitValue.value,
    },
    scale_out: {
      enabled: scaleOutRatioPercent.value > 0,
      ratio_percent: Math.max(1, scaleOutRatioPercent.value),
    },
    pyramiding: { enabled: pyramidingEnabled.value, max_entries: pyramidingMaxEntries.value },
    reentry: { enabled: reentryEnabled.value, cooldown_bars: reentryCooldownBars.value },
    risk: {
      max_position_percent: positionPercent.value,
      max_drawdown_percent: maxDrawdownPercent.value,
    },
    execution: original
      ? {
          run_mode: original.execution.runMode,
          signal_timing: original.execution.signalTiming,
          fill_price: original.execution.fillPrice,
        }
      : {
          run_mode: "backtest",
          signal_timing: "bar_close",
          fill_price: "next_open",
        },
    backtest: {
      initial_cash: initialCash.value,
      commission_rate: commissionRate.value,
      slippage_rate: slippageRate.value,
    },
    created_at: structuredCreatedAt.value || now,
    updated_at: now,
  };
}
async function save(): Promise<void> {
  if (!name.value.trim()) {
    error.value = "请填写策略名";
    return;
  }
  submitting.value = true;
  error.value = "";
  try {
    if (scriptKind.value === "structured_v1") {
      if (!(await validateStructuredDefinition())) return;
      const document = structuredDocument();
      await apiPost("/api/strategy/definition-resources", document);
    } else {
      const payload = {
        displayName: name.value.trim(),
        description: "",
        scriptKind: scriptKind.value,
        script: script(),
      };
      await apiPost("/api/strategy/condition/validate", {
        conditionKind: scriptKind.value,
        script: script(),
      });
      if (mode.value === "create")
        await apiPost("/api/strategy/definitions", payload);
      else
        await apiPut(
          `/api/strategy/definitions/${encodeURIComponent(editingId.value)}`,
          payload,
        );
    }
    dialog.value = false;
    await load();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略保存失败";
  } finally {
    submitting.value = false;
  }
}
async function remove(item: Strategy): Promise<void> {
  const action = item.scriptKind === "structured_v1" ? "归档" : "删除";
  if (!window.confirm(`${action}策略“${item.displayName}”？`)) return;
  try {
    await apiDelete(
      item.scriptKind === "structured_v1"
        ? `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}`
        : `/api/strategy/definitions/${encodeURIComponent(item.strategyId)}`,
      { confirmDisplayName: item.displayName },
    );
    await load();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : `策略${action}失败`;
  }
}
async function copyStrategy(item: Strategy): Promise<void> {
  try {
    await apiPost(
      item.scriptKind === "structured_v1"
        ? `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}/copy`
        : `/api/strategy/definitions/${encodeURIComponent(item.strategyId)}/copy`,
      {},
    );
    await load();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略复制失败";
  }
}
async function toggleStrategy(item: Strategy): Promise<void> {
  try {
    await apiPatch(
      item.scriptKind === "structured_v1"
        ? `/api/strategy/definition-resources/${encodeURIComponent(item.strategyId)}/status`
        : `/api/strategy/definitions/${encodeURIComponent(item.strategyId)}/status`,
      { status: item.status === "disabled" ? "active" : "disabled" },
    );
    await load();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略状态更新失败";
  }
}
async function load(force = true): Promise<void> {
  await loadTab("strategy", force);
}
async function loadTab(tab: ResourceTab, force = false): Promise<void> {
  if (loaded.value[tab] && !force) return;
  loading.value = true;
  error.value = "";
  try {
    if (tab === "indicator")
      indicators.value = (
        await apiGet<{ items: CatalogItem[] }>(
          "/api/strategy/indicators",
          undefined,
          { force },
        )
      ).items;
    else if (tab === "function")
      functions.value = (
        await apiGet<{ items: CatalogItem[] }>(
          "/api/strategy/functions",
          undefined,
          { force },
        )
      ).items;
    else {
      // The signal manager owns the current strategy list; legacy resources remain on disk.
    }
    loaded.value[tab] = true;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "策略目录加载失败";
  } finally {
    loading.value = false;
  }
}
function openDetail(item: CatalogItem): void {
  selected.value = item;
  detailOpen.value = true;
}
function openCustomIndicatorCreate(): void {
  const template = copyableIndicatorTemplates.value[0];
  if (!template) {
    error.value = "没有可复制的已启用指标模板";
    return;
  }
  customIndicatorEditorMode.value = "create";
  customIndicatorTarget.value = null;
  customIndicatorTemplateId.value = template.id;
  customIndicatorName.value = `${template.name} 副本`;
  customIndicatorDescription.value = "";
  customIndicatorAssetTypes.value = [...(template.supportedAssetTypes || [])];
  customIndicatorStatus.value = "active";
  customIndicatorDefaults.value = {};
  customIndicatorEditorOpen.value = true;
}
async function openCustomIndicatorEdit(item: CatalogItem): Promise<void> {
  if (item.origin !== "custom" || item.resourceKind !== "indicator") return;
  let target = item;
  try {
    target = await apiGet<CatalogItem>(
      `/api/strategy/indicators/${encodeURIComponent(item.id)}`,
      undefined,
      { force: true },
    );
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "无法读取自定义指标的最新版本";
    return;
  }
  customIndicatorEditorMode.value = "edit";
  customIndicatorTarget.value = target;
  customIndicatorTemplateId.value = target.calculationId || target.id;
  customIndicatorName.value = target.name;
  customIndicatorDescription.value = target.description || "";
  customIndicatorAssetTypes.value = [...(target.supportedAssetTypes || [])];
  customIndicatorStatus.value =
    target.status === "disabled" || target.status === "deprecated"
      ? target.status
      : "active";
  customIndicatorDefaults.value = Object.fromEntries(
    (target.parameters || [])
      .filter((parameter) => parameter.name && typeof parameter.default === "number")
      .map((parameter) => [parameter.name as string, parameter.default as number]),
  );
  customIndicatorEditorOpen.value = true;
}
function customIndicatorAssetOptions(): string[] {
  const target = customIndicatorTarget.value;
  if (!target) return [];
  return (
    indicators.value.find((item) => item.id === (target.calculationId || target.id))
      ?.supportedAssetTypes || target.supportedAssetTypes || []
  );
}
function selectedIndicatorTemplate(): CatalogItem | undefined {
  return copyableIndicatorTemplates.value.find(
    (item) => item.id === customIndicatorTemplateId.value,
  );
}
function customIndicatorDocument(target: CatalogItem): Record<string, unknown> {
  const now = new Date().toISOString();
  return {
    schema_version: 1,
    resource_kind: "indicator",
    id: target.id,
    version: (target.version || 1) + 1,
    display_name: customIndicatorName.value.trim(),
    origin: "custom",
    supported_asset_types: customIndicatorAssetTypes.value,
    status: customIndicatorStatus.value,
    created_at: target.createdAt || now,
    updated_at: now,
    dependencies: (target.dependencies || []).map((dependency) => ({
      resource_kind: "strategy_function",
      id: dependency.id,
      version: dependency.version,
    })),
    capabilities: ["market_data_input", "plot_create"],
    definition: {
      english_name: target.englishName || target.name,
      category: target.category || "custom",
      category_label: target.categoryLabel || "自定义",
      description: customIndicatorDescription.value.trim(),
      placement: target.placement || "overlay",
      parameters: (target.parameters || []).map((parameter) => ({
        ...parameter,
        default:
          parameter.name && customIndicatorDefaults.value[parameter.name] !== undefined
            ? customIndicatorDefaults.value[parameter.name]
            : parameter.default,
      })),
      plots: (target.plots || []).map((plot) => ({ ...plot })),
      calculation_id: target.calculationId || target.id,
    },
  };
}
async function saveCustomIndicator(): Promise<void> {
  if (!customIndicatorName.value.trim()) {
    error.value = "请填写指标名称";
    return;
  }
  if (!customIndicatorAssetTypes.value.length) {
    error.value = "至少选择一个支持的资产类型";
    return;
  }
  customIndicatorSaving.value = true;
  try {
    if (customIndicatorEditorMode.value === "create") {
      const template = selectedIndicatorTemplate();
      if (!template) throw new Error("请选择已启用指标模板");
      await apiPost(
        `/api/strategy/indicators/${encodeURIComponent(template.id)}/copy`,
        { displayName: customIndicatorName.value.trim() },
      );
    } else {
      const target = customIndicatorTarget.value;
      if (!target) throw new Error("未找到要编辑的自定义指标");
      await apiPut(
        `/api/strategy/indicator-resources/${encodeURIComponent(target.id)}`,
        customIndicatorDocument(target),
      );
    }
    customIndicatorEditorOpen.value = false;
    await loadTab("indicator", true);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "自定义指标保存失败";
  } finally {
    customIndicatorSaving.value = false;
  }
}
async function copyCustomIndicator(item: CatalogItem): Promise<void> {
  try {
    await apiPost(`/api/strategy/indicators/${encodeURIComponent(item.id)}/copy`, {
      displayName: `${item.name} 副本`,
    });
    await loadTab("indicator", true);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "指标复制失败";
  }
}
async function deleteCustomIndicator(item: CatalogItem): Promise<void> {
  if (!window.confirm(`删除自定义指标“${item.name}”的所有版本？`)) return;
  try {
    await apiDelete(`/api/strategy/indicator-resources/${encodeURIComponent(item.id)}`);
    detailOpen.value = false;
    selected.value = null;
    await loadTab("indicator", true);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "指标删除失败";
  }
}
type FavoriteResource = Pick<CatalogItem, "id" | "versionedId"> | Pick<Strategy, "strategyId" | "versionedId">;
function favoriteKey(item: FavoriteResource): string {
  return item.versionedId || ("strategyId" in item ? item.strategyId : item.id);
}
function isFavorite(item: FavoriteResource): boolean {
  return favoriteIds.value.includes(favoriteKey(item));
}
function toggleFavorite(item: FavoriteResource): void {
  const id = favoriteKey(item);
  favoriteIds.value = isFavorite(item)
    ? favoriteIds.value.filter((value) => value !== id)
    : [...favoriteIds.value, id];
  localStorage.setItem(
    "marketlistener.strategyFavorites",
    JSON.stringify(favoriteIds.value),
  );
}
function addToCurrentChart(item: CatalogItem): void {
  localStorage.setItem(
    "marketlistener.pendingIndicator",
    JSON.stringify({
      definitionId: item.id,
      version: item.version || 1,
      placement: item.placement || "overlay",
      parameters: Object.fromEntries(
        (item.parameters || [])
          .filter((value) => value.name)
          .map((value) => [value.name as string, value.default]),
      ),
      style: { color: "#f59e0b", lineWidth: 1.5, lineType: "solid" },
      visible: true,
    }),
  );
  detailOpen.value = false;
  void router.push({
    path: "/market/",
    query: { indicator: item.id, version: String(item.version || 1) },
  });
}
function openDrawingTool(item: CatalogItem): void {
  if (item.id !== "drawing.fibonacci_retracement") return;
  localStorage.setItem(
    "marketlistener.pendingDrawingTool",
    JSON.stringify({ tool: "fibonacci_retracement", version: item.version || 1 }),
  );
  detailOpen.value = false;
  void router.push({
    path: "/market/",
    query: {
      drawingTool: "fibonacci_retracement",
      version: String(item.version || 1),
    },
  });
}
function loadStrategyToCurrentChart(item: Strategy): void {
  localStorage.setItem(
    "marketlistener.pendingStrategy",
    JSON.stringify({
      strategyId: item.strategyId,
      strategyVersion: item.version || Number(item.strategyVersion || 1),
      displayName: item.displayName,
      parameters: Object.fromEntries(
        Object.entries(item.parameters || {}).map(([key, value]) => [
          key,
          value.default,
        ]),
      ),
    }),
  );
  void router.push({
    path: "/market/",
    query: { strategy: item.strategyId, version: String(item.version || 1) },
  });
}
function isUntrustedStrategy(item: Strategy): boolean {
  return item.origin === "community" || item.origin === "plugin";
}
function strategyOriginLabel(origin?: string): string {
  if (origin === "builtin") return "内置";
  if (origin === "community") return "社区 · 待审核";
  if (origin === "plugin") return "插件 · 待审核";
  return "自定义";
}
watch(activeTab, (tab) => {
  search.value = "";
  category.value = "";
  assetType.value = "";
  sourceFilter.value = "";
  void router.replace({ query: { ...route.query, section: tab } });
  void loadTab(tab);
});
onMounted(() => void loadTab(activeTab.value));
</script>

<template>
  <main class="strategy-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">策略</h1>
        <p>指标负责可视化，策略函数负责纯计算，策略负责开仓、加仓、减仓和平仓信号监控。</p>
      </div>
      <div class="page-actions">
        <input
          ref="strategyPackageInput"
          class="sr-only"
          type="file"
          accept=".zip,.strategy.zip,application/zip"
          @change="void previewStrategyPackage($event)"
        />
        <el-button
          v-if="activeTab === 'indicator'"
          type="primary"
          @click="openCustomIndicatorCreate"
        >新建自定义指标</el-button>
        <el-button v-if="activeTab === 'strategy'" type="primary" @click="signalManager?.create()">新建策略</el-button>
      </div>
    </header>
    <el-alert
      v-if="error"
      :title="error"
      type="warning"
      :closable="false"
      class="page-alert"
    />
    <section class="panel resource-shell">
      <el-tabs v-model="activeTab" class="resource-tabs">
        <el-tab-pane label="指标" name="indicator" />
        <el-tab-pane label="策略函数" name="function" />
        <el-tab-pane label="策略" name="strategy" />
      </el-tabs>
      <template v-if="activeTab !== 'strategy'">
        <div class="catalog-toolbar">
          <el-input
            v-model="search"
            clearable
            :placeholder="
              activeTab === 'indicator' ? '搜索指标……' : '搜索策略函数……'
            "
          />
          <el-select v-model="category" clearable placeholder="全部分类"
            ><el-option
              v-for="[id, label] in categories"
              :key="id"
              :label="label"
              :value="id"
          /></el-select>
          <el-select v-model="assetType" clearable placeholder="全部资产"
            ><el-option
              v-for="asset in supportedAssetTypes"
              :key="asset"
              :label="asset"
              :value="asset"
          /></el-select>
          <el-select v-model="sourceFilter" clearable placeholder="全部来源"
            ><el-option label="内置" value="builtin" /><el-option
              label="自定义"
              value="custom"
          /></el-select>
        </div>
        <div class="resource-layout" v-loading="loading">
          <aside class="category-nav">
            <button :class="{ active: !category }" @click="category = ''">
              全部</button
            ><button
              v-for="[id, label] in categories"
              :key="id"
              :class="{ active: category === id }"
              @click="category = id"
            >
              {{ label }}</button
            ><button
              :class="{ active: sourceFilter === 'favorite' }"
              @click="
                sourceFilter = sourceFilter === 'favorite' ? '' : 'favorite'
              "
            >
              收藏
            </button>
          </aside>
          <div class="catalog-grid">
            <article
              v-for="item in sourceFilter === 'favorite'
                ? filteredCatalog.filter(isFavorite)
                : filteredCatalog"
              :key="item.versionedId || item.id"
              tabindex="0"
              @click="openDetail(item)"
              @keydown.enter="openDetail(item)"
            >
              <header>
                <div>
                  <h3>{{ item.name }}</h3>
                  <small v-if="item.englishName">{{ item.englishName }}</small>
                </div>
                <el-button
                  text
                  circle
                  :aria-label="isFavorite(item) ? '取消收藏' : '收藏'"
                  @click.stop="toggleFavorite(item)"
                  >{{ isFavorite(item) ? "★" : "☆" }}</el-button
                >
              </header>
              <p>{{ item.description || item.definition }}</p>
              <div class="resource-tags">
                <el-tag size="small" effect="plain">{{
                  item.categoryLabel || item.category || "未分类"
                }}</el-tag
                ><el-tag size="small" effect="plain">{{
                  item.resourceKind === "drawing_tool"
                    ? "绘图工具"
                    : item.placement === "overlay"
                    ? "主图"
                    : item.placement === "pane"
                      ? "副图"
                      : "纯函数"
                }}</el-tag
                ><el-tag
                  v-if="item.status && item.status !== 'active'"
                  size="small"
                  :type="item.status === 'disabled' ? 'warning' : 'info'"
                  >{{ item.status }}</el-tag
                >
              </div>
              <small
                >v{{ item.version || 1 }} ·
                {{ item.origin === "custom" ? "自定义" : "内置" }}</small
              >
            </article>
            <el-empty
              v-if="!filteredCatalog.length"
              description="没有匹配资源"
            />
          </div>
        </div>
      </template>
      <template v-else><SignalStrategyManager ref="signalManager"/></template>
    </section>
    <el-dialog
      v-model="customIndicatorEditorOpen"
      :title="customIndicatorEditorMode === 'create' ? '新建自定义指标' : '编辑自定义指标（新版本）'"
      width="min(640px, calc(100vw - 24px))"
      destroy-on-close
    >
      <p class="muted">
        自定义指标只能复用已发布指标的计算模板；函数依赖、参数边界和绘图结构保持锁定，不接受任意公式或脚本。
      </p>
      <el-form label-position="top" class="indicator-editor-form">
        <el-form-item v-if="customIndicatorEditorMode === 'create'" label="计算模板">
          <el-select v-model="customIndicatorTemplateId" filterable>
            <el-option
              v-for="item in copyableIndicatorTemplates"
              :key="item.id"
              :label="`${item.name} · ${item.placement === 'pane' ? '副图' : '主图'}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="显示名称">
          <el-input v-model="customIndicatorName" maxlength="128" />
        </el-form-item>
        <template v-if="customIndicatorEditorMode === 'edit'">
          <el-form-item label="说明">
            <el-input v-model="customIndicatorDescription" type="textarea" :rows="3" maxlength="2000" />
          </el-form-item>
          <el-form-item label="支持资产">
            <el-checkbox-group v-model="customIndicatorAssetTypes">
              <el-checkbox
                v-for="asset in customIndicatorAssetOptions()"
                :key="asset"
                :label="asset"
              >{{ asset }}</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="状态">
            <el-radio-group v-model="customIndicatorStatus">
              <el-radio value="active">已启用</el-radio>
              <el-radio value="disabled">已禁用</el-radio>
              <el-radio value="deprecated">已归档</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item
            v-for="parameter in customIndicatorTarget?.parameters || []"
            :key="parameter.name"
            :label="`${parameter.name || '参数'}（${parameter.minimum} - ${parameter.maximum}）`"
          >
            <el-input-number
              v-if="parameter.name"
              :model-value="customIndicatorDefaults[parameter.name]"
              :min="parameter.minimum"
              :max="parameter.maximum"
              :step="parameter.type === 'integer' ? 1 : 0.1"
              @update:model-value="customIndicatorDefaults[parameter.name] = Number($event)"
            />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="customIndicatorEditorOpen = false">取消</el-button>
        <el-button type="primary" :loading="customIndicatorSaving" @click="void saveCustomIndicator()">
          {{ customIndicatorEditorMode === 'create' ? '创建' : '发布新版本' }}
        </el-button>
      </template>
    </el-dialog>
    <el-drawer
      v-model="detailOpen"
      :title="selected?.name || '资源详情'"
      size="min(460px, 92vw)"
    >
      <template v-if="selected"
        ><p class="detail-intro">
          {{ selected.description || selected.definition }}
        </p>
        <el-descriptions :column="1" border
          ><el-descriptions-item label="稳定 ID">{{
            selected.versionedId || `${selected.id}@${selected.version || 1}`
          }}</el-descriptions-item
          ><el-descriptions-item label="分类">{{
            selected.categoryLabel || selected.category
          }}</el-descriptions-item
          ><el-descriptions-item label="图表位置">{{
            selected.resourceKind === "drawing_tool"
              ? "绘图工具"
              : selected.placement === "overlay"
              ? "主图"
              : selected.placement === "pane"
                ? "副图"
                : "不绘图"
          }}</el-descriptions-item
          ><el-descriptions-item label="资产类型">{{
            selected.supportedAssetTypes?.join("、") || "—"
          }}</el-descriptions-item
          ><el-descriptions-item label="状态"
            >{{ selected.status || "active"
            }}<span v-if="selected.unavailableReason"
              >：{{ selected.unavailableReason }}</span
            ></el-descriptions-item
          ><el-descriptions-item v-if="selected.mathFormula" label="公式">
            {{ selected.mathFormula }}
          </el-descriptions-item
          ><el-descriptions-item v-if="selected.requiredFields?.length" label="所需字段">
            {{ selected.requiredFields.join("、") }}
          </el-descriptions-item
          ><el-descriptions-item v-if="selected.warmupBars" label="暖机长度">
            {{ selected.warmupBars }}
          </el-descriptions-item
          ></el-descriptions
        >
        <p v-if="selected.formulaSource" class="formula-source">
          公式说明来源：<a :href="selected.formulaSource" target="_blank" rel="noreferrer">公开说明</a>
        </p>
        <div v-if="selected.limitations?.length">
          <h3>限制</h3>
          <ul>
            <li v-for="limitation in selected.limitations" :key="limitation">{{ limitation }}</li>
          </ul>
        </div>
        <h3>参数 / 输入</h3>
        <ul>
          <li
            v-for="parameter in activeTab === 'function'
              ? selected.inputs
              : selected.parameters"
            :key="parameter.name"
          >
            {{ parameter.name }}<span v-if="parameter.type"> · {{ parameter.type }}</span><span
              v-if="parameter.default !== undefined"
            >（默认 {{ parameter.default }}）</span>
          </li>
          <li
            v-if="activeTab === 'function'
              ? !selected.inputs?.length
              : !selected.parameters?.length"
          >无可调参数</li>
        </ul>
        <p v-if="activeTab === 'function'" class="function-output">
          <b>返回类型：</b>{{ selected.output?.type || selected.returnType || "未声明" }}
        </p>
        <h3 v-if="selected.dependencies?.length">依赖函数</h3>
        <ul v-if="selected.dependencies?.length">
          <li v-for="dependency in selected.dependencies" :key="dependency.id">
            {{ dependency.id }}@{{ dependency.version }}
          </li>
        </ul>
        <div v-if="activeTab === 'function'" class="function-references">
          <h3>被引用</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="指标">
              {{ selected.referencedBy?.indicators?.join("、") || "暂无" }}
            </el-descriptions-item>
            <el-descriptions-item label="策略">
              {{ selected.referencedBy?.strategies?.join("、") || "暂无" }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
        <div class="drawer-actions">
          <el-button @click="toggleFavorite(selected)">{{
            isFavorite(selected) ? "取消收藏" : "收藏"
          }}</el-button
          ><el-button
            v-if="activeTab === 'indicator' && selected.resourceKind !== 'drawing_tool'"
            type="primary"
            :disabled="selected.status === 'disabled'"
            @click="addToCurrentChart(selected)"
            >添加到当前图表</el-button
          >
          <el-button
            v-if="activeTab === 'indicator' && selected.resourceKind === 'indicator'"
            @click="void copyCustomIndicator(selected)"
          >复制为自定义指标</el-button>
          <el-button
            v-if="activeTab === 'indicator' && selected.origin === 'custom'"
            @click="void openCustomIndicatorEdit(selected)"
          >编辑为新版本</el-button>
          <el-button
            v-if="activeTab === 'indicator' && selected.origin === 'custom'"
            type="danger"
            @click="void deleteCustomIndicator(selected)"
          >删除</el-button>
          <el-button
            v-if="activeTab === 'indicator' && selected.resourceKind === 'drawing_tool'"
            type="primary"
            @click="openDrawingTool(selected)"
            >打开绘图工具</el-button
          >
        </div></template
      >
    </el-drawer>
    <el-drawer
      v-model="versionHistoryOpen"
      :title="`${versionHistoryStrategy?.displayName || '策略'} · 历史版本`"
      size="min(680px, 94vw)"
    >
      <p class="muted">
        已发布版本不可原地改写。选择一个版本可在当前图表回测，或以它为基础创建下一个版本。
      </p>
      <el-table :data="versionHistory" empty-text="暂无历史版本">
        <el-table-column prop="version" label="版本" width="80">
          <template #default="scope">v{{ scope.row.version }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="更新时间" min-width="160">
          <template #default="scope">{{ formatTime(scope.row.updatedAt) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="scope">
            <el-button text @click="loadHistoricalVersionToChart(scope.row)">用于回测</el-button>
            <el-button text @click="void editHistoricalVersion(scope.row)">编辑为新版本</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
    <el-dialog
      v-model="templateWizardOpen"
      title="从策略模板创建"
      width="min(880px, calc(100vw - 24px))"
      destroy-on-close
    >
      <p class="muted">模板是不可变的研究起点。创建后会生成独立 custom v1；请在编辑器中复核规则与参数，默认值不代表收益承诺。</p>
      <el-form label-position="top" class="template-name-form">
        <el-form-item label="新策略名称（可选）">
          <el-input v-model="templateDisplayName" maxlength="128" placeholder="留空则使用模板默认名称" />
        </el-form-item>
      </el-form>
      <div class="template-grid">
        <article v-for="template in templates" :key="template.templateId" class="template-card">
          <header>
            <div>
              <h3>{{ template.displayName }} <small>v{{ template.version }}</small></h3>
              <p>{{ template.description }}</p>
            </div>
            <el-tag size="small" effect="plain">内置模板</el-tag>
          </header>
          <dl>
            <dt>来源</dt>
            <dd>{{ template.sourceStrategyId ? `${template.sourceStrategyId}@${template.sourceStrategyVersion}` : "受控空白占位规则" }}</dd>
            <dt>适用资产</dt>
            <dd>{{ template.supportedAssetTypes.join("、") }}</dd>
            <dt>依赖</dt>
            <dd>{{ template.dependencies.map((item) => `${item.id}@${item.version}`).join("、") }}</dd>
            <dt>风险预览</dt>
            <dd>仓位 {{ template.preview.positionSizing.kind }} {{ template.preview.positionSizing.value }}；最大回撤 {{ template.preview.risk.maxDrawdownPercent }}%</dd>
            <dt>参数</dt>
            <dd>{{ Object.entries(template.preview.parameters).map(([key, value]) => `${key}=${value.default ?? "—"}`).join("、") || "无" }}</dd>
          </dl>
          <p class="template-disclaimer">{{ template.disclaimer }}</p>
          <el-button
            type="primary"
            :loading="templateCreating === template.templateId"
            @click="void createFromTemplate(template)"
            >以此模板创建</el-button
          >
        </article>
      </div>
    </el-dialog>
    <el-dialog
      v-model="packageImportOpen"
      title="导入策略包"
      width="min(620px, calc(100vw - 24px))"
      destroy-on-close
    >
      <template v-if="packagePreview">
        <p class="muted">{{ packageImportFileName }} · 先在本地完成结构、哈希、依赖与签名校验，确认后才写入。</p>
        <el-descriptions :column="1" border>
          <el-descriptions-item label="策略">
            {{ packagePreview.definition.displayName }} · {{ packagePreview.definition.id }}@{{ packagePreview.definition.version }}
          </el-descriptions-item>
          <el-descriptions-item label="依赖函数">
            {{ packagePreview.dependencyLock.strategyFunctions.map((item) => `${item.id}@${item.version}`).join("、") || "无" }}
          </el-descriptions-item>
          <el-descriptions-item label="签名">
            {{ packagePreview.signatureVerified ? "Ed25519 校验通过" : "未签名（仅可作为桌面受限规则包导入）" }}
          </el-descriptions-item>
          <el-descriptions-item label="Android">
            不可导入：{{ packagePreview.androidReason }}
          </el-descriptions-item>
        </el-descriptions>
        <template v-if="packagePreview.conflict.exists">
          <el-alert
            class="package-conflict-alert"
            type="warning"
            :closable="false"
            title="检测到同 ID 的已发布策略"
            :description="`现有版本：v${packagePreview.conflict.versions.join('、v')}。不可覆盖，请明确选择处理方式。`"
          />
          <el-radio-group v-model="packageConflict" aria-label="导入冲突处理">
            <el-radio value="cancel">取消导入</el-radio>
            <el-radio value="new_version">作为新版本导入</el-radio>
            <el-radio value="rename">改名导入</el-radio>
          </el-radio-group>
          <el-input
            v-if="packageConflict === 'rename'"
            v-model="packageNewStrategyId"
            class="package-new-id"
            placeholder="新策略 ID，例如 strategy.my_import"
          />
        </template>
        <el-alert
          v-else
          class="package-conflict-alert"
          type="info"
          :closable="false"
          title="目标目录没有同 ID 策略，将以原始版本无损导入。"
        />
        <p v-if="packageImportError" class="rule-validation-summary" role="alert">{{ packageImportError }}</p>
      </template>
      <template #footer>
        <el-button @click="packageImportOpen = false">取消</el-button>
        <el-button type="primary" :loading="packageImportBusy" @click="void importStrategyPackage()">确认导入</el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="dialog"
      :title="mode === 'create' ? '新建策略' : '编辑策略'"
      width="min(960px, calc(100vw - 24px))"
      destroy-on-close
      ><el-form label-position="top"
        ><el-form-item label="策略名" required
          ><el-input v-model="name" maxlength="64" /></el-form-item
        ><el-form-item label="策略定义"
          ><el-radio-group v-model="scriptKind"
            ><el-radio-button value="structured_v1">结构化规则</el-radio-button
            ><el-radio-button value="builder_v1">兼容可视化条件</el-radio-button
            ><el-radio-button value="python_safe_v1"
              >兼容安全 Python</el-radio-button
            ></el-radio-group
          ></el-form-item
        ><template v-if="scriptKind === 'structured_v1'"
          ><section
            class="structured-editor"
            :data-supported-asset-types="strategySupportedAssetTypes.join(',')"
          >
            <h3>标的与周期</h3>
            <div class="scope-row">
              <el-checkbox-group v-model="marketTypes"
                ><el-checkbox
                  v-for="[id, text] in marketOptions"
                  :key="id"
                  :value="id"
                  >{{ text }}</el-checkbox
                ></el-checkbox-group
              ><el-checkbox v-model="excludeSt">排除 ST 与 *ST</el-checkbox
              ><el-select v-model="period" aria-label="基础周期"
                ><el-option
                  v-for="[id, text] in [
                    ['1m', '1M'],
                    ['5m', '5M'],
                    ['15m', '15M'],
                    ['30m', '30M'],
                    ['1h', '1H'],
                    ['2h', '2H'],
                    ['4h', '4H'],
                    ['1d', '1D'],
                    ['1w', '1W'],
                    ['1mo', '月线'],
                  ]"
                  :key="id"
                  :label="text"
                  :value="id"
              /></el-select>
              <el-select v-model="strategyDirection" aria-label="交易方向">
                <el-option label="做多" value="long" />
                <el-option label="做空" value="short" />
              </el-select>
            </div>
            <div class="rule-toolbar">
              <div>
                <h3>开仓与平仓规则树</h3>
                <p class="muted">
                  函数、版本和参数签名来自策略函数注册表；AND / OR / NOT 会直接写入规范 AST。
                </p>
              </div>
              <div>
                <el-button @click="void validateStructuredDefinition()">验证规则</el-button>
                <el-button :disabled="!ruleHistory.length" @click="undoRuleEdit">撤销上次规则编辑</el-button>
              </div>
            </div>
            <p v-if="ruleValidationSummary" class="rule-validation-summary" aria-live="polite">
              {{ ruleValidationSummary }}
            </p>
            <div class="rule-columns">
              <section>
                <h4>开仓</h4>
                <StrategyRuleTreeEditor
                  :model-value="entryRules"
                  :functions="visualFunctionCatalog"
                  :supported-asset-types="strategySupportedAssetTypes"
                  path="entryRules"
                  :errors="ruleValidationErrors"
                  root
                  @update:model-value="updateEntryRules"
                />
              </section>
              <section>
                <h4>平仓</h4>
                <StrategyRuleTreeEditor
                  :model-value="exitRules"
                  :functions="visualFunctionCatalog"
                  :supported-asset-types="strategySupportedAssetTypes"
                  path="exitRules"
                  :errors="ruleValidationErrors"
                  root
                  @update:model-value="updateExitRules"
                />
              </section>
            </div>
            <div class="rule-dependencies" aria-label="规则函数依赖预览">
              <b>依赖预览</b>
              <span v-if="structuredDependencies.length">{{ structuredDependencies.join("、") }}</span>
              <span v-else class="muted">尚无函数依赖</span>
            </div>
            <h3>仓位、风险与回测</h3>
            <div class="structured-grid">
              <el-form-item label="仓位模型"
                ><el-select v-model="positionSizingKind" aria-label="仓位模型"
                  ><el-option label="权益比例" value="equity_percent" /><el-option
                    label="固定数量"
                    value="fixed_quantity" /><el-option
                    label="风险比例"
                    value="risk_percent" /></el-select></el-form-item
              ><el-form-item label="仓位数值"
                ><el-input-number
                  v-model="positionPercent"
                  :min="0.01"
                  :max="100" /></el-form-item
              ><el-form-item label="最大回撤 %"
                ><el-input-number
                  v-model="maxDrawdownPercent"
                  :min="0"
                  :max="100" /></el-form-item
              ><el-form-item label="初始资金"
                ><el-input-number
                  v-model="initialCash"
                  :min="1" /></el-form-item
              ><el-form-item label="手续费率"
                ><el-input-number
                  v-model="commissionRate"
                  :min="0"
                  :max="1"
                  :step="0.0001" /></el-form-item
              ><el-form-item label="滑点率"
                ><el-input-number
                  v-model="slippageRate"
                  :min="0"
                  :max="1"
                  :step="0.0001" /></el-form-item
              ><el-form-item label="止损"
                ><el-switch v-model="stopLossEnabled" /><el-input-number
                  v-model="stopLossValue"
                  :disabled="!stopLossEnabled"
                  :min="0"
                  :max="stopLossKind === 'fixed_price' ? undefined : 100" /><el-select
                  v-model="stopLossKind"
                  :disabled="!stopLossEnabled"
                  ><el-option label="百分比" value="percent" /><el-option
                    label="ATR 倍数"
                    value="atr_multiple" /><el-option
                    label="固定价格"
                    value="fixed_price" /></el-select></el-form-item
              ><el-form-item label="止盈"
                ><el-switch v-model="takeProfitEnabled" /><el-input-number
                  v-model="takeProfitValue"
                  :disabled="!takeProfitEnabled"
                  :min="0"
                  :max="takeProfitKind === 'fixed_price' ? undefined : 100" /><el-select
                  v-model="takeProfitKind"
                  :disabled="!takeProfitEnabled"
                  ><el-option label="百分比" value="percent" /><el-option
                    label="ATR 倍数"
                    value="atr_multiple" /><el-option
                    label="固定价格"
                    value="fixed_price" /></el-select></el-form-item
              ><el-form-item label="止盈减仓">
                <div class="exit-scale-out" data-testid="strategy-scale-out">
                  <el-input-number
                    v-model="scaleOutRatioPercent"
                    aria-label="止盈减仓比例"
                    :disabled="!takeProfitEnabled"
                    :min="0"
                    :max="99"
                  />
                  <span class="muted">0 表示关闭；1–99 表示首次触及止盈时一次性减仓的比例</span>
                </div>
              </el-form-item>
              ><el-form-item label="允许加仓"
                ><el-switch v-model="pyramidingEnabled" /><el-input-number
                  v-model="pyramidingMaxEntries"
                  :disabled="!pyramidingEnabled"
                  :min="1"
                  :max="100" /></el-form-item
              ><el-form-item label="允许重新入场"
                ><el-switch v-model="reentryEnabled" /><el-input-number
                  v-model="reentryCooldownBars"
                  :disabled="!reentryEnabled"
                  :min="0"
                  :max="1000" /></el-form-item>
            </div>
            <el-alert
              title="实盘模式不可用；当前定义只允许回测或模拟执行。"
              type="info"
              :closable="false"
            /></section></template
        ><template v-else-if="scriptKind === 'builder_v1'"
          ><section class="condition-builder">
            <div class="scope-row">
              <b>市场范围函数</b
              ><el-checkbox-group v-model="marketTypes"
                ><el-checkbox
                  v-for="[id, text] in marketOptions"
                  :key="id"
                  :value="id"
                  >{{ text }}</el-checkbox
                ></el-checkbox-group
              ><el-checkbox v-model="excludeSt">不包括 ST 与 *ST</el-checkbox
              ><el-select v-model="capField" clearable placeholder="市值条件"
                ><el-option
                  label="总市值（亿元）"
                  value="total_market_cap_yi" />
                <el-option
                  label="流通市值（亿元）"
                  value="float_market_cap_yi" /></el-select
              ><el-select v-if="capField" v-model="capOperator"
                ><el-option label=">" value="gt" /><el-option
                  label="&lt;"
                  value="lt" /></el-select
              ><el-input-number
                v-if="capField"
                v-model="capValue"
                :min="0.01"
                :controls="false"
                placeholder="亿元"
              />
            </div>
            <div class="root-row">
              <span>根条件组</span
              ><el-select v-model="rootOperator"
                ><el-option label="AND（同时满足）" value="and" /><el-option
                  label="OR（满足任一）"
                  value="or"
              /></el-select>
            </div>
            <section
              v-for="(group, g) in groups"
              :key="g"
              class="condition-group"
            >
              <header>
                <b>条件组 {{ g + 1 }}</b
                ><el-select v-model="group.operator" size="small"
                  ><el-option label="AND" value="and" /><el-option
                    label="OR"
                    value="or" /></el-select
                ><el-button text type="danger" @click="groups.splice(g, 1)"
                  >删除组</el-button
                >
              </header>
              <div
                v-for="(row, r) in group.conditions"
                :key="r"
                class="condition-row"
              >
                <el-select
                  v-model="row.functionId"
                  filterable
                  @change="updateFunction(row)"
                  ><el-option
                    v-for="item in functionChoices"
                    :key="item.id"
                    :label="item.name"
                    :value="item.id" /></el-select
                ><el-select v-model="row.period"
                  ><el-option label="1D" value="1d" /><el-option
                    label="1W"
                    value="1w" /></el-select
                ><el-input-number
                  v-for="(_arg, a) in row.args"
                  :key="a"
                  v-model="row.args[a]"
                  :controls="false"
                  :min="0"
                /><el-select v-if="row.operator" v-model="row.operator"
                  ><el-option label=">" value="gt" /><el-option
                    label="&lt;"
                    value="lt" /></el-select
                ><el-input-number
                  v-if="row.operator"
                  v-model="row.value"
                  :controls="false"
                />
              </div>
            </section></section></template
        ><template v-else
          ><el-form-item label="K 线周期"
            ><el-select v-model="period"
              ><el-option label="1D" value="1d" /><el-option
                label="1W"
                value="1w" /></el-select></el-form-item
          ><el-form-item label="兼容安全 Python 条件"
            ><el-input
              v-model="source"
              type="textarea"
              :rows="14"
              spellcheck="false"
              class="source"
            />
            <p class="muted">
              旧定义兼容入口；新策略应使用结构化规则。
            </p></el-form-item
          ></template
        ></el-form
      ><template #footer
        ><el-button @click="dialog = false">取消</el-button
        ><el-button
          type="primary"
          :loading="submitting"
          @click="void save()"
          >{{
            scriptKind === "structured_v1"
              ? `保存为 v${structuredVersion}`
              : "保存"
          }}</el-button
        ></template
      ></el-dialog
    >
  </main>
</template>

<style scoped>
.resource-shell {
  min-height: 520px;
}
.resource-tabs {
  margin-bottom: 14px;
}
.catalog-toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 2fr) repeat(3, minmax(130px, 1fr));
  gap: 10px;
}
.strategy-toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 2fr) repeat(5, minmax(120px, 1fr)) auto;
  gap: 10px;
  margin: 14px 0;
}
.resource-layout {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr);
  gap: 16px;
  margin-top: 14px;
}
.category-nav {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.category-nav button {
  padding: 8px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ml-text-secondary);
  text-align: left;
  cursor: pointer;
}
.category-nav button:hover,
.category-nav button.active {
  background: var(--ml-background);
  color: var(--ml-accent, #409eff);
}
.catalog-grid article {
  cursor: pointer;
}
.catalog-grid article:hover,
.catalog-grid article:focus {
  border-color: var(--ml-accent, #409eff);
  outline: none;
}
.catalog-grid article header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.resource-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.execution-boundary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: var(--ml-background);
}
.execution-boundary > div:first-child {
  display: grid;
  gap: 4px;
}
.detail-intro {
  color: var(--ml-text-secondary);
  line-height: 1.55;
}
.drawer-actions {
  display: flex;
  gap: 8px;
  margin-top: 20px;
}
.structured-editor {
  display: grid;
  gap: 10px;
}
.structured-editor h3 {
  margin: 8px 0 0;
}
.structured-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 12px;
}
.structured-grid .el-form-item {
  margin-bottom: 10px;
}
.structured-grid .el-input-number,
.structured-grid .el-select {
  width: 100%;
}
.strategy-page {
  max-width: 1500px;
  margin: 0 auto;
}
.page-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 18px;
}
.page-heading h1,
.page-heading p {
  margin: 0;
}
.page-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.package-conflict-alert {
  margin: 14px 0;
}
.package-new-id {
  margin-top: 12px;
}
.template-name-form {
  max-width: 420px;
}
.template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 12px;
}
.template-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
}
.template-card header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.template-card h3,
.template-card p,
.template-card dl {
  margin: 0;
}
.template-card h3 small,
.template-card p,
.template-card dt {
  color: var(--ml-text-secondary);
}
.template-card p,
.template-card dd,
.template-card dt {
  font-size: 12px;
  line-height: 1.5;
}
.template-card dl {
  display: grid;
  grid-template-columns: 70px minmax(0, 1fr);
  gap: 5px 8px;
}
.template-card dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.template-disclaimer {
  margin-top: auto !important;
}
.page-heading p {
  margin-top: 6px;
  color: var(--ml-text-secondary);
  font-size: 13px;
}
.panel {
  margin-bottom: 18px;
  padding: 18px;
}
.panel-title {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.panel-title h2,
.panel-title p {
  margin: 0;
}
.catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
  margin-top: 14px;
}
.catalog-grid article {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
}
.catalog-grid h3,
.catalog-grid p {
  margin: 0;
}
.catalog-grid p {
  color: var(--ml-text-secondary);
  font-size: 13px;
  line-height: 1.55;
}
.catalog-grid code {
  padding: 7px;
  border-radius: 5px;
  background: var(--ml-background);
  font-size: 11px;
  overflow: auto;
}
.catalog-grid small {
  color: var(--ml-text-secondary);
}
.functions {
  margin-top: 18px;
}
.condition-builder {
  display: grid;
  gap: 12px;
}
.scope-row,
.root-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--ml-divider);
  border-radius: 7px;
}
.scope-row .el-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}
.root-row .el-select {
  width: 160px;
}
.condition-group {
  padding: 12px;
  border: 1px dashed var(--ml-divider);
  border-radius: 8px;
}
.condition-group header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.condition-group header .el-select {
  width: 100px;
}
.condition-row {
  display: grid;
  grid-template-columns:
    minmax(170px, 1fr) repeat(3, minmax(90px, 120px))
    minmax(70px, 90px) minmax(100px, 130px) auto;
  gap: 8px;
  margin: 7px 0;
}
.source :deep(textarea) {
  font-family: ui-monospace, Consolas, monospace;
  line-height: 1.55;
}
@media (max-width: 760px) {
  .page-heading {
    flex-direction: column;
  }
  .condition-row {
    grid-template-columns: 1fr 1fr;
  }
  .strategy-page {
    min-width: 0;
  }
  .execution-boundary {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
