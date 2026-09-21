<script setup lang="ts">
import { ref } from 'vue';

withDefaults(
  defineProps<{
    ariaLabel?: string;
    busy?: boolean;
    topSpacer?: number;
    bottomSpacer?: number;
    rowHeight?: number;
    headerHeight?: number;
  }>(),
  {
    ariaLabel: '行情列表',
    busy: false,
    topSpacer: 0,
    bottomSpacer: 0,
    rowHeight: 33,
    headerHeight: 30,
  },
);

const viewport = ref<HTMLDivElement>();
const emit = defineEmits<{
  scroll: [event: Event];
  wheel: [event: WheelEvent];
}>();

defineExpose({
  getViewport: () => viewport.value,
});
</script>

<template>
  <section
    class="instrument-list-frame"
    role="grid"
    :aria-label="ariaLabel"
    :aria-busy="busy"
    :style="{
      '--instrument-row-height': `${rowHeight}px`,
      '--instrument-header-height': `${headerHeight}px`,
    }"
  >
    <div v-if="$slots.header" class="instrument-list-frame__header" role="rowgroup">
      <slot name="header" />
    </div>

    <div
      ref="viewport"
      class="instrument-list-frame__viewport"
      @scroll.passive="emit('scroll', $event)"
      @wheel="emit('wheel', $event)"
    >
      <div
        v-if="topSpacer > 0"
        class="instrument-list-frame__spacer"
        :style="{ height: `${topSpacer}px` }"
        aria-hidden="true"
      />

      <div class="instrument-list-frame__rows" role="rowgroup">
        <slot />
      </div>

      <div
        v-if="bottomSpacer > 0"
        class="instrument-list-frame__spacer"
        :style="{ height: `${bottomSpacer}px` }"
        aria-hidden="true"
      />
    </div>
  </section>
</template>

<style scoped>
.instrument-list-frame {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
  height: 100%;
  color: var(--ml-text-primary);
  background: var(--ml-surface);
}

.instrument-list-frame__header {
  min-height: var(--instrument-header-height, 30px);
  position: relative;
  z-index: var(--ml-layer-sticky);
}

.instrument-list-frame__viewport {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
}

.instrument-list-frame__rows {
  min-width: max-content;
}

.instrument-list-frame__rows :deep([role='row']) {
  min-height: var(--instrument-row-height, 33px);
  box-sizing: border-box;
}

.instrument-list-frame__spacer {
  min-width: max-content;
  pointer-events: none;
}
</style>
