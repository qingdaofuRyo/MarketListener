<script setup lang="ts">
import {computed, onMounted, ref} from 'vue';
import {apiDelete, apiGet, apiPost, apiPut} from '../../domain/api';
import StrategyRuleTreeEditor from './StrategyRuleTreeEditor.vue';
import type {StrategyFunctionOption} from './StrategyOperandEditor.vue';
import type {StrategyRule} from '../../domain/strategyTypes';
interface SignalDefinition {id:string;displayName:string;description:string;action:'open'|'add'|'reduce'|'close';direction:'long'|'short';period:string;enabled:boolean;version:number;rule:StrategyRule}
const items=ref<SignalDefinition[]>([]), functions=ref<StrategyFunctionOption[]>([]), dialog=ref(false), busy=ref(false), error=ref(''), search=ref(''), action=ref(''), deletedId=ref('');
const actions = [['open','开仓'],['add','加仓'],['reduce','减仓'],['close','平仓']];
const periods = [['5m','5分'],['15m','15分'],['30m','30分'],['1h','60分'],['2h','120分'],['1d','日线'],['1w','周线'],['1mo','月线'],['3mo','季线'],['1y','年线']];
function initial(): SignalDefinition { return {id:'', displayName:'', description:'',action:'open',direction:'long',period:'1d',enabled:true,version:0,rule:{nodeType:'condition',left:{functionId:'condition.crossover',version:1,arguments:[{kind:'series',field:'close'},{kind:'series',field:'close'}]}}}; }
const form=ref<SignalDefinition>(initial());
function keys(value: unknown, camel: boolean): any {
  if(Array.isArray(value)) return value.map((v)=>keys(v,camel));
  if(value && typeof value==='object') return Object.fromEntries(Object.entries(value).map(([key,v])=>[camel?key.replace(/_([a-z])/g,(_,c)=>c.toUpperCase()):key.replace(/[A-Z]/g,c=>'_'+c.toLowerCase()),keys(v,camel)]));
  return value;
}
async function load() { items.value=(await apiGet<{items:SignalDefinition[]}>('/api/signals/definitions',undefined,{force:true})).items.map((item)=>({...item,rule:keys(item.rule,true)})); }
function create() {form.value=initial(); error.value=''; dialog.value=true;}
function edit(item:SignalDefinition) {form.value=JSON.parse(JSON.stringify(item));error.value='';dialog.value=true;}
function payload(item:SignalDefinition) { const {id,...body}=item; return {...body,rule:keys(item.rule,false)}; }
async function save() {
  busy.value=true;error.value='';
  try { if(form.value.id) await apiPut('/api/signals/definitions/'+form.value.id,payload(form.value)); else await apiPost('/api/signals/definitions',payload(form.value)); await load();dialog.value=false; }
  catch(reason) {error.value=reason instanceof Error?reason.message:'保存失败';} finally {busy.value=false;}
}
async function toggle(item:SignalDefinition) {try{await apiPut('/api/signals/definitions/'+item.id,payload({...item,enabled:!item.enabled}));await load();}catch(reason){error.value=String(reason);}}
async function remove(item:SignalDefinition) {if(!window.confirm('删除策略“'+item.displayName+'”？现有监控轮次保留，可以撤销删除。'))return;try{await apiDelete('/api/signals/definitions/'+item.id);deletedId.value=item.id;await load();}catch(reason){error.value=String(reason);}}
async function undo() {await apiPost('/api/signals/definitions/'+deletedId.value+'/restore');deletedId.value='';await load();}
const filtered=computed(()=>items.value.filter(item=>(!action.value||item.action===action.value)&&item.displayName.includes(search.value)));
defineExpose({create});
onMounted(async()=>{try{await load();functions.value=(await apiGet<{items:StrategyFunctionOption[]}>('/api/strategy/functions')).items;}catch(reason){error.value=String(reason);}});
</script>
<template>
  <section class="signal-strategy-manager">
    <p class="muted">策略仅生成观察信号，不连接真实账户。开仓信号建立同向监控，加仓、减仓和平仓仅在该轮监控内生效，平仓优先并结束该轮。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <div class="signal-filters"><el-input v-model="search" aria-label="搜索策略" placeholder="搜索策略" clearable/><el-select v-model="action" aria-label="策略操作筛选" clearable placeholder="全部操作"><el-option v-for="[id,label] in actions" :key="id" :label="label" :value="id"/></el-select><el-button v-if="deletedId" @click="undo">撤销删除</el-button></div>
    <el-table :data="filtered" empty-text="暂无策略，请新建开仓、加仓、减仓或平仓策略">
      <el-table-column prop="displayName" label="策略名" min-width="180"/>
      <el-table-column label="操作"><template #default="{row}">{{actions.find(([id])=>id===row.action)?.[1]}}</template></el-table-column>
      <el-table-column label="方向"><template #default="{row}">{{row.direction==='long'?'多头':'空头'}}</template></el-table-column>
      <el-table-column label="周期"><template #default="{row}">{{periods.find(([id])=>id===row.period)?.[1]}}</template></el-table-column>
      <el-table-column label="状态"><template #default="{row}">{{row.enabled?'已启用':'已停用'}}</template></el-table-column>
      <el-table-column label="操作" width="230"><template #default="{row}"><el-button text @click="edit(row)">编辑</el-button><el-button text @click="toggle(row)">{{row.enabled?'停用':'启用'}}</el-button><el-button text type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>
    <el-dialog v-model="dialog" :title="form.id?'编辑策略':'新建策略'" width="min(1060px, 92vw)" append-to-body>
      <el-alert v-if="error" :title="error" type="error" :closable="false"/>
      <el-form label-position="top"><div class="signal-form-top">
        <el-form-item label="策略名称"><el-input v-model="form.displayName" aria-label="策略名称" maxlength="128"/></el-form-item>
        <el-form-item label="操作类型"><el-select v-model="form.action" aria-label="策略操作"><el-option v-for="[id,label] in actions" :key="id" :label="label" :value="id"/></el-select></el-form-item>
        <el-form-item label="方向"><el-select v-model="form.direction" aria-label="策略方向"><el-option label="多头" value="long"/><el-option label="空头" value="short"/></el-select></el-form-item>
        <el-form-item label="周期"><el-select v-model="form.period" aria-label="策略周期"><el-option v-for="[id,label] in periods" :key="id" :label="label" :value="id"/></el-select></el-form-item>
      </div><el-form-item label="说明"><el-input v-model="form.description" maxlength="2000"/></el-form-item>
      <h3>{{actions.find(([id])=>id===form.action)?.[1]}}信号条件</h3>
      <p class="muted">适用于具备所需行情字段的市场；缺字段或暖机不足不产生信号。首次检查只判断最新已结束 K 线，之后处理新增 K 线。</p>
      <StrategyRuleTreeEditor v-model="form.rule" :functions="functions" :supported-asset-types="[]" root/>
      </el-form>
      <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="!form.displayName.trim()" @click="save">保存策略</el-button></template>
    </el-dialog>
  </section>
</template>
<style scoped>.signal-filters{display:flex;gap:12px;margin:20px 0;max-width:700px}.signal-form-top{display:grid;grid-template-columns:2fr repeat(3,1fr);gap:16px}.muted{color:var(--ml-text-secondary);font-size:13px;line-height:1.8}</style>
