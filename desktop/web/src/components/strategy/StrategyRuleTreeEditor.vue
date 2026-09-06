<script setup lang="ts">
import { computed, ref } from "vue";
import type {
  StrategyConditionRule,
  StrategyFunctionCall,
  StrategyOperand,
  StrategyRule,
} from "../../domain/strategyTypes";
import StrategyOperandEditor, {
  type StrategyFunctionOption,
} from "./StrategyOperandEditor.vue";

defineOptions({ name: "StrategyRuleTreeEditor" });

const props = withDefaults(
  defineProps<{
    modelValue: StrategyRule;
    functions: StrategyFunctionOption[];
    supportedAssetTypes: string[];
    path?: string;
    errors?: Record<string, string>;
    root?: boolean;
  }>(),
  { path: "rule", errors: () => ({}), root: false },
);
const emit = defineEmits<{ "update:modelValue": [value: StrategyRule] }>();
const draggingChildIndex = ref<number | null>(null);

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
function supportsAssets(item: StrategyFunctionOption): boolean {
  return props.supportedAssetTypes.every((asset) =>
    item.supportedAssetTypes?.includes(asset),
  );
}
const conditionFunctions = computed(() =>
  props.functions.filter((item) => {
    const output = item.output?.type;
    return (
      supportsAssets(item) &&
      ["boolean", "series<boolean>", "number", "integer", "series<number>"].includes(output || "")
    );
  }),
);
function functionById(id: string): StrategyFunctionOption | undefined {
  return props.functions.find((item) => item.id === id);
}
function defaultOperand(input: { name: string; type: string; default?: unknown }): StrategyOperand {
  if (input.type.startsWith("series<")) {
    const field = ["open", "high", "low", "close", "volume", "amount", "open_interest", "settlement"].includes(input.name)
      ? input.name
      : "close";
    return {
      kind: "series",
      field: field as Extract<StrategyOperand, { kind: "series" }>["field"],
    };
  }
  if (input.type === "boolean") return { kind: "literal", value: false };
  if (["number", "integer"].includes(input.type)) {
    return { kind: "literal", value: typeof input.default === "number" ? input.default : input.type === "integer" ? 20 : 0 };
  }
  return { kind: "literal", value: String(input.default ?? "") };
}
function defaultCall(item: StrategyFunctionOption): StrategyFunctionCall {
  return {
    functionId: item.id,
    version: item.version || 1,
    arguments: (item.inputs || []).map(defaultOperand),
  };
}
function fallbackCondition(): StrategyConditionRule {
  const item = conditionFunctions.value[0];
  return {
    nodeType: "condition",
    left: item
      ? defaultCall(item)
      : { functionId: "condition.crossover", version: 1, arguments: [] },
  };
}
function defaultGroup(): StrategyRule {
  return { nodeType: "group", operator: "AND", children: [fallbackCondition()] };
}
function emitNode(value: StrategyRule): void {
  emit("update:modelValue", clone(value));
}
function updateOperator(value: "AND" | "OR" | "NOT"): void {
  if (props.modelValue.nodeType !== "group") return;
  const children = value === "NOT" ? [props.modelValue.children[0] || fallbackCondition()] : props.modelValue.children;
  emitNode({ ...props.modelValue, operator: value, children });
}
function updateChild(index: number, child: StrategyRule): void {
  if (props.modelValue.nodeType !== "group") return;
  const children = [...props.modelValue.children];
  children[index] = child;
  emitNode({ ...props.modelValue, children });
}
function addCondition(): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  emitNode({ ...props.modelValue, children: [...props.modelValue.children, fallbackCondition()] });
}
function addGroup(): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  emitNode({ ...props.modelValue, children: [...props.modelValue.children, defaultGroup()] });
}
function duplicateChild(index: number): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  const children = [...props.modelValue.children];
  children.splice(index + 1, 0, clone(children[index]));
  emitNode({ ...props.modelValue, children });
}
function removeChild(index: number): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  const children = props.modelValue.children.filter((_item, current) => current !== index);
  emitNode({ ...props.modelValue, children: children.length ? children : [fallbackCondition()] });
}
function moveChild(index: number, delta: number): void {
  if (props.modelValue.nodeType !== "group") return;
  const target = index + delta;
  if (target < 0 || target >= props.modelValue.children.length) return;
  const children = [...props.modelValue.children];
  [children[index], children[target]] = [children[target], children[index]];
  emitNode({ ...props.modelValue, children });
}
function startDrag(index: number, event: DragEvent): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  draggingChildIndex.value = index;
  event.dataTransfer?.setData("text/plain", `${props.path}.${index}`);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}
function dropChild(index: number): void {
  if (props.modelValue.nodeType !== "group" || props.modelValue.operator === "NOT") return;
  const source = draggingChildIndex.value;
  draggingChildIndex.value = null;
  if (source === null || source === index) return;
  const children = [...props.modelValue.children];
  const [moved] = children.splice(source, 1);
  children.splice(source < index ? index - 1 : index, 0, moved);
  emitNode({ ...props.modelValue, children });
}
function updateConditionCall(call: StrategyFunctionCall): void {
  if (props.modelValue.nodeType !== "condition") return;
  const output = functionById(call.functionId)?.output?.type;
  emitNode({
    ...props.modelValue,
    left: call,
    ...(output === "boolean" || output === "series<boolean>"
      ? {}
      : { comparator: props.modelValue.comparator || "gt", right: props.modelValue.right || { kind: "literal", value: 0 } }),
  });
}
function selectConditionFunction(id: string): void {
  const item = functionById(id);
  if (item) updateConditionCall(defaultCall(item));
}
function updateArgument(index: number, operand: StrategyOperand): void {
  if (props.modelValue.nodeType !== "condition") return;
  const arguments_ = [...props.modelValue.left.arguments];
  arguments_[index] = operand;
  updateConditionCall({ ...props.modelValue.left, arguments: arguments_ });
}
function setComparator(value: string): void {
  if (props.modelValue.nodeType !== "condition") return;
  emitNode({ ...props.modelValue, comparator: value as StrategyConditionRule["comparator"], right: props.modelValue.right || { kind: "literal", value: 0 } });
}
function updateRight(value: StrategyOperand): void {
  if (props.modelValue.nodeType !== "condition") return;
  emitNode({ ...props.modelValue, right: value });
}
function conditionIsBoolean(rule: StrategyConditionRule): boolean {
  const output = functionById(rule.left.functionId)?.output?.type;
  return output === "boolean" || output === "series<boolean>";
}
</script>

<template>
  <section
    v-if="modelValue.nodeType === 'group'"
    class="rule-group"
    :data-rule-path="path"
  >
    <header>
      <b>{{ root ? "根规则组" : "规则组" }}</b>
      <el-select
        :model-value="modelValue.operator"
        :aria-label="`逻辑运算 ${path}`"
        size="small"
        @change="updateOperator($event as 'AND' | 'OR' | 'NOT')"
      >
        <el-option label="AND（全部满足）" value="AND" />
        <el-option label="OR（满足任一）" value="OR" />
        <el-option label="NOT（取反）" value="NOT" />
      </el-select>
      <el-button
        v-if="modelValue.operator !== 'NOT'"
        text
        size="small"
        @click="addCondition"
        >添加条件</el-button
      ><el-button
        v-if="modelValue.operator !== 'NOT'"
        text
        size="small"
        @click="addGroup"
        >添加规则组</el-button
      >
    </header>
    <p v-if="errors[path]" class="rule-error">{{ errors[path] }}</p>
    <div class="rule-children">
      <div
        v-for="(child, index) in modelValue.children"
        :key="`${path}.${index}`"
        class="rule-child"
        :class="{ 'is-dragging': draggingChildIndex === index }"
        @dragover.prevent
        @drop="dropChild(index)"
      >
        <button
          v-if="modelValue.operator !== 'NOT'"
          type="button"
          class="drag-handle"
          :aria-label="`拖动排序节点 ${path}.${index}`"
          draggable="true"
          @dragstart="startDrag(index, $event)"
          @dragend="draggingChildIndex = null"
          >↕</button
        >
        <StrategyRuleTreeEditor
          :model-value="child"
          :functions="functions"
          :supported-asset-types="supportedAssetTypes"
          :path="`${path}.${index}`"
          :errors="errors"
          @update:model-value="updateChild(index, $event)"
        />
        <div v-if="modelValue.operator !== 'NOT'" class="node-actions">
          <el-button :disabled="index === 0" text size="small" @click="moveChild(index, -1)">上移</el-button>
          <el-button :disabled="index === modelValue.children.length - 1" text size="small" @click="moveChild(index, 1)">下移</el-button>
          <el-button text size="small" @click="duplicateChild(index)">复制节点</el-button>
          <el-button text type="danger" size="small" @click="removeChild(index)">删除</el-button>
        </div>
      </div>
    </div>
  </section>
  <section v-else class="rule-condition" :data-rule-path="path">
    <header>
      <b>条件</b>
      <el-select
        :model-value="modelValue.left.functionId"
        filterable
        :aria-label="`策略函数 ${path}`"
        @change="selectConditionFunction(String($event))"
      >
        <el-option
          v-for="item in conditionFunctions"
          :key="`${item.id}@${item.version || 1}`"
          :label="item.name"
          :value="item.id"
        />
      </el-select>
    </header>
    <p v-if="errors[path]" class="rule-error">{{ errors[path] }}</p>
    <div class="condition-arguments">
      <label
        v-for="(input, index) in (functionById(modelValue.left.functionId)?.inputs || [])"
        :key="`${input.name}-${index}`"
        ><span>{{ input.name }} · {{ input.type }}</span
        ><StrategyOperandEditor
          :model-value="modelValue.left.arguments[index]"
          :expected-type="input.type"
          :functions="functions"
          :supported-asset-types="supportedAssetTypes"
          @update:model-value="updateArgument(index, $event)"
      /></label>
    </div>
    <div v-if="!conditionIsBoolean(modelValue)" class="condition-comparator">
      <el-select
        :model-value="modelValue.comparator || 'gt'"
        aria-label="比较运算"
        @change="setComparator(String($event))"
      >
        <el-option label=">" value="gt" /><el-option label="≥" value="gte" />
        <el-option label="=" value="eq" /><el-option label="≤" value="lte" />
        <el-option label="<" value="lt" /><el-option label="≠" value="ne" />
      </el-select>
      <StrategyOperandEditor
        :model-value="modelValue.right || { kind: 'literal', value: 0 }"
        expected-type="number"
        :functions="functions"
        :supported-asset-types="supportedAssetTypes"
        @update:model-value="updateRight"
      />
    </div>
  </section>
</template>

<style scoped>
.rule-group,
.rule-condition {
  display: grid;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: var(--ml-background);
}
.rule-group > header,
.rule-condition > header,
.node-actions,
.condition-comparator {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}
.rule-group > header :deep(.el-select) {
  width: 160px;
}
.rule-condition > header :deep(.el-select) {
  min-width: min(100%, 260px);
}
.rule-children {
  display: grid;
  gap: 8px;
  padding-left: 10px;
  border-left: 2px solid var(--ml-divider);
}
.rule-child {
  position: relative;
  display: grid;
  gap: 4px;
}
.rule-child.is-dragging {
  opacity: 0.55;
}
.drag-handle {
  position: absolute;
  z-index: 1;
  top: 14px;
  right: 12px;
  min-width: 24px;
  border: 0;
  border-radius: 4px;
  background: var(--ml-surface, var(--ml-background));
  color: var(--ml-text-secondary);
  cursor: grab;
}
.drag-handle:active {
  cursor: grabbing;
}
.node-actions {
  justify-content: flex-end;
}
.condition-arguments {
  display: grid;
  gap: 7px;
}
.condition-arguments > label {
  display: grid;
  gap: 3px;
  color: var(--ml-text-secondary);
  font-size: 11px;
}
.condition-comparator :deep(.el-select) {
  width: 80px;
}
.condition-comparator > :last-child {
  flex: 1;
  min-width: 180px;
}
.rule-error {
  margin: 0;
  color: var(--ml-error);
  font-size: 12px;
}
@media (max-width: 760px) {
  .node-actions {
    justify-content: flex-start;
  }
}
</style>
