"""PPTX workbench controls, embedded into the existing local page."""

PPTX_UI = r'''
  const isPptx=item=>item?.kind==='pptx_extract';
  const pptxStyle=document.createElement('style');pptxStyle.textContent=`
    #pptxPanel{--pptx-accent:#28785e;margin:16px 0 0;color:var(--ink)}
    html[data-theme="dark"] #pptxPanel{--pptx-accent:#94cfb8}
    .pptx-view .run-head .panel-sub{display:none}.pptx-view .timeline{margin-bottom:14px;gap:16px}
    .pptx-view.pptx-idle .status-card{display:none}
    .pptx-view .status-card{margin-bottom:0}.pptx-view .run-actions{margin-top:18px!important}
    #pptxPanel .pptx-card{border:1px solid var(--line);border-radius:16px;padding:18px;min-width:0;background:color-mix(in srgb,var(--canvas) 96%,var(--ink))}
    #pptxPanel .pptx-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
    #pptxPanel h4{margin:0;font-size:13px;font-weight:600}
    #pptxPanel .pptx-caption{font-size:11px;line-height:1.6;color:var(--muted);margin:5px 0 0}
    #pptxPanel .pptx-pill{font-size:10px;white-space:nowrap;color:var(--pptx-accent);border:1px solid color-mix(in srgb,var(--pptx-accent) 30%,transparent);border-radius:999px;padding:4px 8px}
    #pptxPanel .pptx-drop{border:1px dashed var(--line);border-radius:12px;padding:20px;display:flex;align-items:center;gap:16px;background:var(--canvas)}
    #pptxPanel .pptx-file-icon{width:42px;height:48px;border:1px solid var(--line);border-radius:10px;display:grid;place-items:center;font-size:10px;font-weight:700;letter-spacing:.04em;flex-shrink:0;color:var(--pptx-accent)}
    #pptxPanel .pptx-drop-copy{flex:1;min-width:0;font-size:13px;font-weight:500}
    #pptxPanel .pptx-drop button{flex-shrink:0}
    #pptxPanel .pptx-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}
    #pptxPanel .picker-row{display:flex;align-items:center;gap:8px}
    #pptxPanel .picker-row .field{flex:1;min-width:0;width:100%;text-overflow:ellipsis;text-align:left}
    #pptxPanel .picker-row button{flex-shrink:0;white-space:nowrap}
    #pptxPanel .pptx-path-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:18px;margin:0 0 10px}
    #pptxPanel .field{font-size:11px;height:38px;padding:8px 10px}
    #pptxPanel .ghost{font-size:11px;padding:8px 12px}
    #pptxPanel .pptx-link{border:0;padding:0;background:transparent;color:var(--muted);font-size:11px;cursor:pointer}
    #pptxPanel .pptx-link:hover{color:var(--ink)}
    #pptxPanel .pptx-card-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:10px}
    #pptxInputs{max-height:150px;overflow:auto;margin-top:10px;display:grid;gap:4px}
    #pptxInputs:empty{display:none}
    #pptxInputs .pptx-file{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;background:var(--canvas);font-size:12px}
    #pptxInputs .pptx-file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
    #pptxInputs .pptx-file button{border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:2px 5px}
    #pptxMappingDetails{border:1px solid var(--line);border-radius:14px;padding:15px 18px}
    #pptxMappingDetails summary{cursor:pointer;font-size:12px;font-weight:500;list-style:none;display:flex;gap:10px;align-items:center}
    #pptxMappingDetails summary:before{content:'›';font-size:18px;color:var(--muted);transition:transform .15s}
    #pptxMappingDetails[open] summary:before{transform:rotate(90deg)}
    #pptxMappingSummary{margin-left:auto;color:var(--muted);font-size:11px;font-weight:400}
    #pptxMapping{display:grid;gap:0;max-height:280px;overflow:auto;margin-top:12px}
    #pptxMapping .pptx-map-row{display:grid;grid-template-columns:minmax(100px,1fr) minmax(140px,1fr);gap:12px;align-items:center;padding:8px 0;border-top:1px solid var(--line);font-size:12px}
    #pptxPanel .pptx-advanced-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
    #pptxPanel .pptx-note{display:flex;gap:6px;color:var(--muted);font-size:10px;line-height:1.6;margin:12px 2px 0}
    #pptxError{color:var(--bad);border:1px solid color-mix(in srgb,var(--bad) 30%,transparent);border-radius:10px;padding:12px;font-size:12px;line-height:1.6}
    #pptxRulesDialog{width:min(660px,calc(100vw - 32px));max-height:85dvh;overflow:auto}
    #pptxRuleRows .pptx-rule-row{display:grid;grid-template-columns:minmax(0,1fr) 18px minmax(0,1fr) auto;gap:8px;align-items:center}
    #pptxRuleRows .field{min-width:0;width:100%}#pptxRuleRows .pptx-rule-arrow{color:var(--muted);text-align:center}
    @media(max-width:900px){#pptxPanel .pptx-grid{grid-template-columns:1fr}}
    @media(max-width:600px){#pptxPanel .pptx-card{padding:14px}#pptxPanel .pptx-drop{padding:14px;flex-wrap:wrap;gap:12px}#pptxPanel .pptx-drop button{width:100%}#pptxMappingSummary{font-size:10px}#pptxRuleRows .pptx-rule-row{grid-template-columns:minmax(0,1fr) 16px minmax(0,1fr)}#pptxRuleRows .pptx-rule-row button{grid-column:3;justify-self:end}}
  `;document.head.append(pptxStyle);
  runningPhases.add('running');labels.running='提取中';
  pptxStyle.textContent+='#pptxPanel:not([hidden]) + .run-actions{position:static;margin-top:22px}@media(max-width:600px){.topbar{height:auto;min-height:60px;flex-wrap:wrap;gap:8px;padding:10px 14px}.crumb{white-space:nowrap;flex-shrink:0}.top-right{flex-wrap:wrap;gap:6px}}';
  let pptx={inputs:[],template:'',output_dir:'',rules:[],mapping:[]},pptxAnalysis=null,pptxBusy=false,pptxReady=false,pptxFilesKey='';
  const pptxPanel=document.createElement('div');pptxPanel.id='pptxPanel';pptxPanel.hidden=true;
  pptxPanel.innerHTML=`<section class="pptx-card" aria-labelledby="pptxSourceTitle">
    <div class="pptx-head"><h4 id="pptxSourceTitle">工艺单文件</h4><span class="pptx-pill" id="pptxFileCount">支持批量提取</span></div>
    <div class="pptx-drop"><span class="pptx-file-icon" aria-hidden="true">PPTX</span><div class="pptx-drop-copy"><span id="pptxSourceHint">选择需要提取的工艺单</span><p class="pptx-caption">可一次选择多个文件，统一汇总为一份 Excel</p></div><button id="pptxChooseInputs" type="button" class="ghost">选择文件</button></div>
    <div id="pptxInputs" aria-label="已选择的 PPTX 文件"></div>
    <div class="pptx-card-foot" id="pptxFileFoot" hidden><span class="pptx-caption">仅在本机读取，不上传文件</span><button id="pptxClearInputs" type="button" class="pptx-link">清空列表</button></div>
    </section>
    <div class="pptx-grid">
      <section class="pptx-card" aria-labelledby="pptxTemplateTitle"><div class="pptx-head"><h4 id="pptxTemplateTitle">输出模板</h4><span class="pptx-pill">XLSX</span></div><p class="pptx-path-name" id="pptxTemplateName">尚未选择模板</p><div class="picker-row"><input id="pptxTemplate" class="field" aria-label="模板完整路径" readonly placeholder="选择一个 Excel 模板"><button id="pptxChooseTemplate" type="button" class="ghost">选择模板</button></div><p class="pptx-caption">沿用模板列顺序与样式，不新增列</p></section>
      <section class="pptx-card" aria-labelledby="pptxOutputTitle"><div class="pptx-head"><h4 id="pptxOutputTitle">保存位置</h4><button id="pptxDefaultOutput" type="button" class="pptx-link">恢复默认</button></div><p class="pptx-path-name" id="pptxOutputName">首个 PPTX 所在文件夹</p><div class="picker-row"><input id="pptxOutput" class="field" aria-label="保存目录完整路径" readonly placeholder="默认使用首个 PPTX 所在目录"><button id="pptxChooseOutput" type="button" class="ghost">选择目录</button></div><p class="pptx-caption">生成“面辅料汇总.xlsx”，重名自动编号</p></section>
    </div>
    <details id="pptxMappingDetails"><summary>字段与提取设置<span id="pptxMappingSummary">选择模板后自动匹配</span></summary><p class="pptx-caption" style="margin-top:12px">左侧为模板列，右侧为提取字段。未匹配时尝试同名字段，没有数据则留空。</p><div id="pptxMapping"></div><div class="pptx-advanced-actions"><button id="pptxAnalyze" type="button" class="ghost">重新匹配</button><button id="pptxRules" type="button" class="ghost">字段别名</button><button id="pptxSave" type="button" class="ghost">保存设置</button></div></details>
    <p id="pptxError" role="alert" hidden></p>
    <p class="pptx-note">◈ 无需登录 OA · 模板与设置自动记忆 · 下次只需重新选择工艺单</p>`;
  $('printFields').after(pptxPanel);
  const rulesModal=document.createElement('div');rulesModal.className='modal-backdrop';
  rulesModal.innerHTML=`<section class="modal" id="pptxRulesDialog" role="dialog" aria-modal="true" aria-labelledby="pptxRulesTitle"><div class="modal-top"><h3 id="pptxRulesTitle">字段别名</h3><button id="pptxCloseRules" type="button" class="ghost">关闭</button></div><p class="panel-sub">把同一字段的不同写法统一起来。例如“用量” → “数量”，多种写法可以指向同一字段。</p><div id="pptxRuleRows" style="display:grid;gap:10px;max-height:320px;overflow:auto;margin-top:20px"></div><div class="modal-actions"><button id="pptxAddRule" type="button" class="ghost">＋ 新增别名</button><button id="pptxApplyRules" type="button" class="primary">应用并保存</button></div><p id="pptxRulesError" role="alert" style="color:var(--bad)"></p></section>`;
  document.body.append(rulesModal);
  const aliasSettingsButton=document.createElement('button');aliasSettingsButton.type='button';aliasSettingsButton.className='ghost';aliasSettingsButton.textContent='PPTX 字段别名';aliasSettingsButton.style.marginTop='20px';settingsModal.querySelector('.modal').append(aliasSettingsButton);
  function pptxError(error){$('pptxError').textContent=error?.message||String(error);$('pptxError').hidden=false}
  function fieldSelect(value,onchange,blank=false){const select=document.createElement('select');select.className='field';if(blank){const o=document.createElement('option');o.value=-1;o.textContent='同名字段 / 留空';select.append(o)}for(const [index,name] of (pptxAnalysis?.fields||[]).entries()){const o=document.createElement('option');o.value=index;o.textContent=name;select.append(o)}select.value=String(value);select.onchange=()=>onchange(Number(select.value));return select}
  function drawMapping(){const box=$('pptxMapping');box.replaceChildren();for(const [index,header] of (pptxAnalysis?.headers||[]).entries()){const row=document.createElement('label');row.className='pptx-map-row';const title=document.createElement('span');title.textContent=header||`第 ${index+1} 列`;row.append(title,fieldSelect(pptx.mapping[index]??-1,v=>{pptx.mapping[index]=v},true));box.append(row)}}
  function drawRules(){const box=$('pptxRuleRows');box.replaceChildren();if(!pptx.rules.length){const empty=document.createElement('p');empty.className='panel-sub';empty.textContent='尚未添加别名，当前使用内置字段识别。';box.append(empty)}pptx.rules.forEach((rule,index)=>{const row=document.createElement('div');row.className='pptx-rule-row';const input=document.createElement('input');input.className='field';input.placeholder='原字段名，如：用量';input.setAttribute('aria-label',`第 ${index+1} 条原字段名`);input.maxLength=100;input.value=rule.source;input.oninput=()=>{rule.source=input.value};const arrow=document.createElement('span');arrow.className='pptx-rule-arrow';arrow.textContent='→';arrow.setAttribute('aria-hidden','true');const select=fieldSelect(rule.target,v=>{rule.target=v});select.setAttribute('aria-label',`第 ${index+1} 条目标字段`);const remove=document.createElement('button');remove.type='button';remove.className='ghost';remove.textContent='删除';remove.onclick=()=>{pptx.rules.splice(index,1);drawRules()};row.append(input,arrow,select,remove);box.append(row)})}
  async function pptxAnalyze(preserve=false){const result=await api('/api/pptx/template','POST',{template:pptx.template,rules:pptx.rules});pptxAnalysis=result;if(!preserve||pptx.mapping.length!==result.headers.length)pptx.mapping=result.mapping;drawMapping()}
  async function pptxSave(){const value=await api('/api/pptx/settings','POST',pptx);pptxAnalysis=value.analysis;drawMapping();toast('提取配置已保存')}
  async function pptxAction(action){if(pptxBusy)return;pptxBusy=true;$('pptxError').hidden=true;renderTask();try{await action()}catch(error){pptxError(error)}finally{pptxBusy=false;renderTask()}}
  $('pptxChooseInputs').onclick=()=>pptxAction(async()=>{const data=await api('/api/pptx/files','POST',{kind:'pptx'});if(data.paths.length)pptx.inputs=data.paths});
  $('pptxClearInputs').onclick=()=>{pptx.inputs=[];renderTask()};
  $('pptxChooseTemplate').onclick=()=>pptxAction(async()=>{const data=await api('/api/pptx/files','POST',{kind:'xlsx'});if(data.paths.length){pptx.template=data.paths[0];pptxAnalysis=null;pptx.mapping=[];drawMapping();await pptxAnalyze();await pptxSave()}});
  $('pptxChooseOutput').onclick=()=>pptxAction(async()=>{const data=await api('/api/choose-directory','POST',{initial:pptx.output_dir});if(data.path)pptx.output_dir=data.path});
  $('pptxDefaultOutput').onclick=()=>{pptx.output_dir='';renderTask()};
  $('pptxAnalyze').onclick=()=>pptxAction(()=>pptxAnalyze());
  $('pptxSave').onclick=()=>pptxAction(pptxSave);
  const openRules=()=>{if(!pptxAnalysis){toast('请先选择并分析 XLSX 模板');return}drawRules();rulesModal.classList.add('open')};
  $('pptxRules').onclick=openRules;aliasSettingsButton.onclick=openRules;
  $('pptxCloseRules').onclick=()=>rulesModal.classList.remove('open');
  $('pptxAddRule').onclick=()=>{if(pptx.rules.length>=500)return;pptx.rules.push({source:'',target:2});drawRules()};
  $('pptxApplyRules').onclick=async()=>{if(pptxBusy)return;pptxBusy=true;renderTask();$('pptxRulesError').textContent='';$('pptxApplyRules').disabled=true;try{await pptxAnalyze();await pptxSave();rulesModal.classList.remove('open')}catch(error){$('pptxRulesError').textContent=error.message;pptxAnalysis=null;drawMapping()}finally{pptxBusy=false;$('pptxApplyRules').disabled=false;renderTask()}};
  const baseCanRun=canRun;canRun=item=>isPptx(item)?pptxReady&&!pptxBusy&&!choosingDirectory&&!exiting&&!licenseChecking&&moduleAllowed(item)&&!runningPhases.has(currentTask.phase)&&pptx.inputs.length>0&&!!pptx.template&&!!pptxAnalysis:baseCanRun(item);
  const pptxBaseName=value=>String(value).split(/[\\/]/).filter(Boolean).pop()||value;
  const baseRenderTask=renderTask;renderTask=()=>{
    baseRenderTask();const local=isPptx(selected()),active=runningPhases.has(currentTask.phase);
    document.body.classList.toggle('pptx-view',local);document.body.classList.toggle('pptx-idle',local&&currentTask.phase==='idle');pptxPanel.hidden=!local;
    for(const [id,value] of [['pptxTemplate',pptx.template],['pptxOutput',pptx.output_dir]]){$(id).value=value;$(id).title=value}
    $('pptxTemplateName').textContent=pptx.template?pptxBaseName(pptx.template):'尚未选择模板';
    $('pptxOutputName').textContent=pptx.output_dir?pptxBaseName(pptx.output_dir):'首个 PPTX 所在文件夹';
    $('pptxFileCount').textContent=pptx.inputs.length?`已选 ${pptx.inputs.length} 个文件`:'支持批量提取';
    $('pptxSourceHint').textContent=pptx.inputs.length?'工艺单已准备好':'选择需要提取的工艺单';
    $('pptxChooseInputs').textContent=pptx.inputs.length?'重新选择':'选择文件';$('pptxFileFoot').hidden=!pptx.inputs.length;
    const key=JSON.stringify(pptx.inputs);
    if(key!==pptxFilesKey){pptxFilesKey=key;$('pptxInputs').replaceChildren();pptx.inputs.forEach((path,index)=>{const row=document.createElement('div');row.className='pptx-file';const name=document.createElement('span');name.textContent=pptxBaseName(path);name.title=path;const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除 '+pptxBaseName(path));remove.onclick=()=>{pptx.inputs.splice(index,1);renderTask()};row.append(name,remove);$('pptxInputs').append(row)})}
    $('pptxMappingSummary').textContent=pptxAnalysis?`${pptxAnalysis.headers.length} 列 · ${pptx.mapping.filter(v=>v>=0).length} 列已匹配 · ${pptx.rules.length} 条别名`:'选择模板后自动匹配';
    for(const el of pptxPanel.querySelectorAll('button,select'))el.disabled=active||pptxBusy||!pptxReady;
    for(const id of ['pptxAnalyze','pptxRules','pptxSave'])$(id).disabled=active||pptxBusy||!pptxReady||!pptx.template;
    for(const el of rulesModal.querySelectorAll('button,input,select'))el.disabled=active||pptxBusy;
    if(local){$('runButton').disabled=!canRun(selected());$('runButton').textContent=active?'正在提取…':'开始提取';$('composerCopy').firstChild.textContent=pptx.inputs.length?`${pptx.inputs.length} 个文件 → 1 份汇总表`:'准备提取工艺单';document.querySelector('.fine-print').textContent='文件保留在本机，提取过程无需 OA 登录。'}else{document.querySelector('.fine-print').textContent='密码不会保存；登录状态保存在本机浏览器配置中，OA 会话失效后需重新登录。'}
    exitButton.disabled=choosingDirectory||pptxBusy||exiting;
    if(currentTask.operation==='PPTX 面辅料提取'&&currentTask.content){$('resultHeading').textContent='提取结果';$('resultTitle').textContent='面辅料汇总 XLSX'}
  };
  const baseTimeline=renderTimeline;renderTimeline=item=>{if(!isPptx(item)){baseTimeline(item);return}const timeline=$('taskTimeline');timeline.replaceChildren();const phase=currentTask.operation===item.name?currentTask.phase:'idle';for(const [index,label] of ['提取面辅料','汇总并保存 XLSX'].entries()){const step=document.createElement('div');step.className='step'+(phase==='done'?' complete':phase==='running'&&index===0?' active':'');const number=document.createElement('span');number.className='step-num';number.textContent=index+1;const text=document.createElement('span');text.className='step-label';text.textContent=label;step.append(number,text);timeline.append(step)}};
  $('runButton').addEventListener('click',event=>{if(!isPptx(selected()))return;event.stopImmediatePropagation();if(!canRun(selected()))return;pptxAction(async()=>{await pptxSave();await api('/api/pptx/run','POST',pptx);await refresh()})},true);
  (async()=>{try{const saved=await api('/api/pptx/settings');pptx={...pptx,...saved,inputs:[]};if(pptx.template)await pptxAnalyze(true)}catch(error){pptxError(error)}finally{pptxReady=true;renderTask()}})();
'''
