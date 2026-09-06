<script setup lang="ts">
import { computed } from "vue";
import type { StrategyOperand } from "../../domain/strategyTypes";

export interface StrategyFunctionInputOption {
  name: string;
  type: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
}

export interface StrategyFunctionOption {
  id: string;
  version?: number;
  name: string;
  inputs?: StrategyFunctionInputOption[];
  output?: { type?: string };
  supportedAssetTypes?: string[];
}

const props = withDefaults(
  defineProps<{
    modelValue: StrategyOperand;
    expectedType: string;
    functions: StrategyFunctionOption[];
    supportedAssetTypes: string[];
    depth?: number;
  }>(),
  { depth: 0 },
);
const emit = defineEmits<{ "update:modelValue": [value: StrategyOperand] }>();

const seriesFields = [
  ["open", "开盘价"],
  ["high", "最高价"],
  ["low", "最低价"],
  ["close", "收盘价"],
  ["volume", "成交量"],
  ["amount", "成交额"],
  ["open_interest", "持仓量"],
  ["settlement", "结算价"],
] as const;
type SeriesField = Extract<StrategyOperand, { kind: "series" }>["field"];
const seriesFieldIds = new Set<SeriesField>(seriesFields.map(([id]) => id));

function supportsAssets(item: StrategyFunctionOption): boolean {
  return props.supportedAssetTypes.every((asset) =>
    item.supportedAssetTypes?.includes(asset),
  );
}
function outputMatches(item: StrategyFunctionOption): boolean {
  const output = item.output?.type;
  if (props.expectedType === "series<number>") return output === "series<number>";
  if (props.expectedType === "series<boolean>") return output === "series<boolean>";
  if (props.expectedType === "boolean") return output === "boolean";
  return ["number", "integer"].includes(output || "");
}
const functionChoices = computed(() =>
  props.functions.filter((item) => supportsAssets(item) && outputMatches(item)),
);
const operandMode = computed(() => {
  if (props.modelValue.kind === "function") return "function";
  if (props.modelValue.kind === "series") return "series";
  if (props.modelValue.kind === "parameter") return "parameter";
  return "literal";
});
const isSeries = computed(() => props.expectedType.startsWith("series<"));
const isBoolean = computed(() => props.expectedType === "boolean");
const functionCall = computed(() =>
  props.modelValue.kind === "function" ? props.modelValue.call : undefined,
);
const literalValue = computed(() =>
  props.modelValue.kind === "literal" ? props.modelValue.value : 0,
);

function numberDefault(input: StrategyFunctionInputOption): number {
  return typeof input.default === "number" ? input.default : input.type === "integer" ? 20 : 0;
}
function fieldFor(input: StrategyFunctionInputOption): SeriesField {
  const normalized = input.name.toLowerCase();
  return seriesFieldIds.has(normalized as SeriesField)
    ? (normalized as SeriesField)
    : "close";
}
function defaultOperand(input: StrategyFunctionInputOption): StrategyOperand {
  if (input.type.startsWith("series<")) {
    return { kind: "series", field: fieldFor(input) };
  }
  if (input.type === "boolean") return { kind: "literal", value: false };
  if (["number", "integer"].includes(input.type)) {
    return { kind: "literal", value: numberDefault(input) };
  }
  return { kind: "literal", value: String(input.default ?? "") };
}
function functionOperand(item: StrategyFunctionOption): StrategyOperand {
  return {
    kind: "function",
    call: {
      functionId: item.id,
      version: item.version || 1,
      arguments: (item.inputs || []).map(defaultOperand),
    },
  };
}
function changeMode(value: string): void {
  if (value === "function") {
    const choice = functionChoices.value[0];
    if (choice) emit("update:modelValue", functionOperand(choice));
    return;
  }
  if (value === "series") {
    emit("update:modelValue", { kind: "series", field: "close" });
    return;
  }
  if (value === "parameter") {
    emit("update:modelValue", { kind: "parameter", name: "" });
    return;
  }
  emit("update:modelValue", { kind: "literal", value: isBoolean.value ? false : 0 });
}
function selectFunction(id: string): void {
  const choice = functionChoices.value.find((item) => item.id === id);
  if (choice) emit("update:modelValue", functionOperand(choice));
}
function updateArgument(index: number, value: StrategyOperand): void {
  if (props.modelValue.kind !== "function") return;
  const arguments_ = [...props.modelValue.call.arguments];
  arguments_[index] = value;
  emit("update:modelValue", {
    kind: "function",
    call: { ...props.modelValue.call, arguments: arguments_ },
  });
}
function updateLiteral(value: number | string | boolean): void {
  emit("update:modelValue", { kind: "literal", value });
}
function updateSeries(field: string): void {
  const offset = props.modelValue.kind === "series" ? props.modelValue.offset : undefined;
  emit("update:modelValue", {
    kind: "series",
    field: field as SeriesField,
    ...(offset ? { offset } : {}),
  });
}
function updateOffset(value: number | undefined): void {
  if (props.modelValue.kind !== "series") return;
  emit("update:modelValue", {
    ...props.modelValue,
    ...(value ? { offset: value } : {}),
  });
}
</script>

<template>
  <div class="operand-editor" :class="{ nested: depth > 0 }">
    <el-select
      class="operand-mode"
      :model-value="operandMode"
      aria-label="操作数类型"
      @change="changeMode(String($event))"
    >
      <el-option label="常量" value="literal" />
      <el-option v-if="isSeries" label="行情字段" value="series" />
      <el-option
        v-if="functionChoices.length && depth < 5"
        label="函数结果"
        value="function"
      />
    </el-select>
    <template v-if="modelValue.kind === 'series'">
      <el-select
        :model-value="modelValue.field"
        aria-label="行情字段"
        @change="updateSeries(String($event))"
      >
        <el-option
          v-for="[id, label] in seriesFields"
          :key="id"
          :label="label"
          :value="id"
        />
      </el-select>
      <el-input-number
        :model-value="modelValue.offset || 0"
        :min="0"
        :max="500"
        :controls="false"
        aria-label="历史偏移"
        @change="updateOffset($event ?? undefined)"
      />
    </template>
    <template v-else-if="functionCall">
      <el-select
        :model-value="functionCall.functionId"
        filterable
        aria-label="嵌套策略函数"
        @change="selectFunction(String($event))"
      >
        <el-option
          v-for="item in functionChoices"
          :key="`${item.id}@${item.version || 1}`"
          :label="item.name"
          :value="item.id"
        />
      </el-select>
      <div v-if="functionCall.arguments.length" class="nested-arguments">
        <label
          v-for="(input, index) in (
            functions.find((item) => item.id === functionCall?.functionId)
              ?.inputs || []
          )"
          :key="`${input.name}-${index}`"
          ><span>{{ input.name }}</span
          ><StrategyOperandEditor
            :model-value="functionCall.arguments[index]"
            :expected-type="input.type"
            :functions="functions"
            :supported-asset-types="supportedAssetTypes"
            :depth="depth + 1"
            @update:model-value="updateArgument(index, $event)"
        /></label>
      </div>
    </template>
    <el-switch
      v-else-if="isBoolean && typeof literalValue === 'boolean'"
      :model-value="literalValue"
      aria-label="布尔常量"
      @change="updateLiteral(Boolean($event))"
    />
    <el-input-number
      v-else-if="typeof literalValue === 'number'"
      :model-value="literalValue"
      :controls="false"
      aria-label="数值常量"
      @change="updateLiteral(Number($event ?? 0))"
    />
    <el-input
      v-else
      :model-value="String(literalValue)"
      aria-label="文本常量"
      @update:model-value="updateLiteral($event)"
    />
  </div>
</template>

<style scoped>
.operand-editor {
  display: grid;
  grid-template-columns: minmax(88px, auto) minmax(110px, 1fr) minmax(70px, 100px);
  gap: 6px;
  align-items: start;
}
.operand-editor :deep(.el-select),
.operand-editor :deep(.el-input-number),
.operand-editor :deep(.el-input) {
  min-width: 0;
  width: 100%;
}
.nested-arguments {
  grid-column: 1 / -1;
  display: grid;
  gap: 6px;
  padding: 7px;
  border-left: 2px solid var(--ml-divider);
}
.nested-arguments label {
  display: grid;
  gap: 3px;
  color: var(--ml-text-secondary);
  font-size: 11px;
}
@media (max-width: 760px) {
  .operand-editor {
    grid-template-columns: 1fr;
  }
}
</style>
