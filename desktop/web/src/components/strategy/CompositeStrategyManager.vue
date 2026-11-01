<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { apiDelete, apiGet, apiPost, apiPut } from "../../domain/api";
import { useTargetMarketStore } from "../../stores/targetMarket";
import ResearchLoadingSkeleton from "../../views/ResearchLoadingSkeleton.vue";
interface Definition {id:string;displayName:string;description:string;period:string;source:string;enabled:boolean;version:number}
interface StrategyFunction {id:string;name:string;version:number;inputs?:{name:string;type:string}[];description?:string}
const target = useTargetMarketStore();
const items=ref<Definition[]>([]), functions=ref<StrategyFunction[]>([]);
const search=ref(""), functionSearch=ref(""), dialog=ref(false), busy=ref(false), error=ref(""), validation=ref("");
const template=ref(""), legacyCount=ref(0), deletedId=ref("");
const loading=ref(true), loaded=ref(false), functionsLoading=ref(true), feedback=ref("");
const statusFilter=ref(""), periodFilter=ref("");
let disposed=false;
const periods=[["5m","5分"],["15m","15分"],["30m","30分"],["1h","60分"],["2h","120分"],["1d","日线"],["1w","周线"],["1mo","月线"],["3mo","季线"],["1y","年线"]];
function initial():Definition {return {id:"",displayName:"",description:"",period:"1d",source:template.value,enabled:false,version:0};}
const form=ref<Definition>(initial());
const editorNameInput=ref<{focus:()=>void}>();
const editorBaseline=ref("");
const hasUnsavedChanges=computed(()=>dialog.value && JSON.stringify(body(form.value))!==editorBaseline.value);
async function load() {
  loading.value=true;error.value="";
  try {
    const result=await apiGet<{items:Definition[];template:string;legacyCount:number}>("/api/composites/definitions",undefined,{force:true});
    if(disposed)return;
    items.value=result.items;template.value=result.template;legacyCount.value=result.legacyCount;loaded.value=true;
  } catch(reason) {if(!disposed)error.value=reason instanceof Error?reason.message:"策略列表读取失败";}
  finally{if(!disposed)loading.value=false;}
}
function openEditor(item:Definition):void {
  form.value={...item};
  editorBaseline.value=JSON.stringify(body(item));
  error.value="";
  validation.value="";
  feedback.value="";
  dialog.value=true;
}
function create() {openEditor(initial());}
function edit(item:Definition) {openEditor({...item});}
function body(item:Definition) {return {displayName:item.displayName,description:item.description,period:item.period,source:item.source,enabled:item.enabled,version:item.version};}
async function confirmLeave():Promise<boolean> {
  if(!hasUnsavedChanges.value)return true;
  try {
    await ElMessageBox.confirm("当前策略的修改尚未保存，离开后将丢失这些修改。", "放弃未保存的修改？", {
      confirmButtonText:"放弃修改",
      cancelButtonText:"继续编辑",
      confirmButtonClass:"el-button--danger",
      type:"warning",
    });
    return true;
  } catch {return false;}
}
async function cancelEditor():Promise<void> {
  if(busy.value)return;
  if(await confirmLeave())dialog.value=false;
}
async function beforeEditorClose(done:()=>void):Promise<void> {
  if(busy.value)return;
  if(await confirmLeave())done();
}
function focusEditorName():void {editorNameInput.value?.focus();}
async function refreshAfterEdit() {
  target.stopScan();
  await load();
  await target.load();
}
async function validate() {
  if(busy.value)return;
  busy.value=true;error.value="";validation.value="";
  const submitted=JSON.stringify(body(form.value));
  try {await apiPost("/api/composites/validate",body(form.value));if(submitted===JSON.stringify(body(form.value)))validation.value="三类规则与函数依赖校验通过";}
  catch(reason){error.value=String(reason);} finally{busy.value=false;}
}
async function save() {
  if(busy.value)return;
  busy.value=true;error.value="";
  try {
    if(form.value.id)await apiPut("/api/composites/definitions/"+form.value.id,body(form.value));
    else await apiPost("/api/composites/definitions",body(form.value));
    editorBaseline.value=JSON.stringify(body(form.value));dialog.value=false;feedback.value="策略已保存，观察结果将在扫描时更新";
    try {await refreshAfterEdit();}
    catch(reason) {error.value=`策略已保存，但列表刷新失败：${reason instanceof Error?reason.message:String(reason)}`;}
  }catch(reason){error.value=String(reason);}finally{busy.value=false;}
}
async function toggle(item:Definition) {
  busy.value=true;error.value="";
  try{await apiPut("/api/composites/definitions/"+item.id,body({...item,enabled:!item.enabled}));await refreshAfterEdit();}
  catch(reason){error.value=String(reason);}finally{busy.value=false;}
}
async function remove(item:Definition) {
  if(!window.confirm("删除组合策略“"+item.displayName+"”？历史记录会保留，可撤销删除。"))return;
  busy.value=true;error.value="";
  try{await apiDelete("/api/composites/definitions/"+item.id);deletedId.value=item.id;await refreshAfterEdit();}
  catch(reason){error.value=String(reason);}finally{busy.value=false;}
}
async function undo() {
  busy.value=true;error.value="";
  try{await apiPost("/api/composites/definitions/"+deletedId.value+"/restore");deletedId.value="";await refreshAfterEdit();}
  catch(reason){error.value=String(reason);}finally{busy.value=false;}
}
const enabledCount=computed(()=>items.value.filter(item=>item.enabled).length);
const sourceLines=computed(()=>form.value.source.split("\n").length);
const filtered=computed(()=>items.value.filter(item=>{
  const term=search.value.trim().toLocaleLowerCase();
  return(!term || (item.displayName+" "+item.description).toLocaleLowerCase().includes(term))
    &&(!statusFilter.value || item.enabled===(statusFilter.value==="enabled"))
    &&(!periodFilter.value || item.period===periodFilter.value);
}));
const functionList=computed(()=>functions.value.filter(item=>(item.name+" "+item.id).toLowerCase().includes(functionSearch.value.toLowerCase())));
function clearFilters(){search.value="";statusFilter.value="";periodFilter.value="";}
function editorShortcut(event:KeyboardEvent){
  if(!(event.ctrlKey || event.metaKey)||busy.value)return;
  if(event.key==="Enter"){event.preventDefault();void validate();}
  if(event.key.toLowerCase()==="s"){event.preventDefault();if(form.value.displayName.trim()&&form.value.source.trim())void save();}
}
async function copyFunction(item:StrategyFunction){
  try{await navigator.clipboard.writeText(`fn("${item.id}", ${item.version}${item.inputs?.length?", "+item.inputs.map(input=>input.name).join(", "):""})`);feedback.value=`已复制 ${item.name} 调用格式`;}
  catch{feedback.value="可在函数详情中选择并复制调用格式";}
}
function warnBeforeUnload(event:BeforeUnloadEvent):void {
  if(!hasUnsavedChanges.value)return;
  event.preventDefault();
  event.returnValue="";
}
watch(form,()=>{validation.value="";},{deep:true});
defineExpose({create,confirmLeave});
onMounted(async()=>{window.addEventListener("beforeunload",warnBeforeUnload);try {
  await load();
  const result=await apiGet<{items:StrategyFunction[]}>("/api/strategy/functions");
  if(!disposed)functions.value=result.items.filter(item=>(item.id.includes(".") || ["up_days", "sum_daily_returns"].includes(item.id)) && item.id !== "market.volume_profile" && item.id !== "market.up_down_count");
}catch(reason){if(!disposed)error.value=String(reason);}finally{if(!disposed)functionsLoading.value=false;}});
onBeforeUnmount(()=>{disposed=true;window.removeEventListener("beforeunload",warnBeforeUnload);});
</script>
<template>
  <section class="composite-strategy-manager">
    <header class="manager-heading"><h2>组合策略</h2><el-button type="primary" :disabled="busy" @click="create">新建组合策略</el-button></header>
    <div class="strategy-overview">
      <div><span>我的策略</span><strong>{{loaded ? items.length : '—'}}</strong></div><div><span>已启用</span><strong class="enabled-number">{{loaded ? enabledCount : '—'}}</strong></div><div><span>已停用</span><strong>{{loaded ? items.length-enabledCount : '—'}}</strong></div>
      <div class="strategy-flow"><span><b>01</b> 关注</span><i>→</i><span><b>02</b> 仓位</span><i>→</i><span><b>03</b> 择时</span><small>在目标行情中观察策略结果</small></div>
    </div>
    <el-alert v-if="error && !dialog" :title="error" type="error" :closable="false"/>
    <div v-if="feedback&&!dialog" class="manager-feedback" role="status">{{feedback}}</div>
    <div class="filters"><el-input v-model="search" aria-label="搜索组合策略" placeholder="搜索策略名称或说明" clearable/><el-select v-model="statusFilter" clearable placeholder="全部状态" aria-label="策略状态筛选"><el-option label="已启用" value="enabled"/><el-option label="已停用" value="disabled"/></el-select><el-select v-model="periodFilter" clearable placeholder="全部周期" aria-label="策略周期筛选"><el-option v-for="[id,label] in periods" :key="id" :value="id" :label="label"/></el-select><span class="result-count">{{filtered.length}} 个策略</span><el-button v-if="deletedId" :disabled="busy" @click="undo">撤销删除</el-button><el-button :disabled="busy||loading" @click="void load()">刷新</el-button></div>
    <ResearchLoadingSkeleton v-if="loading&&!loaded" kind="table" :rows="5"/>
    <el-table v-else :data="filtered" row-key="id" max-height="600" highlight-current-row :aria-busy="loading" @row-dblclick="edit">
      <el-table-column prop="displayName" label="策略名称" min-width="230"><template #default="{row}"><button class="strategy-name" @click="edit(row)">{{row.displayName}}</button><p v-if="row.description" class="strategy-description" :title="row.description">{{row.description}}</p></template></el-table-column>
      <el-table-column label="规则组成" min-width="180"><template #default><div class="rule-chips"><span>关注</span><span>仓位</span><span>择时</span></div></template></el-table-column>
      <el-table-column label="周期"><template #default="{row}">{{periods.find(([id])=>id===row.period)?.[1]}}</template></el-table-column>
      <el-table-column label="版本" width="85"><template #default="{row}"><span class="version-label">v{{row.version}}</span></template></el-table-column>
      <el-table-column label="状态" width="115"><template #default="{row}"><span class="strategy-state" :class="{enabled:row.enabled}"><i/>{{row.enabled?'已启用':'已停用'}}</span></template></el-table-column>
      <el-table-column label="管理" min-width="210" fixed="right"><template #default="{row}"><el-button text :disabled="busy" @click="edit(row)">编辑</el-button><el-button text :disabled="busy" @click="toggle(row)">{{row.enabled?'停用':'启用'}}</el-button><el-button text type="danger" :disabled="busy" @click="remove(row)">删除</el-button></template></el-table-column>
      <template #empty><div class="strategy-empty"><span aria-hidden="true">◇</span><h3>{{error?'策略列表暂时无法读取':items.length?'没有匹配的策略':'开始创建你的观察策略'}}</h3><p>{{items.length?'调整名称、状态或周期筛选。':'将关注、仓位与择时规则保存到一个组合策略。'}}</p><el-button v-if="error" @click="void load()">重新读取</el-button><el-button v-else-if="items.length" @click="clearFilters">清除筛选</el-button><el-button v-else type="primary" @click="create">新建策略</el-button></div></template>
    </el-table>
    <details v-if="legacyCount" class="legacy-details"><summary>历史定义 · {{legacyCount}} 条已保留</summary><p>历史操作信号定义继续保留，未合并为关注、仓位、择时三类规则齐全的组合策略。</p></details>
    <el-dialog v-model="dialog" class="strategy-editor-dialog" :title="form.id?'编辑组合策略':'新建组合策略'" width="min(1180px,96vw)" append-to-body :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" :before-close="beforeEditorClose" @opened="focusEditorName">
      <div @keydown="editorShortcut">
      <div class="editor-intro"><p>{{form.id?'修改后将保存为新版本；当前版本继续保留。':'新策略先填写名称与规则，再校验并保存。'}}</p><span class="draft-state" :class="{modified:hasUnsavedChanges}" role="status">{{busy?'正在处理…':hasUnsavedChanges?'有未保存的修改':'尚无修改'}}</span></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" role="alert" class="editor-alert"/>
      <el-alert v-if="validation" :title="validation" type="success" :closable="false" role="status" class="editor-alert"/>
      <el-form label-position="top" :disabled="busy" :aria-busy="busy">
        <div class="form-top"><el-form-item label="组合策略名称" required><el-input ref="editorNameInput" v-model="form.displayName" aria-label="组合策略名称" aria-required="true" maxlength="128" show-word-limit/></el-form-item>
          <el-form-item label="K线周期"><el-select v-model="form.period" aria-label="组合策略周期"><el-option v-for="[id,label] in periods" :key="id" :label="label" :value="id"/></el-select></el-form-item>
          <el-form-item label="启用扫描"><el-switch v-model="form.enabled" aria-label="启用组合策略"/></el-form-item></div>
        <el-form-item label="说明"><el-input v-model="form.description" maxlength="2000" placeholder="可选：记录这套规则的用途和边界"/></el-form-item>
        <div class="code-layout">
          <section class="code-pane"><div class="editor-heading"><h3>规则编辑器 <span class="required-mark" aria-hidden="true">*</span></h3><span>Python · 三类规则</span></div><div class="rule-step-strip"><span><b>attention</b> 关注</span><span><b>position</b> 仓位</span><span><b>timing</b> 择时</span></div><el-input v-model="form.source" type="textarea" :rows="23" aria-label="组合策略Python代码" aria-required="true" spellcheck="false" @input="validation=''"/><div class="editor-status"><span>{{sourceLines}} 行 · {{form.id?'当前版本 v'+form.version:'新策略'}}</span><span>{{validation?'校验通过':'待校验'}}</span></div></section>
          <aside class="function-pane"><div class="editor-heading"><h3>函数参考</h3><span>{{functions.length}} 个函数</span></div><el-input v-model="functionSearch" placeholder="搜索函数名称" aria-label="搜索预定义函数" clearable/><ResearchLoadingSkeleton v-if="functionsLoading" kind="table" :rows="4"/><div v-else class="function-list"><details v-for="item in functionList" :key="item.id+'@'+item.version"><summary>{{item.name}}<small>v{{item.version}}</small></summary><code>fn("{{item.id}}", {{item.version}}{{item.inputs?.length?', '+item.inputs.map(input=>input.name).join(', '):''}})</code><p>{{item.description}}</p><el-button text size="small" @click="void copyFunction(item)">复制调用格式</el-button></details><p v-if="!functionList.length" class="muted">没有匹配函数，试试其他名称。</p></div></aside>
        </div>
      </el-form>
      <details class="editor-help"><summary>返回值与字段说明</summary><p>attention 返回多/空/取消关注条件和比较窗口；position 返回五项配置指标；timing 返回开/加/减/平仓条件。None 表示暂不可用，比例 0.5 表示 50%。direction：多头 1、空头 −1、未关注 0；observed_win_rate 为已结束观察轮次胜率，无样本为 None；margin_rate 为真实保证金率，缺失为 None。</p><p>校验检查规则和函数依赖；实际行情可用性在扫描时检查。</p></details><div v-if="feedback" class="manager-feedback" role="status">{{feedback}}</div>
      </div>
      <template #footer><div class="editor-footer"><span class="shortcut-hint">Ctrl + Enter 校验 · Ctrl + S 保存</span><div><el-button :disabled="busy" @click="void cancelEditor()">取消</el-button><el-button :loading="busy" @click="validate">校验规则</el-button><el-button type="primary" :loading="busy" :disabled="!form.displayName.trim() || !form.source.trim()" @click="save">保存策略</el-button></div></div></template>
    </el-dialog>
  </section>
</template>
<style scoped>
.manager-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }
.manager-heading h2 { margin:0; font-size:14px; font-weight:650; }
.filters{display:flex;gap:12px;max-width:500px;margin:16px 0}.form-top{display:grid;grid-template-columns:2fr 1fr 1fr;gap:16px}
.code-layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:16px}.code-layout :deep(textarea){font-family:Consolas,monospace;font-size:12px;line-height:1.5;white-space:pre;overflow:auto}.function-list{max-height:330px;overflow:auto;margin-top:12px}.function-list details{padding:8px 0;border-bottom:1px solid var(--ml-divider)}.function-list code{white-space:pre-wrap;word-break:break-word}.muted{font-size:12px;color:var(--ml-text-secondary);line-height:1.7}
.strategy-overview { display:grid; grid-template-columns:repeat(3,minmax(85px,1fr)) minmax(270px,2fr); border:1px solid var(--ml-divider); border-radius:10px; background:var(--ml-surface-elevated); overflow:hidden; }
.strategy-overview > div { padding:15px 20px; border-right:1px solid var(--ml-divider); display:grid; gap:6px; }.strategy-overview > div:last-child { border:0; }.strategy-overview span { color:var(--ml-text-secondary); font-size:12px; }.strategy-overview strong { font-size:26px; line-height:1.2; font-variant-numeric:tabular-nums; font-weight:650; }.strategy-overview .enabled-number { color:var(--ml-accent); }
.strategy-flow { display:flex!important; flex-wrap:wrap; justify-content:center; align-content:center; align-items:center; gap:8px 12px!important; }.strategy-flow b { font-size:10px; color:var(--ml-accent); padding-right:4px; }.strategy-flow i { font-style:normal; color:var(--ml-text-disabled); }.strategy-flow small { width:100%; text-align:center; color:var(--ml-text-disabled); font-size:11px; }
.filters { flex-wrap:wrap; align-items:center; gap:10px; max-width:none; margin:20px 0 14px; }.filters > .el-input { flex:1; min-width:190px; max-width:390px; }.filters > .el-select { width:145px; }.result-count { color:var(--ml-text-disabled); font-size:12px; margin-right:auto; font-variant-numeric:tabular-nums; }
.strategy-name { padding:0; color:var(--ml-text-primary); background:none; border:0; font-weight:600; text-align:left; cursor:pointer; line-height:1.7; }.strategy-name:hover { color:var(--ml-accent); }.strategy-name:focus-visible { outline:2px solid var(--ml-accent); outline-offset:3px; }.strategy-description { margin:3px 0 0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ml-text-disabled); font-size:11px; max-width:350px; }
.rule-chips { display:flex; gap:7px; }.rule-chips span { font-size:11px; padding:2px 7px; border:1px solid var(--ml-divider); border-radius:5px; background:var(--ml-surface-elevated); color:var(--ml-text-secondary); }.version-label { font-size:11px; font-family:ui-monospace,Consolas,monospace; color:var(--ml-text-secondary); }
.strategy-state { display:inline-flex; align-items:center; gap:7px; font-size:12px; color:var(--ml-text-secondary); }.strategy-state i { width:6px; height:6px; border-radius:50%; background:var(--ml-text-disabled); }.strategy-state.enabled i { background:var(--ml-accent); box-shadow:0 0 0 3px color-mix(in srgb,var(--ml-accent) 12%,transparent); }
.manager-feedback { padding:8px 11px; margin:10px 0; font-size:12px; color:var(--ml-accent); background:color-mix(in srgb,var(--ml-accent) 7%,transparent); border-radius:7px; }
.strategy-empty { display:grid; gap:10px; justify-items:center; padding:50px 18px; line-height:1.5; }.strategy-empty > span { font-size:34px; color:var(--ml-text-disabled); }.strategy-empty h3,.strategy-empty p { margin:0; }.strategy-empty h3 { font-size:15px; color:var(--ml-text-primary); }.strategy-empty p { font-size:12px; color:var(--ml-text-secondary); }
.legacy-details,.editor-help { margin:14px 0 0; color:var(--ml-text-secondary); font-size:12px; line-height:1.7; }.legacy-details summary,.editor-help summary { cursor:pointer; }.legacy-details p { margin:6px 0; }.editor-help { border:1px solid var(--ml-divider); border-radius:7px; padding:10px 14px; }.editor-help p { margin:9px 0 0; }
.code-layout { grid-template-columns:minmax(0,1fr) 300px; gap:14px; }.code-pane,.function-pane { min-width:0; padding:14px; border:1px solid var(--ml-divider); border-radius:9px; background:var(--ml-surface-elevated); }.editor-heading { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; }.editor-heading h3 { margin:0; font-size:13px; color:var(--ml-text-primary); }.editor-heading span { color:var(--ml-text-disabled); font-size:10px; }.rule-step-strip { display:flex; gap:14px; flex-wrap:wrap; font-size:11px; margin-bottom:10px; color:var(--ml-text-secondary); }.rule-step-strip b { font-family:ui-monospace,Consolas,monospace; color:var(--ml-accent); font-weight:500; }.code-layout :deep(textarea) { line-height:1.7; tab-size:4; border-radius:6px; }.editor-status { display:flex; justify-content:space-between; gap:10px; padding-top:9px; color:var(--ml-text-disabled); font-size:10px; font-variant-numeric:tabular-nums; }
.function-list { max-height:430px; scrollbar-width:thin; }.function-list summary { cursor:pointer; color:var(--ml-text-primary); font-size:12px; }.function-list summary small { float:right; color:var(--ml-text-disabled); }.function-list code { display:block; font-size:11px; padding:10px; margin-top:8px; border-radius:5px; background:var(--ml-background); color:var(--ml-accent); }.function-list p { font-size:11px; color:var(--ml-text-secondary); line-height:1.7; }
.editor-footer { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; }.shortcut-hint { font-size:11px; color:var(--ml-text-disabled); }
@media(max-width:800px) { .strategy-overview { grid-template-columns:repeat(3,1fr); }.strategy-flow { grid-column:1/-1; border-top:1px solid var(--ml-divider)!important; }.strategy-overview > div { padding:14px; }.code-layout { grid-template-columns:minmax(0,1fr); }.form-top { grid-template-columns:minmax(0,1fr) 1fr; }.form-top > :first-child { grid-column:1/-1; }.function-list { max-height:240px; }.filters > .el-input { max-width:none; }.filters > .el-select { flex:1; min-width:120px; }.result-count,.shortcut-hint { display:none; } }
</style>
