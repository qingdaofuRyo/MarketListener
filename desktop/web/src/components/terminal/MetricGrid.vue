<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";

const props = withDefaults(
  defineProps<{
    groups: readonly (readonly string[])[];
    values: Record<string, string>;
    colors?: Record<string, string>;
    inline?: boolean;
  }>(),
  {
    colors: () => ({}),
    inline: false,
  },
);

const root = ref<HTMLElement>();
const groupWidths = ref<number[]>([]);
let observer: ResizeObserver | undefined;

onMounted(() => {
  observer = new ResizeObserver(() => {
    const widths = Array.from(root.value?.children || [], (element) => element.getBoundingClientRect().width);
    if (widths.some((width, index) => width !== groupWidths.value[index])) groupWidths.value = widths;
  });
  if (root.value) observer.observe(root.value);
});

onBeforeUnmount(() => observer?.disconnect());

function labelWidth(group: number): number {
  const width = groupWidths.value[group] || 132;
  return Math.min(group >= 5 ? 46 : group === 2 || group === 3 ? 24 : 13, width * (group >= 5 ? 0.42 : 0.3));
}

function numericSize(name: string, group: number): string {
  const width = (groupWidths.value[group] || 132) - labelWidth(group) - 2;
  const value = props.values[name] ?? "--";
  return `${Math.min(11, width / Math.max(1, value.length) / 0.68)}px`;
}

function labelSize(name: string, group: number): string {
  return `${Math.min(11, labelWidth(group) / Math.max(1, name.length) / 1.05)}px`;
}
</script>

<template>
  <div ref="root" class="quote-values metric-grid ml-numeric" :class="{ inline }">
    <div v-for="(group, index) in groups" :key="group[0]" class="quote-field-group">
      <p
        v-for="name in group"
        :key="name"
        :data-field="name"
        :title="`${name} ${values[name] ?? '--'}`"
      >
        <span :style="{ fontSize: labelSize(name, index) }">{{ name }}</span>
        <strong
          :style="{
            color: colors[name] || 'var(--ml-text-primary)',
            fontSize: numericSize(name, index),
          }"
        >{{ values[name] ?? "--" }}</strong>
      </p>
    </div>
  </div>
</template>

<style scoped>
.quote-values {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr)) repeat(2, minmax(0, 1.4fr));
  gap: var(--ml-quote-grid-gap);
  width: 100%;
  font-size: clamp(7px, 1.65cqw, 11px);
}
.quote-field-group {
  display: grid;
  grid-template-rows: repeat(2, 18px);
}
.quote-values p {
  display: grid;
  grid-template-columns: min(13px, 30%) minmax(0, 1fr);
  align-items: center;
  gap: var(--ml-quote-label-gap, 2px);
  margin: 0;
  white-space: nowrap;
}
.quote-field-group:nth-child(3) p,
.quote-field-group:nth-child(4) p {
  grid-template-columns: min(24px, 30%) minmax(0, 1fr);
}
.quote-field-group:nth-last-child(-n + 2) p {
  grid-template-columns: min(46px, 42%) minmax(0, 1fr);
}
.quote-values span {
  color: var(--ml-text-secondary);
}
.quote-values strong {
  font-family: Consolas, monospace;
  font-weight: 600;
  white-space: nowrap;
}
</style>
