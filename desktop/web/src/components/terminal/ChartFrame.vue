<script setup lang="ts">
import DataState from "./DataState.vue";

type ChartFrameStatus =
  | "ready"
  | "loading"
  | "empty"
  | "error"
  | "stale"
  | "unavailable"
  | "partial";

type DisplayState = Exclude<ChartFrameStatus, "ready">;

const props = withDefaults(
  defineProps<{
    status?: ChartFrameStatus;
    title?: string;
    subtitle?: string;
    stateDetail?: string;
    updatedAt?: string;
    sourceStatus?: string;
  }>(),
  {
    status: "ready",
    title: "",
    subtitle: "",
    stateDetail: "",
    updatedAt: "",
    sourceStatus: "",
  },
);

function displayState(): DisplayState {
  return props.status === "ready" ? "empty" : props.status;
}

function isBlockingState(): boolean {
  return ["loading", "empty", "error", "unavailable"].includes(props.status);
}

function isNoticeState(): boolean {
  return props.status === "stale" || props.status === "partial";
}
</script>

<template>
  <section
    class="chart-frame"
    :data-status="status"
    :aria-busy="status === 'loading'"
  >
    <header v-if="title || subtitle || $slots.toolbar" class="chart-frame__header">
      <div v-if="title || subtitle" class="chart-frame__titles">
        <strong v-if="title">{{ title }}</strong>
        <span v-if="subtitle">{{ subtitle }}</span>
      </div>
      <div v-if="$slots.toolbar" class="chart-frame__toolbar">
        <slot name="toolbar" />
      </div>
    </header>

    <div class="chart-frame__body">
      <slot />

      <div v-if="isBlockingState()" class="chart-frame__state">
        <DataState :state="displayState()" :detail="stateDetail">
          <template v-if="$slots.actions" #actions>
            <slot name="actions" />
          </template>
        </DataState>
      </div>

      <DataState
        v-else-if="isNoticeState()"
        class="chart-frame__notice"
        :state="displayState()"
        :detail="stateDetail"
        compact
      />

      <slot name="overlay" />
    </div>

    <footer
      v-if="updatedAt || sourceStatus || $slots.footer"
      class="chart-frame__footer"
    >
      <span v-if="sourceStatus">{{ sourceStatus }}</span>
      <span v-if="updatedAt">{{ updatedAt }}</span>
      <slot name="footer" />
    </footer>
  </section>
</template>

<style scoped>
.chart-frame {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--ml-surface);
}
.chart-frame__header,
.chart-frame__footer {
  display: flex;
  align-items: center;
  gap: var(--ml-space-3, 8px);
  flex: 0 0 auto;
  min-width: 0;
  padding: 0 var(--ml-space-4, 12px);
  color: var(--ml-text-secondary);
  border-color: var(--ml-divider);
  font-size: 12px;
}
.chart-frame__header {
  min-height: var(--ml-control-height-standard, 32px);
  border-bottom: 1px solid var(--ml-divider);
}
.chart-frame__footer {
  min-height: 24px;
  justify-content: flex-end;
  border-top: 1px solid var(--ml-divider);
  font-size: 11px;
}
.chart-frame__titles {
  display: flex;
  align-items: baseline;
  gap: var(--ml-space-2, 6px);
  min-width: 0;
}
.chart-frame__titles strong {
  overflow: hidden;
  color: var(--ml-text-primary);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chart-frame__titles span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chart-frame__toolbar {
  display: flex;
  align-items: center;
  gap: var(--ml-space-2, 6px);
  margin-left: auto;
}
.chart-frame__body {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
.chart-frame__state {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: grid;
  place-items: center;
  background: var(--ml-surface);
}
.chart-frame__notice {
  position: absolute;
  top: var(--ml-space-3, 8px);
  right: var(--ml-space-3, 8px);
  z-index: 2;
  max-width: min(360px, calc(100% - 16px));
  border: 1px solid var(--ml-divider);
  border-radius: var(--ml-radius-control, 6px);
  background: var(--ml-surface-elevated);
}
</style>
