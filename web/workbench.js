
  const businessPanel = document.querySelector('[aria-label="业务操作"]');
  const sidebar = document.querySelector('.sidebar');
  sidebar.insertBefore(businessPanel, document.querySelector('.side-bottom'));
  document.querySelector('.hero').remove();
  document.querySelector('.page-title').textContent = '选择业务操作';
  document.querySelector('.page-desc').textContent = '选择左侧业务，查看任务内容并开始运行。';
  document.querySelector('.crumb').innerHTML = '<b>AIOA</b> <span style="font-size:12px;color:#aaa;margin-left:8px">工作台</span>';
  const menuButton = document.createElement('button');
  menuButton.className = 'sidebar-toggle'; menuButton.textContent = '☰'; menuButton.setAttribute('aria-label','展开或收起业务列表');
  menuButton.addEventListener('click',()=>sidebar.classList.toggle('sidebar-open'));
  document.querySelector('.crumb').prepend(menuButton);
  document.addEventListener('click',event=>{if(!sidebar.contains(event.target)&&!menuButton.contains(event.target))sidebar.classList.remove('sidebar-open')});
  const composerCopy=document.createElement('div');composerCopy.className='composer-copy';composerCopy.id='composerCopy';
  document.querySelector('.run-actions').prepend(composerCopy);
  const TOKEN = '__AIOA_TOKEN__';
  const themeButton=document.createElement('button');themeButton.type='button';themeButton.className='theme-toggle';
  function updateThemeButton(){const dark=document.documentElement.dataset.theme==='dark';themeButton.textContent=dark?'☀ 浅色':'☾ 深色';themeButton.setAttribute('aria-label',dark?'切换到浅色模式':'切换到深色模式')}
  themeButton.addEventListener('click',()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;document.cookie='aioa-theme='+next+'; Path=/; Max-Age=31536000; SameSite=Strict';updateThemeButton()});
  document.querySelector('.top-right').prepend(themeButton);updateThemeButton();
  let exiting=false;const exitButton=document.createElement('button');exitButton.type='button';exitButton.className='theme-toggle';exitButton.id='exitWorkbench';exitButton.textContent='退出工作台';document.querySelector('.top-right').append(exitButton);
  const $ = id => document.getElementById(id);
  let licenseState={valid:false,features:[]},licenseChecking=false,licenseAutoAttempted=false;
  const licenseButton=document.createElement('button');licenseButton.type='button';licenseButton.className='theme-toggle';licenseButton.id='licenseButton';licenseButton.textContent='授权管理';document.querySelector('.top-right').prepend(licenseButton);
  const licenseModal=document.createElement('div');licenseModal.className='modal-backdrop';licenseModal.id='licenseModal';licenseModal.innerHTML=`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="licenseTitle"><div class="modal-top"><h3 id="licenseTitle">软件授权</h3><button class="ghost" id="closeLicense" type="button">关闭</button></div><p id="licenseMessage" role="status"></p><label class="field-label">设备码</label><input id="licenseDevice" class="field" readonly><p id="licenseDetails" class="panel-sub"></p><div class="modal-actions" style="flex-wrap:wrap"><button id="exportRequest" class="ghost" type="button">导出设备申请</button><button id="checkLicense" class="ghost" type="button">联网校验</button><button id="importLicense" class="primary" type="button">导入授权文件</button></div><input id="licenseFile" type="file" accept=".license,application/json" hidden><p class="panel-sub">首次激活、工作台重启后及每隔 24 小时需要联网校时。当前任务可执行完成；到期后无法开始新任务。</p></section>`;document.body.append(licenseModal);
  const licensedModules=document.createElement('p');licensedModules.id='licensedModules';licensedModules.className='panel-sub';$('licenseDetails').after(licensedModules);
  function moduleAllowed(item){return licenseState.valid&&(licenseState.unrestricted||Array.isArray(licenseState.features)&&licenseState.features.includes(item?.module_id))}
  function renderLicense(){licenseButton.textContent=licenseState.valid?'授权有效':'授权管理';$('licenseMessage').textContent=licenseChecking?'正在联网校验时间…':licenseState.message||'尚未授权';$('licenseDevice').value=licenseState.device_id||'';$('licenseDetails').textContent=licenseState.expires_at?'授权到期：'+new Date(licenseState.expires_at).toLocaleString('zh-CN')+(licenseState.valid?' · 剩余 '+Math.ceil(licenseState.remaining_seconds/86400)+' 天':'')+(licenseState.customer?' · '+licenseState.customer:''):'';const features=licenseState.features||[];licensedModules.textContent='许可证模块范围：'+(features.length?features.map(id=>operations.find(item=>item.module_id===id)?.name||id).join('、'):'尚未授权');$('checkLicense').disabled=licenseChecking;$('importLicense').disabled=licenseChecking||runningPhases.has(currentTask.phase)}
  const renderRestrictedLicense=renderLicense;
  renderLicense=()=>{renderRestrictedLicense();if(licenseState.unrestricted){licenseButton.textContent='全部开放';licensedModules.textContent='模块范围：全部业务';$('licenseDevice').closest('section').querySelectorAll('.field-label,#licenseDevice,.modal-actions,.panel-sub:last-child').forEach(el=>el.hidden=true)}};
  licenseButton.onclick=()=>{renderLicense();licenseModal.classList.add('open')};$('closeLicense').onclick=()=>licenseModal.classList.remove('open');
  async function checkLicense(){if(licenseChecking)return;licenseChecking=true;renderLicense();try{const result=await api('/api/license/check','POST',{});licenseState=result.license}catch(error){licenseState={...licenseState,valid:false,message:error.message};toast(error.message)}finally{licenseChecking=false;renderLicense();renderTask()}}
  $('checkLicense').onclick=checkLicense;
  $('exportRequest').onclick=async()=>{try{const data=await api('/api/license/request','POST',{});const url=URL.createObjectURL(new Blob([JSON.stringify(data.request,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='AIOA-设备申请.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000)}catch(error){toast(error.message)}};
  $('importLicense').onclick=()=>$('licenseFile').click();$('licenseFile').onchange=async()=>{const file=$('licenseFile').files[0];if(!file)return;try{if(file.size>65536)throw Error('授权文件过大');licenseChecking=true;renderLicense();const result=await api('/api/license/import','POST',{document:await file.text()});licenseState=result.license;licenseAutoAttempted=false;toast('授权导入成功')}catch(error){toast(error.message)}finally{licenseChecking=false;$('licenseFile').value='';renderLicense();renderTask()}};
  const printFields=document.createElement('div');printFields.id='printFields';printFields.hidden=true;
  printFields.innerHTML=`<div class="print-config"><label class="field-label" for="styleCode">工艺单款号（单个）</label><input class="field" id="styleCode" maxlength="80" placeholder="例如：TTOW27A24B220B"><label class="field-label" for="styleCodesFile" style="margin-top:12px">或上传批量款号 XLSX（第一列每行一个，不含表头）</label><input class="field" id="styleCodesFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
    <div class="print-config"><label class="field-label" for="templateHistory">辅料格式模板（XLSX，可选）</label><div class="picker-row"><select class="field" id="templateHistory"></select><button type="button" class="ghost" id="uploadTemplate">上传模板</button></div><input id="templateFile" type="file" hidden accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
    <div class="print-config"><label class="field-label" for="originalDir">完整工艺单 PDF 保存目录</label><div class="picker-row"><input class="field" id="originalDir" list="originalHistory" placeholder="请选择文件夹或历史目录"><button type="button" class="ghost" id="chooseOriginalDir">选择文件夹</button></div><datalist id="originalHistory"></datalist></div>
    <div class="print-config"><label class="field-label" for="convertedDir">辅料转换 XLSX 保存目录（选择模板时必填）</label><div class="picker-row"><input class="field" id="convertedDir" list="convertedHistory" placeholder="请选择文件夹或历史目录"><button type="button" class="ghost" id="chooseConvertedDir">选择文件夹</button></div><datalist id="convertedHistory"></datalist></div>
    <p class="panel-sub">未选择模板时只保存 PDF；选择模板后将同时生成辅料 XLSX。</p>`;
  document.querySelector('.run-actions').before(printFields);
  let choosingDirectory=false;
  $('uploadTemplate').addEventListener('click',()=>$('templateFile').click());
  for(const [buttonId,fieldId] of [['chooseOriginalDir','originalDir'],['chooseConvertedDir','convertedDir']]){
    $(buttonId).addEventListener('click',async()=>{if(choosingDirectory)return;choosingDirectory=true;$(buttonId).textContent='选择中…';renderTask();try{const data=await api('/api/choose-directory','POST',{initial:$(fieldId).value});if(data.path)$(fieldId).value=data.path}catch(error){toast(error.message)}finally{choosingDirectory=false;$(buttonId).textContent='选择文件夹';renderTask()}});
  }
  function isPrintOperation(item){if(!item)return false;if(item.kind==='process_sheet')return true;try{const u=new URL(item.url);return u.pathname.toLowerCase().endsWith('/plm/stylemanage/newstylemanage.html')}catch{return false}}
  const settingsButton=document.createElement('button');settingsButton.className='theme-toggle';settingsButton.id='applicationSettings';settingsButton.textContent='应用配置';document.querySelector('.top-right').prepend(settingsButton);
  const settingsModal=document.createElement('div');settingsModal.className='modal-backdrop';
  settingsModal.innerHTML=`<div class="modal" role="dialog" aria-modal="true" aria-labelledby="settingsTitle"><div class="modal-top"><h3 id="settingsTitle">应用配置</h3><button type="button" class="icon-btn" id="closeSettings" aria-label="关闭配置">×</button></div><p>配置保存在当前电脑，后续任务自动使用。</p><form id="settingsForm"><label class="field-label" for="systemLoginUrl">登录页面 URL</label><input class="field" id="systemLoginUrl" type="url" placeholder="https://example.com/oa/login"><div class="modal-actions"><button type="submit" class="primary">保存登录设置</button></div></form><section style="margin-top:22px;padding-top:18px;border-top:1px solid var(--line)" aria-labelledby="supplierMappingTitle"><h4 id="supplierMappingTitle" style="margin:0 0 8px">供应商名称映射</h4><p class="panel-sub">可选 XLSX 文件，首个工作表第一列为供应商编号、第二列为供应商名称，可含表头。上传后长期保存，更新后用于下一次任务；未上传或编号未匹配时显示供应商编号。</p><p id="supplierMappingStatus" class="panel-sub" role="status"></p><input id="supplierMappingFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden><div class="modal-actions"><button id="clearSupplierMapping" type="button" class="ghost" disabled>清除映射</button><button id="uploadSupplierMapping" type="button" class="primary">上传映射表</button></div></section></div>`;
  document.body.append(settingsModal);
  let supplierMappingState={configured:false,count:0},supplierMappingBusy=false;
  const supplierMappingError=document.createElement('p');supplierMappingError.id='supplierMappingError';supplierMappingError.className='panel-sub';supplierMappingError.style.color='var(--bad)';supplierMappingError.setAttribute('role','alert');supplierMappingError.hidden=true;$('supplierMappingStatus').after(supplierMappingError);
  function renderSupplierMapping(){const active=runningPhases.has(currentTask.phase);$('uploadSupplierMapping').disabled=supplierMappingBusy||active;$('clearSupplierMapping').disabled=supplierMappingBusy||active||(!supplierMappingState.configured&&!supplierMappingState.error);$('uploadSupplierMapping').textContent=supplierMappingBusy?'处理中…':supplierMappingState.configured?'更新映射表':'上传映射表';$('supplierMappingStatus').textContent=supplierMappingState.error|| (supplierMappingState.configured?`${supplierMappingState.filename} · ${supplierMappingState.count} 条映射 · 更新于 ${new Date(supplierMappingState.uploaded_at).toLocaleString('zh-CN')}`:'尚未上传，导出的供应商字段将显示供应商编号。')}
  settingsButton.onclick=async()=>{try{const data=await api('/api/state');$('systemLoginUrl').value=data.settings.login_url||'';supplierMappingState=data.supplier_mapping||{configured:false,count:0};renderSupplierMapping();settingsModal.classList.add('open');$('systemLoginUrl').focus()}catch(error){toast(error.message)}};
  $('uploadSupplierMapping').onclick=()=>$('supplierMappingFile').click();
  $('supplierMappingFile').onchange=async()=>{const file=$('supplierMappingFile').files[0];if(!file)return;supplierMappingBusy=true;supplierMappingError.hidden=true;renderSupplierMapping();try{if(!file.name.toLowerCase().endsWith('.xlsx')||file.size>5*1024*1024)throw Error('请选择 5 MB 以内的 XLSX 映射表。');const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('映射表读取失败'));reader.readAsDataURL(file)});const data=await api('/api/supplier-mapping','POST',{filename:file.name,content});supplierMappingState=data.supplier_mapping;toast('供应商映射已保存，后续任务自动使用')}catch(error){supplierMappingError.textContent=error.message;supplierMappingError.hidden=false;toast(error.message)}finally{supplierMappingBusy=false;$('supplierMappingFile').value='';renderSupplierMapping()}};
  $('clearSupplierMapping').onclick=async()=>{if(supplierMappingBusy)return;supplierMappingBusy=true;renderSupplierMapping();try{const data=await api('/api/supplier-mapping/clear','POST',{});supplierMappingState=data.supplier_mapping;supplierMappingError.hidden=true;toast('映射已清除，后续导出显示供应商编号')}catch(error){toast(error.message)}finally{supplierMappingBusy=false;renderSupplierMapping()}};
  $('closeSettings').onclick=()=>{settingsModal.classList.remove('open');settingsButton.focus()};
  settingsModal.addEventListener('click',event=>{if(event.target===settingsModal)$('closeSettings').click()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&settingsModal.classList.contains('open'))$('closeSettings').click()});
  $('settingsForm').onsubmit=async event=>{event.preventDefault();try{await api('/api/settings','POST',{login_url:$('systemLoginUrl').value.trim()});$('closeSettings').click();await refresh();toast('系统设置已保存')}catch(error){toast(error.message)}};
  let operations = [], selectedName = '', currentTask = {phase:'idle'}, toastTimer, printSettingsInitialized = false, lastPrintSettingsKey = '';
  function syncPrintSettings(settings){if(!settings)return;const key=JSON.stringify([settings.original_history,settings.converted_history,settings.templates,settings.template_id]);if(printSettingsInitialized&&key===lastPrintSettingsKey)return;lastPrintSettingsKey=key;if(!printSettingsInitialized){$('originalDir').value=settings.original_dir||'';$('convertedDir').value=settings.converted_dir||'';printSettingsInitialized=true}for(const [name,values] of [['originalHistory',settings.original_history],['convertedHistory',settings.converted_history]]){const list=$(name);list.replaceChildren();for(const value of values||[]){const option=document.createElement('option');option.value=value;list.append(option)}}const select=$('templateHistory'),before=select.value,hadOptions=select.options.length>0;select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='不转换辅料（仅保存 PDF）';select.append(empty);for(const item of settings.templates||[]){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+item.uploaded_at;select.append(option)}select.value=hadOptions&&[...select.options].some(o=>o.value===before)?before:(settings.template_id||'')}
  $('templateFile').addEventListener('change',async()=>{const file=$('templateFile').files[0];if(!file)return;if(file.size>5*1024*1024){toast('模板不能超过 5 MB');return}try{const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('模板读取失败'));reader.readAsDataURL(file)});const data=await api('/api/template','POST',{filename:file.name,content});await refresh();$('templateHistory').value=data.template.id;toast('模板已保存，下次默认使用')}catch(error){toast(error.message)}});
  const runningPhases = new Set(['opening','login_required','authenticating','reading','stopping']);
  const labels = {idle:'待开始',opening:'打开页面',login_required:'等待登录',authenticating:'验证中',reading:'读取中',stopping:'停止中',done:'已完成',error:'执行失败',cancelled:'已停止'};
  function toast(message){const node=$('toast');node.textContent=message;node.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('show'),3600)}
  async function api(path,method='GET',body){const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-AIOA-Token':TOKEN},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});let data;try{data=await response.json()}catch{data={error:'无法读取服务器响应。'}}if(!response.ok)throw new Error(data.error||'请求失败。');return data}
  function selected(){return operations.find(item=>item.name===selectedName)}
  function businessDescription(item){if(!item)return '选择左侧业务，查看任务内容并开始运行。';if(item.description)return item.description;return isPrintOperation(item)?'输入款号，查询并打印完整工艺单，再按模板生成辅料 XLSX。':'打开业务页面并读取主要内容。'}
  function canRun(item){if(!moduleAllowed(item)||licenseChecking||exiting||!item||runningPhases.has(currentTask.phase)||choosingDirectory)return false;if(!isPrintOperation(item))return true;const file=$('styleCodesFile').files[0],code=$('styleCode').value.trim(),template=$('templateHistory').value;if(file&&code)return false;const hasCodes=file?file.name.toLowerCase().endsWith('.xlsx'):/^[A-Za-z0-9_-]{1,80}$/.test(code);return hasCodes&&!!$('originalDir').value.trim()&&(!template||!!$('convertedDir').value.trim())}
  function renderTimeline(item){
    const steps=isPrintOperation(item)?[['open','打开业务页'],['query','查询款号'],['select','获取工艺单'],['save_pdf','保存完整 PDF']]:[['open','打开业务页'],['read','读取页面内容']];
    if(isPrintOperation(item)&&$('templateHistory').value){if(item.supplier_lookup_enabled)steps.push(['suppliers','查询辅料供应商']);steps.push(['convert','生成辅料 XLSX'])}
    const progress=item?.name===currentTask.operation?currentTask:{phase:'idle',step:'open',login_encountered:false};
    if(progress.login_encountered)steps.splice(1,0,['login','完成登录']);
    const index=Math.max(0,steps.findIndex(([key])=>key===(progress.step||'open')));
    const timeline=$('taskTimeline');timeline.replaceChildren();
    steps.forEach(([key,label],position)=>{if(position){const line=document.createElement('div');line.className='step-line';timeline.append(line)}const node=document.createElement('div');node.className='step'+(progress.phase==='done'||position<index?' complete':position===index?' active':'');node.dataset.step=key;const num=document.createElement('span');num.className='step-num';num.textContent=String(position+1);const title=document.createElement('span');title.className='step-label';title.textContent=label;node.append(num,title);timeline.append(node)});
  }
  function renderOperations(){
    const list=$('operationList');list.replaceChildren();const query=$('searchInput').value.trim().toLocaleLowerCase();const filtered=operations.filter(item=>(item.name+' '+item.url+' '+(item.login_url||'')).toLocaleLowerCase().includes(query));
    if(!filtered.length){const wrap=document.createElement('div');wrap.className='empty';wrap.innerHTML='<div class="empty-icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 10h8M8 14h6"/></svg></div><h4>'+(operations.length?'没有找到匹配的业务':'还没有业务操作')+'</h4><p>'+(operations.length?'试试其他名称或地址':'暂无可用业务')+'</p>';list.append(wrap)}
    for(const item of filtered){const button=document.createElement('button');button.className='operation'+(item.name===selectedName?' selected':'');button.type='button';button.setAttribute('aria-pressed',item.name===selectedName?'true':'false');const icon=document.createElement('span');icon.className='op-icon';icon.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6.5h16v11H4z"/><path d="M8 10h8M8 13.5h5"/></svg>';const copy=document.createElement('span');copy.className='op-copy';const name=document.createElement('div');name.className='op-name';name.textContent=item.name;const url=document.createElement('div');url.className='op-url';url.textContent=item.url;copy.append(name,url);const arrow=document.createElement('span');arrow.className='op-arrow';arrow.textContent='›';button.append(icon,copy,arrow);button.addEventListener('click',()=>{selectedName=item.name;renderOperations();renderDetail();renderTask()});list.append(button)}
    filtered.forEach((item,index)=>{if(licenseState.valid&&!moduleAllowed(item))list.children[index].querySelector('.op-name').textContent+=' · 未授权'});
    $('countValue').textContent=operations.length+' 项';
  }
  function renderDetail(){const item=selected();const box=$('selectionDetail');box.replaceChildren();if(!item){box.className='detail-none';box.textContent='尚未选择业务操作';return}box.className='';const n=document.createElement('div');n.className='selected-name';n.textContent=item.name;const u=document.createElement('p');u.className='selected-url';u.textContent=item.url;box.append(n,u);if(item.login_url){const login=document.createElement('p');login.className='selected-url';login.textContent='登录页：'+item.login_url;box.append(login)}}
  function renderTask(){
    renderSupplierMapping();
    exitButton.disabled=choosingDirectory||exiting;
    $('printFields').hidden=!isPrintOperation(selected());for(const id of ['styleCode','styleCodesFile','originalDir','convertedDir','templateHistory','templateFile','uploadTemplate'])$(id).disabled=runningPhases.has(currentTask.phase);
    for(const id of ['chooseOriginalDir','chooseConvertedDir'])$(id).disabled=choosingDirectory||runningPhases.has(currentTask.phase);
    $('convertedDir').disabled=runningPhases.has(currentTask.phase)||!$('templateHistory').value;
    $('chooseConvertedDir').disabled=choosingDirectory||runningPhases.has(currentTask.phase)||!$('templateHistory').value;
    const chosen=selected();$('composerCopy').textContent=chosen?chosen.name:'先选择一个业务操作';const hint=document.createElement('small');hint.textContent=businessDescription(chosen);$('composerCopy').append(hint);
    document.querySelector('.page-title').textContent=chosen?chosen.name:'选择业务操作';document.querySelector('.page-desc').textContent=businessDescription(chosen);
    const phase=currentTask.phase||'idle', active=runningPhases.has(phase), badge=$('statusBadge');badge.className='status-badge '+(phase==='done'?'done':phase==='error'?'error':phase==='login_required'?'login_required':active?'running':'');badge.textContent=labels[phase]||phase;$('taskValue').textContent=labels[phase]||'待开始';
    $('statusTitle').textContent=phase==='idle'?'准备就绪':(currentTask.operation||'业务操作')+' · '+(labels[phase]||phase);$('statusMessage').textContent=phase==='idle'?'选择左侧业务操作后，即可开始。':currentTask.message||'';
    if(phase==='idle'&&chosen&&licenseState.valid&&!moduleAllowed(chosen)){$('statusTitle').textContent='业务未授权';$('statusMessage').textContent='当前许可证未包含此业务模块，请联系管理员调整授权。'}
    $('runButton').disabled=!canRun(chosen)||!/^https?:\/\//.test(chosen?.url||'');$('runButton').textContent=phase==='done'||phase==='error'||phase==='cancelled'?'重新运行':'开始运行';$('stopButton').hidden=!active;$('stopButton').disabled=phase==='stopping';$('loginCard').hidden=phase!=='login_required';
    renderTimeline(active?(operations.find(item=>item.name===currentTask.operation)||chosen):chosen);
    const hasResult=(phase==='done'||(['error','cancelled'].includes(phase)&&currentTask.batch_total>1))&&currentTask.content;$('result').hidden=!hasResult;if(hasResult){$('resultHeading').textContent=currentTask.batch_total>1?'批量执行结果':'页面主要内容';$('resultTitle').textContent=currentTask.title||'OA 业务页面';$('resultUrl').textContent=currentTask.url||'';$('resultContent').textContent=currentTask.content;$('resultTime').textContent=currentTask.updated_at||''}
  }
  async function refresh(){try{const data=await api('/api/state');operations=data.operations||[];currentTask=data.task||{phase:'idle'};if(!supplierMappingBusy)supplierMappingState=data.supplier_mapping||{configured:false,count:0};if(!licenseChecking){licenseState=data.license||{valid:false};renderLicense();if(licenseState.valid)licenseAutoAttempted=false;else if(licenseState.code==='time_check_required'&&!licenseAutoAttempted){licenseAutoAttempted=true;checkLicense()}}syncPrintSettings(data.print_settings);if(selectedName&&!selected())selectedName='';if(!selectedName&&operations.length)selectedName=operations[0].name;renderOperations();renderDetail();renderTask();}catch(error){$('statusMessage').textContent='本地服务连接失败：'+error.message}}
  $('searchInput').addEventListener('input',renderOperations);
  $('styleCode').addEventListener('input',()=>{if($('styleCode').value.trim())$('styleCodesFile').value=''});
  $('styleCodesFile').addEventListener('change',()=>{if($('styleCodesFile').files.length)$('styleCode').value=''});
  for(const id of ['styleCode','styleCodesFile','originalDir','convertedDir','templateHistory']){for(const event of ['input','change'])$(id).addEventListener(event,renderTask)}
  $('runButton').addEventListener('click',async()=>{const item=selected();if(!item)return;const style_code=$('styleCode').value.trim().toUpperCase();if(isPrintOperation(item)&&!canRun(item)){toast('请填写单个款号或选择 XLSX 文件，并完成其余运行配置');return}try{if(isPrintOperation(item))await api('/api/print-settings','POST',{original_dir:$('originalDir').value.trim(),converted_dir:$('convertedDir').value.trim(),template_id:$('templateHistory').value});const body={name:item.name,style_code};const file=$('styleCodesFile').files[0];if(isPrintOperation(item)&&file){body.style_codes_file={filename:file.name,content:await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('批量款号文件读取失败'));reader.readAsDataURL(file)})}}await api('/api/run','POST',body);await refresh()}catch(error){toast(error.message)}});
  $('stopButton').addEventListener('click',async()=>{try{await api('/api/stop','POST',{});await refresh()}catch(error){toast(error.message)}});
  $('loginForm').addEventListener('submit',async event=>{event.preventDefault();const username=$('username').value,password=$('password').value;try{await api('/api/credentials','POST',{username,password});$('password').value='';await refresh();toast('登录信息已填入 OA 页面')}catch(error){toast(error.message)}});
  const refreshTimer=setInterval(refresh,1300);
  exitButton.addEventListener('click',async()=>{if(runningPhases.has(currentTask.phase)&&!confirm('当前任务正在运行，退出会停止任务。确定退出工作台吗？'))return;exiting=true;exitButton.disabled=true;exitButton.textContent='正在退出…';try{await api('/api/shutdown','POST',{});clearInterval(refreshTimer);$('statusMessage').textContent='正在停止任务并关闭自动化浏览器，请稍候…';$('runButton').disabled=true;$('stopButton').disabled=true;const timer=setInterval(async()=>{try{await api('/api/state')}catch(_){clearInterval(timer);$('statusMessage').textContent='工作台服务已停止，可以关闭此页面。';exitButton.textContent='工作台已退出'}},1000)}catch(error){exiting=false;exitButton.disabled=false;exitButton.textContent='退出工作台';toast(error.message)}});
  __PPTX_UI__
  let updateStatus={enabled:false,version:'正在读取…',can_rollback:false},updateBusy=false;
  const updateSection=document.createElement('section');
  updateSection.className='software-updates';
  updateSection.setAttribute('aria-labelledby','softwareUpdatesTitle');
  updateSection.innerHTML=`<h4 id="softwareUpdatesTitle">软件更新</h4>
    <p class="panel-sub" id="softwareUpdateVersion"></p>
    <p class="panel-sub">选择发布者提供的离线更新包。完成后自动刷新，登录状态及配置会保留；尚未执行的文件选择需重新填写。</p>
    <p id="softwareUpdateMessage" class="panel-sub" role="status" aria-live="polite"></p>
    <input id="softwareUpdateFile" type="file" accept=".oaf-update,.zip,application/zip" hidden>
    <div class="modal-actions"><button id="rollbackSoftware" type="button" class="ghost">回退上一版本</button>
    <button id="importSoftwareUpdate" type="button" class="primary">导入更新包</button></div>
    <p id="softwareUpdateError" class="panel-sub" role="alert" hidden></p>
    <p class="panel-sub"><a href="/updates/recovery">打开基础恢复界面</a></p>`;
  settingsModal.querySelector('.modal').append(updateSection);
  function renderUpdates(){
    const busy=updateBusy||currentTask.maintenance,active=currentTask.task_active||runningPhases.has(currentTask.phase);
    $('softwareUpdateVersion').textContent='当前版本：'+updateStatus.version;
    $('importSoftwareUpdate').disabled=busy||active||exiting||!updateStatus.enabled;
    $('rollbackSoftware').disabled=busy||active||exiting||!updateStatus.can_rollback;
    $('closeSettings').disabled=updateBusy;
    $('importSoftwareUpdate').textContent=updateBusy?'正在处理…':'导入更新包';
    $('softwareUpdateMessage').textContent=busy?'更新正在处理中，请保持工作台打开。':active?'任务运行期间不能更新，请等待任务结束。':updateStatus.error||(!updateStatus.enabled?'当前版本尚未启用离线更新，请安装支持更新的基础版本。':'更新包会经过签名与完整性校验。');
  }
  async function loadUpdateStatus(){
    try{updateStatus=await api('/api/updates/status');renderUpdates()}
    catch(error){$('softwareUpdateError').textContent=error.message;$('softwareUpdateError').hidden=false}
  }
  settingsButton.addEventListener('click',loadUpdateStatus);
  $('importSoftwareUpdate').onclick=()=>$('softwareUpdateFile').click();
  async function changeSoftware(action){
    if(updateBusy||currentTask.maintenance||currentTask.task_active||runningPhases.has(currentTask.phase))return;
    updateBusy=true;$('softwareUpdateError').hidden=true;renderTask();
    try{updateStatus=await action();$('softwareUpdateMessage').textContent='更新完成，正在刷新工作台…';location.reload()}
    catch(error){$('softwareUpdateError').textContent=error.message;$('softwareUpdateError').hidden=false;updateBusy=false;renderTask()}
    finally{$('softwareUpdateFile').value=''}
  }
  $('softwareUpdateFile').onchange=()=>{
    const file=$('softwareUpdateFile').files[0];if(!file)return;
    if(!file.size||file.size>128*1024*1024){$('softwareUpdateError').textContent='请选择 128 MB 以内的更新包。';$('softwareUpdateError').hidden=false;$('softwareUpdateFile').value='';return}
    changeSoftware(async()=>{
      const response=await fetch('/api/updates/install',{method:'POST',headers:{'X-AIOA-Token':TOKEN,'Content-Type':'application/octet-stream'},body:file,cache:'no-store'});
      let result;try{result=await response.json()}catch{throw Error('无法读取更新结果，请重新打开应用配置查看版本。')}
      if(!response.ok)throw Error(result.error||'更新失败');return result;
    });
  };
  $('rollbackSoftware').onclick=()=>{if(confirm('回退上一版本并刷新工作台？'))changeSoftware(()=>api('/api/updates/rollback','POST',{}))};
  const beforeUpdateCanRun=canRun;
  canRun=item=>!updateBusy&&!currentTask.maintenance&&!currentTask.task_active&&beforeUpdateCanRun(item);
  const beforeUpdateRenderTask=renderTask;
  renderTask=()=>{beforeUpdateRenderTask();renderUpdates();if(updateBusy||currentTask.maintenance){$('runButton').disabled=true;exitButton.disabled=true}};
  refresh();
