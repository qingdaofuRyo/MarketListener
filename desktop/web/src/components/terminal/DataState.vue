<script setup lang="ts">
import { computed } from "vue";

type DataStateKind = "loading" | "empty" | "error" | "stale" | "unavailable" | "partial";

const props = withDefaults(
  defineProps<{
    state: DataStateKind;
    title?: string;
    detail?: string;
    compact?: boolean;
  }>(),
  {
    title: "",
    detail: "",
    compact: false,
  },
);

const defaultTitles: Record<DataStateKind, string> = {
  loading: "正在加载",
  empty: "暂无数据",
  error: "加载失败",
  stale: "数据可能已过期",
  unavailable: "数据暂不可用",
  partial: "数据不完整",
};

const titleText = computed(() => props.title || defaultTitles[props.state]);
const role = computed(() => (props.state === "error" ? "alert" : "status"));
</script>

<template>
  <div
    class="data-state"
    :class="[`is-${state}`, { compact }]"
    :role="role"
    :aria-live="state === 'error' ? 'assertive' : 'polite'"
  >
    <span class="data-state__indicator" aria-hidden="true" />
    <div class="data-state__copy">
      <strong>{{ titleText }}</strong>
      <span v-if="detail">{{ detail }}</span>
    </div>
    <div v-if="$slots.actions" class="data-state__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.data-state {
  display: flex;
  align-items: flex-start;
  gap: var(--ml-space-3, 8px);
  min-width: 0;
  padding: var(--ml-space-4, 12px);
  color: var(--ml-text-secondary);
  font-size: 12px;
  line-height: var(--ml-line-height-normal, 1.45);
}
.data-state.compact {
  align-items: center;
  padding: var(--ml-space-2, 6px) var(--ml-space-3, 8px);
  font-size: 11px;
}
.data-state__indicator {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--ml-text-disabled);
}
.compact .data-state__indicator {
  margin-top: 0;
}
.data-state__copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.data-state__copy strong {
  color: var(--ml-text-primary);
  font-weight: 600;
}
.data-state__copy span {
  color: var(--ml-text-secondary);
}
.data-state__actions {
  margin-left: auto;
}
.is-loading .data-state__indicator,
.is-stale .data-state__indicator,
.is-partial .data-state__indicator {
  background: var(--ml-warning);
}
.is-error .data-state__indicator {
  background: var(--ml-error);
}
.is-unavailable .data-state__indicator,
.is-empty .data-state__indicator {
  background: var(--ml-text-disabled);
}
</style>
