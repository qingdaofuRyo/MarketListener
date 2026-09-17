<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiDelete, apiGet, apiPost, apiPut } from "../../domain/api";
import { useTargetMarketStore } from "../../stores/targetMarket";
interface Definition {id:string;displayName:string;description:string;period:string;source:string;enabled:boolean;version:number}
interface StrategyFunction {id:string;name:string;version:number;inputs?:{name:string;type:string}[];description?:string}
const target = useTargetMarketStore();
const items=ref<Definition[]>([]), functions=ref<StrategyFunction[]>([]);
const search=ref(""), functionSearch=ref(""), dialog=ref(false), busy=ref(false), error=ref(""), validation=ref("");
const template=ref(""), legacyCount=ref(0), deletedId=ref("");
let disposed=false;
const periods=[["5m","5分"],["15m","15分"],["30m","30分"],["1h","60分"],["2h","120分"],["1d","日线"],["1w","周线"],["1mo","月线"],["3mo","季线"],["1y","年线"]];
function initial():Definition {return {id:"",displayName:"",description:"",period:"1d",source:template.value,enabled:false,version:0};}
const form=ref<Definition>(initial());
async function load() {
  const result=await apiGet<{items:Definition[];template:string;legacyCount:number}>("/api/composites/definitions",undefined,{force:true});
  if(disposed)return;
  items.value=result.items;template.value=result.template;legacyCount.value=result.legacyCount;
}
function create() {form.value=initial();error.value="";validation.value="";dialog.value=true;}
function edit(item:Definition) {form.value={...item};error.value="";validation.value="";dialog.value=true;}
function body(item:Definition) {return {displayName:item.displayName,description:item.description,period:item.period,source:item.source,enabled:item.enabled,version:item.version};}
async function refreshAfterEdit() {
  target.stopScan();
  await load();
  await target.load();
}
async function validate() {
  busy.value=true;error.value="";validation.value="";
  try {await apiPost("/api/composites/validate",body(form.value));validation.value="三类规则和函数依赖校验通过；实际数据可用性在扫描时检查。";}
  catch(reason){error.value=String(reason);} finally{busy.value=false;}
}
async function save() {
  busy.value=true;error.value="";
  try {
    if(form.value.id)await apiPut("/api/composites/definitions/"+form.value.id,body(form.value));
    else await apiPost("/api/composites/definitions",body(form.value));
    await refreshAfterEdit();dialog.value=false;
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
const filtered=computed(()=>items.value.filter(item=>item.displayName.includes(search.value)));
const functionList=computed(()=>functions.value.filter(item=>(item.name+" "+item.id).toLowerCase().includes(functionSearch.value.toLowerCase())));
defineExpose({create});
onMounted(async()=>{try {
  await load();
  const result=await apiGet<{items:StrategyFunction[]}>("/api/strategy/functions");
  if(!disposed)functions.value=result.items.filter(item=>item.id.includes(".") && item.id !== "market.volume_profile" && item.id !== "market.up_down_count");
}catch(reason){if(!disposed)error.value=String(reason);}});
onBeforeUnmount(()=>{disposed=true;});
</script>
<template>
  <section class="composite-strategy-manager">
    <p class="muted">每个组合策略同时包含关注、仓位、择时。先关注，再计算配置比例和操作时机；目标行情展示各组合策略的观察结果。</p>
    <p v-if="legacyCount" class="muted">已保留 {{legacyCount}} 条历史操作信号定义；它们不属于三类规则齐全的组合策略。</p>
    <el-alert v-if="error && !dialog" :title="error" type="error" :closable="false"/>
    <div class="filters"><el-input v-model="search" aria-label="搜索组合策略" placeholder="搜索组合策略" clearable/><el-button v-if="deletedId" :disabled="busy" @click="undo">撤销删除</el-button></div>
    <el-table :data="filtered" empty-text="暂无组合策略，请新建">
      <el-table-column prop="displayName" label="组合策略" min-width="160"/>
      <el-table-column label="规则组成" min-width="180"><template #default>关注 · 仓位 · 择时</template></el-table-column>
      <el-table-column label="周期"><template #default="{row}">{{periods.find(([id])=>id===row.period)?.[1]}}</template></el-table-column>
      <el-table-column label="状态"><template #default="{row}">{{row.enabled?'已启用':'已停用'}}</template></el-table-column>
      <el-table-column label="管理" min-width="210"><template #default="{row}"><el-button text :disabled="busy" @click="edit(row)">编辑</el-button><el-button text :disabled="busy" @click="toggle(row)">{{row.enabled?'停用':'启用'}}</el-button><el-button text type="danger" :disabled="busy" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>
    <el-dialog v-model="dialog" :title="form.id?'编辑组合策略':'新建组合策略'" width="min(1180px,96vw)" append-to-body>
      <el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <el-alert v-if="validation" :title="validation" type="success" :closable="false"/>
      <el-form label-position="top">
        <div class="form-top"><el-form-item label="组合策略名称"><el-input v-model="form.displayName" aria-label="组合策略名称" maxlength="128"/></el-form-item>
          <el-form-item label="K线周期"><el-select v-model="form.period" aria-label="组合策略周期"><el-option v-for="[id,label] in periods" :key="id" :label="label" :value="id"/></el-select></el-form-item>
          <el-form-item label="启用扫描"><el-switch v-model="form.enabled" aria-label="启用组合策略"/></el-form-item></div>
        <el-form-item label="说明"><el-input v-model="form.description" maxlength="2000"/></el-form-item>
        <div class="code-layout">
          <div><h3>Python：关注 / 仓位 / 择时</h3><el-input v-model="form.source" type="textarea" :rows="23" aria-label="组合策略Python代码" spellcheck="false" @input="validation=''"/>
            <p class="muted">attention 返回多/空/取消关注条件和比较窗口；position 返回五项配置指标；timing 返回开/加/减/平仓条件。None表示暂不可用，比例0.5表示50%。</p></div>
          <aside><h3>预定义策略函数</h3><el-input v-model="functionSearch" placeholder="搜索函数" aria-label="搜索预定义函数"/><div class="function-list"><details v-for="item in functionList" :key="item.id+'@'+item.version"><summary>{{item.name}}</summary><code>fn("{{item.id}}", {{item.version}}, {{item.inputs?.map(input=>input.name).join(', ')}})</code><p>{{item.description}}</p></details></div>
            <p class="muted">direction：多头1、空头-1、未关注0。observed_win_rate：已结束观察轮次胜率，无样本为None。margin_rate：真实保证金率，缺失为None。</p></aside>
        </div>
      </el-form>
      <template #footer><el-button :disabled="busy" @click="dialog=false">取消</el-button><el-button :loading="busy" @click="validate">校验Python</el-button><el-button type="primary" :loading="busy" :disabled="!form.displayName.trim() || !form.source.trim()" @click="save">保存组合策略</el-button></template>
    </el-dialog>
  </section>
</template>
<style scoped>
.filters{display:flex;gap:12px;max-width:500px;margin:16px 0}.form-top{display:grid;grid-template-columns:2fr 1fr 1fr;gap:16px}
.code-layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:16px}.code-layout :deep(textarea){font-family:Consolas,monospace;font-size:12px;line-height:1.5;white-space:pre;overflow:auto}.function-list{max-height:330px;overflow:auto;margin-top:12px}.function-list details{padding:8px 0;border-bottom:1px solid var(--ml-divider)}.function-list code{white-space:pre-wrap;word-break:break-word}.muted{font-size:12px;color:var(--ml-text-secondary);line-height:1.7}
</style>
