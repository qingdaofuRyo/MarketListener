<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, apiPost, apiPut, formatTime } from "../../domain/api";

type Match = { instrumentId: string; name?: string; upDays20: number | null; return5d: number | null; marketCapYi: number; quoteTime: string; close: number; priceBasis: string; adjustmentEvidence: {actionDays:string[]} | null };
type Unavailable = { instrumentId: string; name?: string; reason: string; quoteName?:string|null; quoteTime?:string|null; lastLocalDay?:string|null };
type Delisted = {instrumentId:string;name:string;delistedAt:string;announcedAt:string;sourceUrl:string};
type SavedScript = { id: string; name: string; source: string; version: number; versionCount: number; savedAt: string };
type ScreenSettings = {mode:"conditions"|"python";source:string;cap:number;upWindow:number;upThreshold:number;returnWindow:number;returnPercent:number;limitWindow:number;useLimits:boolean};
type Batch = { asOf: string; silverRevision: string; universeHash: string; offset: number; processed: number; universeCount: number; hasMore: boolean; requestedInstrumentIds?: string[] | null; belowMarketCap: number; evaluated: number; matches: Match[]; unavailable: Unavailable[]; excludedDelisted?:Delisted[]; poolEvidence: Array<{day:string;kind:string;sha256:string;contentSha256:string}>; corporateActionEvidence?: {status:string;sha256:string|null;localModifiedAt:string|null}; scope: string };
const mode = ref<"conditions" | "python">("conditions");
const upWindow = ref(20), upThreshold = ref(10), returnWindow = ref(5), returnPercent = ref(10), limitWindow = ref(5), cap = ref(200);
const useLimits = ref(true), busy = ref(false), error = ref(""), notice = ref("");
const matches = ref<Match[]>([]), unavailable = ref<Unavailable[]>([]), result = ref<Batch | null>(null);
const excludedDelisted = ref<Delisted[]>([]);
const belowCap = ref(0), evaluated = ref(0);
const snapshotKey = ref(""), runningConfig = ref("");
const snapshotInvalid = ref(false), retryProgress = ref("");
const listingSources = ref<Array<{exchange:string;status:string;count:number;fetchedAt?:string;review?:{affectedCount:number}}>>([]), sourcesBusy = ref(false);
const recoveredAt = ref("");
const progressSettings = ref<ScreenSettings | null>(null);
const progressStorageKey = "marketlistener.stockScreener.progress.v1";
const checkpointStorageKey = "marketlistener.stockScreener.checkpoint.v1";
const checkpointAvailable = ref(false);
let stopRequested = false;
function stopScan(): void { stopRequested = true; }
const storageKey = "marketlistener.stockScreener.python.v1";
const libraryKey = "marketlistener.stockScreener.scripts.v1";
const scriptName = ref("我的选股脚本"), selectedScript = ref("");
const serverScripts = ref<SavedScript[]>([]), savingScript = ref(false);
let scriptCatalogRevision = 0;
const scriptHistory = ref<Array<{version:number;name:string;source:string;savedAt:string}>>([]), historyOpen = ref(false);
const previousSelected = ref("");
function savedLibrary(): Record<string, string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(libraryKey) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([name, script]) => name.length <= 64 && typeof script === "string" && script.length <= 10000));
  } catch { return {}; }
}
const scriptLibrary = ref<Record<string, string>>(savedLibrary());
const generated = computed(() => {
  const parts = [`value > ${upThreshold.value}`, `period_return(close, ${returnWindow.value}) > ${returnPercent.value / 100}`];
  if (useLimits.value) parts.push(`no_limit_up(limit_up, ${limitWindow.value})`, `no_limit_down(limit_down, ${limitWindow.value})`);
  return `value = up_days(close, ${upWindow.value})\nsignal = ${parts.map(part => `(${part})`).join(" and ")}`;
});
function savedScript(): string {
  try { return localStorage.getItem(storageKey) || generated.value; } catch { return generated.value; }
}
const source = ref(savedScript());
const editorBaseline = ref(JSON.stringify([source.value, scriptName.value]));
function hasScriptChanges(): boolean { return editorBaseline.value !== JSON.stringify([source.value, scriptName.value]); }
function confirmDiscard(): boolean { return !hasScriptChanges() || window.confirm("当前脚本有未保存的修改，是否放弃这些修改？"); }
function confirmLeave(): boolean { return confirmDiscard(); }
function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (!hasScriptChanges()) return;
  event.preventDefault(); event.returnValue = "";
}
async function loadServerScripts(): Promise<void> {
  const revision = scriptCatalogRevision;
  try {
    const response = await apiGet<{items:SavedScript[]}>("/api/strategy/stock-screen/scripts", undefined, {force:true});
    if (revision === scriptCatalogRevision) serverScripts.value = response.items;
  } catch (reason) {
    if (revision === scriptCatalogRevision) notice.value = reason instanceof Error ? reason.message : "本机脚本列表读取失败";
  }
}
async function loadListingSources(): Promise<void> {
  try { listingSources.value = (await apiGet<{items:typeof listingSources.value}>("/api/strategy/stock-screen/delisted-sources", undefined, {force:true})).items; }
  catch { listingSources.value = []; }
}
async function refreshListingSources(): Promise<void> {
  sourcesBusy.value = true; notice.value = "";
  try {
    const response = await apiPost<{items:Array<{exchange:string;status:string}>}>("/api/strategy/stock-screen/delisted-sources/refresh", {});
    const succeeded = response.items.filter(item => item.status === "PASS");
    if (succeeded.length && result.value) { snapshotInvalid.value = true; clearProgress(); }
    await loadListingSources();
    notice.value = succeeded.length === 2 ? `沪深摘牌名单已更新。${result.value ? '已有筛选结果需重新扫描。' : ''}`
      : succeeded.length === 1 ? "部分交易所名单已更新；失败来源保留上次快照。"
      : "沪深摘牌名单本次更新失败，保留上次快照。";
  } catch (reason) { notice.value = reason instanceof Error ? reason.message : "摘牌名单更新失败"; }
  finally { sourcesBusy.value = false; }
}
async function saveScript(asNew = false): Promise<void> {
  const name = scriptName.value.trim();
  if (!name || name.length > 64 || source.value.length > 10000) { notice.value = "请填写 64 字以内的脚本名称，脚本最多 10000 字。"; return; }
  const current = !asNew && selectedScript.value.startsWith("server:")
    ? serverScripts.value.find(item => item.id === selectedScript.value.slice(7)) : undefined;
  if (!asNew && selectedScript.value.startsWith("server:") && !current) { notice.value = "当前脚本已变化，请重新载入。"; return; }
  savingScript.value = true;
  scriptCatalogRevision += 1;
  try {
    const payload = { name, source: source.value };
    const saved = current
      ? await apiPut<SavedScript>(`/api/strategy/stock-screen/scripts/${current.id}`, { ...payload, expectedVersion: current.version })
      : await apiPost<SavedScript>("/api/strategy/stock-screen/scripts", payload);
    serverScripts.value = [...serverScripts.value.filter(item => item.id !== saved.id), saved];
    selectedScript.value = `server:${saved.id}`; previousSelected.value = selectedScript.value;
    editorBaseline.value = JSON.stringify([saved.source, saved.name]);
    notice.value = hasScriptChanges() ? `已保存 v${saved.version}；编辑器仍有未保存的修改`
      : `脚本已保存到本机个人资料（v${saved.version}）`;
  }
  catch (reason) { notice.value = reason instanceof Error ? reason.message : "脚本保存失败"; }
  finally { savingScript.value = false; }
}
function loadScript(name: string): void {
  if (!confirmDiscard()) { selectedScript.value = previousSelected.value; return; }
  if (name.startsWith("server:")) {
    const saved = serverScripts.value.find(item => item.id === name.slice(7));
    if (!saved) return;
    source.value = saved.source; scriptName.value = saved.name; notice.value = `已载入本机脚本 v${saved.version}`;
  } else if (name.startsWith("browser:")) {
    const key = name.slice(8);
    if (!Object.hasOwn(scriptLibrary.value, key)) return;
    source.value = scriptLibrary.value[key]; scriptName.value = key; notice.value = "已载入旧浏览器脚本，可保存到本机个人资料";
  }
  previousSelected.value = name;
  editorBaseline.value = JSON.stringify([source.value, scriptName.value]);
}
function useGenerated(): void {
  if (!confirmDiscard()) return;
  source.value = generated.value; notice.value = "已将当前条件写入脚本编辑器，保存后生效";
}
async function showHistory(): Promise<void> {
  if (!selectedScript.value.startsWith("server:")) return;
  const selection = selectedScript.value;
  try {
    const result = await apiGet<{items:Array<{version:number;name:string;source:string;savedAt:string}>}>(
      `/api/strategy/stock-screen/scripts/${selection.slice(7)}/versions`, undefined, {force:true});
    if (selectedScript.value !== selection) return;
    scriptHistory.value = result.items; historyOpen.value = true;
  } catch (reason) { if (selectedScript.value === selection) notice.value = reason instanceof Error ? reason.message : "脚本历史读取失败"; }
}
function restoreHistory(item: {version:number;name:string;source:string}): void {
  if (!confirmDiscard()) return;
  source.value = item.source; scriptName.value = item.name; historyOpen.value = false;
  notice.value = `已载入 v${item.version}，保存时会创建新版本`;
}
const activeSource = computed(() => mode.value === "conditions" ? generated.value : source.value);
function configKey(): string { return JSON.stringify([activeSource.value, cap.value]); }
function currentSettings(): ScreenSettings {
  return {mode:mode.value,source:source.value,cap:cap.value,upWindow:upWindow.value,upThreshold:upThreshold.value,
    returnWindow:returnWindow.value,returnPercent:returnPercent.value,limitWindow:limitWindow.value,useLimits:useLimits.value};
}
function restoreSettings(value: unknown): void {
  if (!value || typeof value !== "object") return;
  const settings = value as ScreenSettings;
  const integerInRange = (number: unknown, min: number, max: number): boolean =>
    typeof number === "number" && Number.isInteger(number) && number >= min && number <= max;
  if ((settings.mode !== "conditions" && settings.mode !== "python") || typeof settings.source !== "string" || settings.source.length > 10000
    || typeof settings.cap !== "number" || !Number.isFinite(settings.cap) || settings.cap < 0
    || !integerInRange(settings.upWindow,1,60) || !integerInRange(settings.upThreshold,0,60)
    || !integerInRange(settings.returnWindow,1,60) || typeof settings.returnPercent !== "number" || !Number.isFinite(settings.returnPercent)
    || !integerInRange(settings.limitWindow,1,60) || typeof settings.useLimits !== "boolean") return;
  mode.value=settings.mode; source.value=settings.source; cap.value=settings.cap; upWindow.value=settings.upWindow;
  upThreshold.value=settings.upThreshold; returnWindow.value=settings.returnWindow; returnPercent.value=settings.returnPercent;
  limitWindow.value=settings.limitWindow; useLimits.value=settings.useLimits; progressSettings.value=settings;
}
const resultsOutdated = computed(() => !!result.value && (snapshotInvalid.value || runningConfig.value !== configKey()));
function signature(data: Batch): string {
  return JSON.stringify([data.asOf, data.silverRevision, data.universeHash, data.universeCount,
    data.poolEvidence.map(item => [item.day, item.kind, item.contentSha256]), data.corporateActionEvidence]);
}
function quoteTimeText(value: string | null | undefined): string {
  return value && /^\d{14}$/.test(value) ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6,8)} ${value.slice(8,10)}:${value.slice(10,12)}` : "未知";
}
function evidenceUrl(value: string): string | undefined {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && ["www.sse.com.cn", "big5.sse.com.cn", "www.szse.cn", "disc.static.szse.cn", "www.nesc.cn", "www.bse.cn"].includes(parsed.hostname) ? parsed.href : undefined;
  } catch { return undefined; }
}
function clearProgress(): void {
  try { sessionStorage.removeItem(progressStorageKey); } catch { /* storage may be unavailable */ }
  recoveredAt.value = "";
}
function serializedProgress(): string {
  return JSON.stringify({
      version: 1, savedAt: new Date().toISOString(), batch: { ...result.value, matches: [], unavailable: [] },
      matches: matches.value, unavailable: unavailable.value, excludedDelisted: excludedDelisted.value, belowCap: belowCap.value,
      evaluated: evaluated.value, snapshotKey: snapshotKey.value, runningConfig: runningConfig.value, settings:progressSettings.value,
  });
}
function saveProgress(): void {
  if (!result.value || snapshotInvalid.value) return;
  try {
    sessionStorage.setItem(progressStorageKey, serializedProgress());
  } catch { notice.value = "浏览器无法暂存扫描进度，刷新后需重新扫描。"; }
}
function restoreProgress(raw: string | null): boolean {
  try {
    if (!raw || raw.length > 4_000_000) return false;
    const state = JSON.parse(raw);
    const excluded = state.excludedDelisted ?? [];
    const batch = state?.batch as Batch | undefined;
    const count = batch ? batch.offset + batch.processed : -1;
    const validItems = (items: unknown): boolean => Array.isArray(items) && items.length <= 100_000 &&
      items.every(item => item && typeof item.instrumentId === "string" && /^CN\.(SSE|SZSE|BSE)\.STOCK\.\d{6}$/.test(item.instrumentId));
    if (state.version !== 1 || !batch || typeof state.savedAt !== "string" || !Number.isFinite(Date.parse(state.savedAt))
      || typeof state.snapshotKey !== "string" || state.snapshotKey !== signature(batch)
      || typeof state.runningConfig !== "string" || state.runningConfig.length > 12000
      || !Number.isSafeInteger(batch.offset) || !Number.isSafeInteger(batch.processed) || !Number.isSafeInteger(batch.universeCount)
      || batch.offset < 0 || batch.processed < 0 || count > batch.universeCount || batch.universeCount > 100_000
      || !Number.isSafeInteger(state.belowCap) || state.belowCap < 0 || !Number.isSafeInteger(state.evaluated) || state.evaluated < 0
      || !validItems(state.matches) || !validItems(state.unavailable) || !validItems(excluded)
      || state.belowCap + state.evaluated + state.unavailable.length + excluded.length !== count
      || state.matches.length > state.evaluated
      || new Set([...state.matches, ...state.unavailable, ...excluded].map(item => item.instrumentId)).size !== state.matches.length + state.unavailable.length + excluded.length) {
      return false;
    }
    const validMatch = state.matches.every((item: Match) => typeof item.marketCapYi === "number" && Number.isFinite(item.marketCapYi)
      && (item.return5d === null || typeof item.return5d === "number" && Number.isFinite(item.return5d)));
    if (!validMatch || !state.unavailable.every((item: Unavailable) => typeof item.reason === "string")
      || !excluded.every((item: Delisted) => typeof item.name === "string" && typeof item.delistedAt === "string" && typeof item.sourceUrl === "string" && !!evidenceUrl(item.sourceUrl))) return false;
    result.value = batch; matches.value = state.matches; unavailable.value = state.unavailable;
    excludedDelisted.value = excluded;
    belowCap.value = state.belowCap; evaluated.value = state.evaluated; snapshotKey.value = state.snapshotKey;
    runningConfig.value = state.runningConfig; restoreSettings(state.settings); recoveredAt.value = state.savedAt;
    snapshotInvalid.value = false;
    return true;
  } catch { return false; }
}
function saveCheckpoint(): void {
  if (!result.value || resultsOutdated.value) return;
  try {
    const progress = serializedProgress();
    if (progress.length > 4_000_000) throw new Error("扫描进度超过大小限制");
    localStorage.setItem(checkpointStorageKey, progress);
    checkpointAvailable.value = true;
    notice.value = "扫描进度已保存到此浏览器；关闭标签页后可手动载入，不随个人资料备份。";
  } catch { notice.value = "浏览器存储不可用或空间不足，未保存扫描进度。"; }
}
function loadCheckpoint(): void {
  if (!confirmDiscard()) return;
  try {
    const raw = localStorage.getItem(checkpointStorageKey);
    if (!restoreProgress(raw)) { notice.value = "保存的进度无法读取或格式无效，当前结果未更改。"; return; }
    saveProgress();
    notice.value = "已载入浏览器保存的进度，继续扫描前会重新核对来源。";
  } catch { notice.value = "浏览器存储不可用，当前结果未更改。"; }
}
async function fetchBatch(next: boolean): Promise<boolean> {
  const key = configKey();
  const settings = currentSettings();
  if (next && key !== runningConfig.value) { error.value = "条件已修改，请重新从第一批开始。"; return false; }
  try {
    const data = await apiPost<Batch>("/api/strategy/stock-screen", {
      source: activeSource.value, minMarketCapYi: cap.value,
      offset: next && result.value ? result.value.offset + result.value.processed : 0, limit: 50,
    });
    const currentSignature = signature(data);
    if (next && currentSignature !== snapshotKey.value) {
      error.value = "行情或涨跌停池数据已变化，请从第一批重新扫描。";
      snapshotInvalid.value = true; clearProgress(); return false;
    }
    if (!next) { matches.value = []; unavailable.value = []; excludedDelisted.value = []; belowCap.value = 0; evaluated.value = 0; snapshotKey.value = currentSignature; runningConfig.value = key; progressSettings.value = settings; snapshotInvalid.value = false; }
    matches.value.push(...data.matches); unavailable.value.push(...data.unavailable);
    excludedDelisted.value.push(...(data.excludedDelisted ?? []));
    belowCap.value += data.belowMarketCap; evaluated.value += data.evaluated; result.value = data;
    recoveredAt.value = ""; saveProgress();
    return data.hasMore;
  } catch (reason) { error.value = reason instanceof Error ? reason.message : "筛选失败，请检查数据来源后重试"; return false; }
}
async function run(next = false, all = false): Promise<void> {
  busy.value = true; error.value = ""; notice.value = ""; retryProgress.value = ""; stopRequested = false;
  if (!next) { clearProgress(); result.value = null; matches.value = []; unavailable.value = []; excludedDelisted.value = []; belowCap.value = 0; evaluated.value = 0; }
  try {
    let hasMore = await fetchBatch(next);
    while (all && hasMore && !stopRequested) hasMore = await fetchBatch(true);
    if (stopRequested) notice.value = "已在当前批次结束后停止，可继续扫描下一批。";
  } finally { busy.value = false; }
}
async function retryUnavailable(): Promise<void> {
  if (!result.value || resultsOutdated.value || !unavailable.value.length) return;
  const pendingIds = unavailable.value.map(item => item.instrumentId);
  const key = configKey();
  let retried = 0, resolved = 0;
  busy.value = true; error.value = ""; notice.value = ""; stopRequested = false;
  retryProgress.value = `已重试 0 / ${pendingIds.length} 只`;
  try {
    for (let start = 0; start < pendingIds.length && !stopRequested; start += 50) {
      if (configKey() !== key) { error.value = "条件已修改，请重新扫描。"; break; }
      const ids = pendingIds.slice(start, start + 50);
      const data = await apiPost<Batch>("/api/strategy/stock-screen", {
        source: activeSource.value, minMarketCapYi: cap.value, offset: 0, limit: ids.length, instrumentIds: ids,
      });
      if (configKey() !== key) { error.value = "条件已修改，本批重试结果未合并。"; break; }
      if (signature(data) !== snapshotKey.value) {
        snapshotInvalid.value = true;
        clearProgress();
        error.value = "行情、标的名单或来源数据已变化；旧结果保留供参考，请重新扫描。"; break;
      }
      const selected = new Set(ids);
      const delisted = data.excludedDelisted ?? [];
      const returned = [...data.matches.map(item => item.instrumentId), ...data.unavailable.map(item => item.instrumentId), ...delisted.map(item => item.instrumentId)];
      if (data.processed !== ids.length || JSON.stringify(data.requestedInstrumentIds) !== JSON.stringify(ids)
          || returned.some(id => !selected.has(id)) || new Set(returned).size !== returned.length
          || data.belowMarketCap + data.evaluated + data.unavailable.length + delisted.length !== ids.length
          || data.matches.length > data.evaluated) {
        error.value = "重试批次返回不一致，未合并该批结果。"; break;
      }
      unavailable.value = [...unavailable.value.filter(item => !selected.has(item.instrumentId)), ...data.unavailable];
      matches.value.push(...data.matches);
      excludedDelisted.value.push(...delisted);
      belowCap.value += data.belowMarketCap; evaluated.value += data.evaluated;
      recoveredAt.value = ""; saveProgress();
      retried += ids.length; resolved += ids.length - data.unavailable.length;
      retryProgress.value = `已重试 ${retried} / ${pendingIds.length} 只`;
    }
    if (!error.value) notice.value = `${stopRequested ? "已停止；" : ""}本次重试 ${retried} 只，恢复 ${resolved} 只，仍待核 ${unavailable.value.length} 只。`;
  } catch (reason) { error.value = reason instanceof Error ? reason.message : "待核重试失败，请稍后再试"; }
  finally { busy.value = false; retryProgress.value = ""; }
}
defineExpose({ confirmLeave });
onBeforeUnmount(() => { stopRequested = true; window.removeEventListener("beforeunload", warnBeforeUnload); });
onMounted(() => {
  window.addEventListener("beforeunload", warnBeforeUnload);
  try {
    const raw = sessionStorage.getItem(progressStorageKey);
    if (raw && !restoreProgress(raw)) clearProgress();
    checkpointAvailable.value = localStorage.getItem(checkpointStorageKey) !== null;
  } catch { /* unavailable browser storage does not block screening */ }
  void loadServerScripts(); void loadListingSources();
});
</script>

<template>
  <section class="screener" aria-labelledby="screener-title">
    <header class="screener-heading"><div><h2 id="screener-title">A 股条件选股</h2><p>组合行情条件，逐批检查本机 A 股；缺失数据会列入待核。</p></div></header>
    <div class="screen-layout">
      <div class="screen-editor">
        <el-radio-group v-model="mode" :disabled="busy" aria-label="条件编辑方式"><el-radio-button value="conditions">条件编辑</el-radio-button><el-radio-button value="python">Python 表达式</el-radio-button></el-radio-group>
        <div v-if="mode === 'conditions'" class="condition-list">
          <div><span>近</span><el-input-number v-model="upWindow" :min="1" :max="60" :controls="false" aria-label="上涨天数回看交易日"/><span>个交易日，上涨天数大于</span><el-input-number v-model="upThreshold" :min="0" :max="60" :controls="false" aria-label="上涨天数阈值"/><span>天</span></div>
          <div><span>近</span><el-input-number v-model="returnWindow" :min="1" :max="60" :controls="false" aria-label="涨幅回看交易日"/><span>个交易日的区间涨幅大于</span><el-input-number v-model="returnPercent" :controls="false" aria-label="区间涨幅阈值"/><span>%</span></div>
          <div><el-checkbox v-model="useLimits">近</el-checkbox><el-input-number v-model="limitWindow" :disabled="!useLimits" :min="1" :max="60" :controls="false" aria-label="涨跌停回看交易日"/><span>个交易日收盘没有涨停或跌停</span></div>
        </div>
        <div v-else class="script-editor">
          <el-input v-model="source" type="textarea" :rows="7" aria-label="选股 Python 表达式" spellcheck="false" />
          <div class="script-actions"><el-button size="small" :disabled="savingScript" @click="useGenerated">根据条件生成脚本</el-button><el-input v-model="scriptName" aria-label="脚本名称" maxlength="64" placeholder="脚本名称"/><el-button size="small" :loading="savingScript" @click="saveScript(false)">{{ selectedScript.startsWith('server:') ? '保存新版本' : '保存脚本' }}</el-button><el-button v-if="selectedScript.startsWith('server:')" size="small" :disabled="savingScript" @click="saveScript(true)">另存为新脚本</el-button><el-button v-if="selectedScript.startsWith('server:')" size="small" :disabled="savingScript" @click="showHistory">历史版本</el-button><el-select v-model="selectedScript" :disabled="savingScript" placeholder="载入已保存脚本" aria-label="载入已保存脚本" @change="loadScript"><el-option-group label="本机个人资料"><el-option v-for="item in serverScripts" :key="item.id" :label="`${item.name} · v${item.version}`" :value="`server:${item.id}`"/></el-option-group><el-option-group v-if="Object.keys(scriptLibrary).length" label="旧浏览器脚本"><el-option v-for="name in Object.keys(scriptLibrary)" :key="name" :label="name" :value="`browser:${name}`"/></el-option-group></el-select></div>
          <p>脚本保存在本机个人资料并可随个人备份保存。支持赋值、比较、and/or，以及右侧列出的函数；不支持导入、文件访问、循环或任意 Python 库。</p>
        </div>
        <div class="scope"><span>范围：沪深京 A 股个股</span><label for="market-cap-threshold">总市值大于</label><el-input-number id="market-cap-threshold" v-model="cap" :min="0" :controls="false" aria-label="总市值阈值（亿元）"/><span>亿元</span></div>
        <div class="run-actions"><el-button type="primary" :disabled="busy || sourcesBusy" @click="run(false, true)">扫描全部已收录 A 股</el-button><el-button :disabled="busy || sourcesBusy" @click="run(false)">仅扫描第一批</el-button><el-button v-if="busy" @click="stopScan">停止扫描</el-button><el-button v-if="checkpointAvailable" :disabled="busy || sourcesBusy" @click="loadCheckpoint">载入已保存进度</el-button><span>{{ retryProgress || '每批 50 只，首次读取除权资料可能需要半分钟。' }}</span></div>
        <div class="listing-source"><span>摘牌名单：</span><span v-for="item in listingSources" :key="item.exchange">{{ item.exchange === 'SSE' ? '上交所' : '深交所' }} {{ item.status === 'PASS' ? `${item.count} 条（本机抓取 ${formatTime(item.fetchedAt)}）` : item.status === 'INVALID' ? '快照待核' : '尚未采集' }}{{ item.review ? `；历史变动 ${item.review.affectedCount} 只待核` : '' }}</span><el-button size="small" :disabled="busy" :loading="sourcesBusy" @click="refreshListingSources">更新沪深摘牌名单</el-button><small>北交所仅核验个别公告，尚未接入完整名单；抓取时间不代表公告发布时间。</small></div>
        <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon/>
        <p v-if="notice" role="status" class="notice">{{ notice }}</p>
      </div>
      <aside class="screen-reference"><h3>计算口径与函数</h3><p>上涨日：收盘高于前一交易日收盘。区间涨幅：最新收盘 ÷ 窗口前收盘 − 1。本机记录到除权时，核对腾讯原价后使用前复权价格；未记录不代表一定没有除权。</p><code>up_days(close, 20)</code><code>period_return(close, 5)</code><code>no_limit_up(limit_up, 5)</code><code>no_limit_down(limit_down, 5)</code><code>sum_daily_returns(close, 5)</code><small>市值为腾讯同日快照，收盘涨跌停来自东方财富；历史不足、停牌或来源缺报不会视为不涨跌停。</small></aside>
    </div>
    <section v-if="result" class="screen-results" aria-live="polite">
      <el-alert v-if="recoveredAt" title="已恢复本标签页上次扫描进度；结果尚未重新核验。继续扫描或重试时会检查数据版本和来源。" type="info" :closable="false" show-icon/>
      <el-alert v-if="resultsOutdated" :title="snapshotInvalid ? '数据版本或来源已变化，以下是旧结果；请重新扫描。' : '条件已修改，以下是旧条件的结果；请重新扫描。'" type="warning" :closable="false" show-icon/>
      <div class="results-heading"><div><h3>筛选结果 <span>{{ matches.length }} 只</span></h3><p>截至 {{ result.asOf }} · 已扫描 {{ result.offset + result.processed }} / {{ result.universeCount }} 只 · 已核验摘牌 {{ excludedDelisted.length }} 只 · 市值未达门槛 {{ belowCap }} 只 · 已计算条件 {{ evaluated }} 只 · 待核 {{ unavailable.length }} 只</p></div><div class="result-actions"><el-button :disabled="busy || sourcesBusy || resultsOutdated" @click="saveCheckpoint">{{ checkpointAvailable ? '覆盖浏览器进度' : '保存进度到浏览器' }}</el-button><el-button v-if="unavailable.length" :disabled="busy || sourcesBusy || resultsOutdated" @click="retryUnavailable">重试待核</el-button><el-button v-if="result.hasMore" :disabled="busy || sourcesBusy || resultsOutdated" @click="run(true)">扫描下一批</el-button></div></div>
      <el-table :data="matches" row-key="instrumentId" max-height="380" empty-text="已扫描范围内没有符合条件的股票">
        <el-table-column prop="name" label="名称" min-width="110"/><el-table-column prop="instrumentId" label="标的" min-width="190"/><el-table-column label="近 20 日上涨" width="120"><template #default="{row}">{{ row.upDays20 ?? '—' }}</template></el-table-column><el-table-column label="近 5 日涨幅" width="120"><template #default="{row}">{{ row.return5d == null ? '—' : (row.return5d * 100).toFixed(2) + '%' }}</template></el-table-column><el-table-column label="总市值（亿元）" min-width="150"><template #default="{row}">{{ row.marketCapYi.toFixed(2) }}</template></el-table-column><el-table-column label="价格口径" min-width="150"><template #default="{row}"><span :title="row.adjustmentEvidence?.actionDays?.join('、') || '本机未记录不代表一定没有除权'">{{ row.priceBasis === 'TENCENT_QFQ_VERIFIED_AGAINST_TDX_RAW' ? '前复权（已核原价）' : '未复权（待核除权）' }}</span></template></el-table-column><el-table-column prop="quoteTime" label="市值报价时间" min-width="150"/>
      </el-table>
      <details v-if="unavailable.length"><summary>查看待核标的（{{ unavailable.length }}）</summary><ul><li v-for="item in unavailable" :key="item.instrumentId">{{ item.quoteName && item.quoteName !== item.name ? item.instrumentId : item.name || item.instrumentId }}：{{ item.reason }}<span v-if="item.quoteTime || item.lastLocalDay || (item.quoteName && item.quoteName !== item.name)">（<template v-if="item.quoteName && item.quoteName !== item.name">本机名称：{{ item.name || '未知' }}；腾讯名称：{{ item.quoteName }}；</template><template v-if="item.quoteTime">腾讯报价时间：{{ quoteTimeText(item.quoteTime) }}；</template><template v-if="item.lastLocalDay">本地日线截至：{{ item.lastLocalDay }}</template>）</span></li></ul></details>
      <details v-if="excludedDelisted.length"><summary>查看已核验摘牌（{{ excludedDelisted.length }}）</summary><ul><li v-for="item in excludedDelisted" :key="item.instrumentId">{{ item.name }}（{{ item.instrumentId }}）：{{ item.delistedAt }} 摘牌 <a v-if="evidenceUrl(item.sourceUrl)" :href="evidenceUrl(item.sourceUrl)" target="_blank" rel="noreferrer">查看公告</a></li></ul></details>
      <p class="scope-note">{{ result.scope }}。摘牌依据为已采集沪深交易所名单及少量单独核验公告；北交所仅有个别公告，不代表完整名单。名单获取时间晚于历史事件，不代表当时已获取。行情、报价和名单分批读取；本机名录不保证覆盖全部上市公司。{{ result.corporateActionEvidence?.status === 'LOCAL_RECORDS_UNVERIFIED' ? `已读取本机除权记录（文件更新时间 ${result.corporateActionEvidence.localModifiedAt?.slice(0, 16).replace('T', ' ') || '未知'}），但其完整性未经证明。` : '本机除权资料不可用，未复权结果需额外核对。' }}涨跌停名单与已存同日汇总家数核对，历史名单的完整性仍受来源限制。结果使用当日收盘信息，不是历史回测或交易建议。</p>
    </section>
    <el-dialog v-model="historyOpen" title="选股脚本历史版本" width="min(740px, 96vw)" append-to-body>
      <div class="history-list"><article v-for="item in scriptHistory" :key="item.version"><header><strong>v{{ item.version }} · {{ item.name }}</strong><span>{{ item.savedAt }}</span><el-button size="small" @click="restoreHistory(item)">载入编辑</el-button></header><pre>{{ item.source }}</pre></article></div>
      <template #footer><el-button @click="historyOpen = false">关闭</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.screener{margin-top:28px;padding-top:24px;border-top:1px solid var(--ml-divider);color:var(--ml-text-primary)}
.screener-heading h2,.results-heading h3{margin:0;font-size:16px}.screener-heading p,.results-heading p,.screen-reference p,.scope-note{color:var(--ml-text-secondary);font-size:12px;line-height:1.7}
.screen-layout{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:20px;margin-top:16px}.screen-editor{min-width:0}.condition-list{display:grid;gap:10px;padding:16px 0}.condition-list>div,.scope,.run-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:13px}.condition-list :deep(.el-input-number),.scope :deep(.el-input-number){width:86px}.scope{padding:13px 0;border-top:1px solid var(--ml-divider)}.scope span:first-child{margin-right:12px}.run-actions{margin:12px 0 16px}.run-actions span,.script-editor p,.screen-reference small{color:var(--ml-text-secondary);font-size:12px;line-height:1.6}.script-editor{padding:16px 0}.script-editor :deep(textarea){font:12px/1.65 Consolas,monospace}.script-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.script-actions :deep(.el-input),.script-actions :deep(.el-select){width:160px}.screen-reference{padding:16px;border:1px solid var(--ml-divider);border-radius:8px;background:var(--ml-surface-elevated)}.screen-reference h3{margin:0;font-size:13px}.screen-reference code{display:block;padding:5px 0;color:var(--ml-text-primary);font:12px Consolas,monospace}.screen-reference small{display:block;margin-top:12px}.screen-results{margin-top:20px}.results-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.results-heading h3 span{font-size:12px;color:var(--ml-text-secondary);font-weight:400}.screen-results details{margin-top:14px;font-size:12px;color:var(--ml-text-secondary)}.screen-results ul{max-height:180px;overflow:auto;line-height:1.8}.notice{font-size:12px;color:var(--ml-accent)}
@media(max-width:800px){.screen-layout{grid-template-columns:1fr}.results-heading{align-items:flex-start;flex-direction:column}}
.history-list{max-height:60vh;overflow:auto}.history-list article{padding:12px 0;border-bottom:1px solid var(--ml-divider)}.history-list header{display:flex;align-items:center;gap:12px}.history-list header span{flex:1;font-size:12px;color:var(--ml-text-secondary)}.history-list pre{overflow:auto;white-space:pre-wrap;font:12px/1.6 Consolas,monospace}
.listing-source{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin:8px 0 16px;color:var(--ml-text-secondary);font-size:12px}.listing-source small{flex-basis:100%;line-height:1.5}
</style>
