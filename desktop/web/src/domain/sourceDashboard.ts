export interface InventoryItem {
  categoryKey: string; market: string; assetType: string; period: string;
  rows: number; instruments: number; earliestBarAt?: string; latestBarAt?: string;
  lastUpdatedAt?: string; sources?: string[];
  sourceDetails?: {providerId: string; name: string}[];
}
export interface StorageGroup {market: string; assetType: string; period: string; bytes: number; files: number; updatedAt?: string}
export interface InventoryPayload {
  inventory: InventoryItem[];
  summary: {rows: number; instruments: number};
  storage?: {available: boolean; bytes: number | null; files: number; missingFiles: number; groups: StorageGroup[]};
  datasets?: {datasetId: string; name: string; source: string; rows?: number | null}[];
}
export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = value === 0 ? 0 : Math.max(0, Math.min(4, Math.floor(Math.log(value) / Math.log(1024))));
  return `${(value / 1024 ** index).toLocaleString("zh-CN", {maximumFractionDigits: 2})} ${units[index]}`;
}
export function occupiedSources(data: InventoryPayload) {
  const sources = new Map<string, {id: string; name: string; categories: Set<string>}>();
  for (const item of data.inventory.filter(item => Number.isFinite(item.rows) && item.rows > 0)) {
    for (const id of item.sources ?? []) {
      const name = item.sourceDetails?.find(source => source.providerId === id)?.name ?? "未登记名称的本地来源";
      const source = sources.get(id) ?? {id, name, categories: new Set<string>()};
      source.categories.add(item.categoryKey); sources.set(id, source);
    }
  }
  return [...sources.values()];
}
export function share(value: number, total: number): number {
  return Number.isFinite(value) && Number.isFinite(total) && total > 0 ? Math.max(0, Math.min(100, value / total * 100)) : 0;
}
