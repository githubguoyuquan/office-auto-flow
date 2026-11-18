  const isTemplateBuilder=item=>item?.kind==='template_builder';
  let builderFields=[],builderFilename='',builderBusy=false,builderSerial=0;
  const builderStyle=document.createElement('style');
  builderStyle.textContent=`
    .template-builder-view .run-head,.template-builder-view .run-actions,.template-builder-view .timeline,.template-builder-view .status-card,.template-builder-view #result,.template-builder-view #loginCard{display:none!important}
    .template-builder-view #templateBuilderPanel{margin-top:0}
    #templateBuilderPanel{margin-top:18px;color:var(--ink)}
    #templateBuilderPanel .builder-card{border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:14px;background:var(--white)}
    #templateBuilderPanel h3{font-size:14px;margin:0 0 10px}
    #templateBuilderPanel p{color:var(--muted);font-size:12px;line-height:1.7}
    #templateBuilderPanel .builder-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    #templateBuilderPanel .builder-source{overflow-wrap:anywhere}
    #builderTableWrap{overflow:auto;max-height:400px;border:1px solid var(--line);border-radius:10px;margin:16px 0}
    #builderTable{border-collapse:collapse;width:100%;min-width:100%}
    #builderTable th{min-width:210px;padding:12px;border-right:1px solid var(--line);background:var(--canvas);vertical-align:top;font-weight:400}
    #builderTable td{height:56px;border-top:1px solid var(--line);border-right:1px solid var(--line)}
    #builderTable input{width:100%;min-width:170px;font-size:12px}
    #builderTable .builder-column-actions{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-top:8px}
    #builderTable .builder-column-actions span{font-size:11px;color:var(--muted)}
    #builderTable button{font-size:11px;padding:5px 8px}
    #builderError{color:var(--bad)!important;white-space:pre-wrap}
    html[data-theme="dark"] #templateBuilderPanel .builder-card{background:var(--canvas)}
    @media(max-width:600px){#templateBuilderPanel .builder-card{padding:15px}}
  `;
  document.head.append(builderStyle);
  const builderPanel=document.createElement('section');builderPanel.id='templateBuilderPanel';builderPanel.hidden=true;
  builderPanel.innerHTML=`<div class="builder-card"><h3>识别候选字段</h3>
    <p>选择 PPTX 或之前导出的 XLSX。识别表头、字段标签并去重，不将业务数据作为列名；图片中的文字暂不支持识别。</p>
    <div class="builder-actions"><button id="builderChoosePptx" type="button" class="ghost">选择 PPTX</button><button id="builderChooseXlsx" type="button" class="ghost">选择 XLSX</button><button id="builderReset" type="button" class="ghost">清空重做</button></div>
    <p id="builderSource" class="builder-source" role="status">也可以直接新增字段，从空白模板开始制作。</p></div>
    <div class="builder-card"><h3>编辑模板列名</h3><p>可修改、删除字段，或左右移动调整列顺序。下载文件只有字段列名和空白数据行。</p>
    <div class="builder-actions"><button id="builderAddField" type="button" class="ghost">＋ 新增字段</button><span id="builderCount" class="panel-sub"></span></div>
    <p id="builderEmpty">尚无字段，请选择文件或新增字段。</p><div id="builderTableWrap" hidden><table id="builderTable" aria-label="空白提取模板预览"><thead><tr id="builderHeaders"></tr></thead><tbody><tr id="builderEmptyRow"></tr></tbody></table></div>
    <p id="builderError" role="alert" hidden></p><div class="builder-actions" style="justify-content:flex-end"><button id="builderDownload" type="button" class="primary" disabled>下载 XLSX 模板</button></div></div>`;
  $('pptxPanel').after(builderPanel);

  function builderProblem(){
    if(!builderFields.length)return '请先添加字段。';
    const names=builderFields.map(field=>field.value.trim());
    if(names.some(name=>!name))return '有字段名称为空，请补全或删除。';
    if(new Set(names).size!==names.length)return '字段名称重复，请修改或删除重复项。';
    return '';
  }
  function showBuilderError(error){$('builderError').textContent=error.message||String(error);$('builderError').hidden=false}
  function renderBuilderControls(){
    const active=taskRunning()||!!currentTask.maintenance||updateBusy||exiting||resettingTask;
    for(const control of builderPanel.querySelectorAll('button,input'))control.disabled=builderBusy||active;
    $('builderDownload').disabled=builderBusy||active||!!builderProblem();
    $('builderCount').textContent=`${builderFields.length} 个字段`;
    $('builderSource').textContent=builderBusy?'正在处理，请稍候…':builderFilename?`来源：${builderFilename}`:'也可以直接新增字段，从空白模板开始制作。';
  }
  function drawBuilder(){
    const header=$('builderHeaders'),body=$('builderEmptyRow');header.replaceChildren();body.replaceChildren();
    $('builderEmpty').hidden=builderFields.length>0;$('builderTableWrap').hidden=!builderFields.length;
    builderFields.forEach((field,index)=>{
      const th=document.createElement('th');th.scope='col';
      const input=document.createElement('input');input.className='field';input.value=field.value;input.maxLength=32767;input.setAttribute('aria-label',`第 ${index+1} 列字段名`);
      input.oninput=()=>{field.value=input.value;$('builderError').hidden=true;renderBuilderControls()};
      const actions=document.createElement('div');actions.className='builder-column-actions';const number=document.createElement('span');number.textContent=`第 ${index+1} 列`;actions.append(number);
      for(const [text,label,offset] of [['←','左移',-1],['→','右移',1],['删除','删除',0]]){
        const button=document.createElement('button');button.type='button';button.className='ghost';button.textContent=text;button.setAttribute('aria-label',`${label}第 ${index+1} 列`);
        button.onclick=()=>{if(offset){const next=index+offset;if(next<0||next>=builderFields.length)return;[builderFields[index],builderFields[next]]=[builderFields[next],builderFields[index]]}else builderFields.splice(index,1);drawBuilder()};actions.append(button);
      }
      th.append(input,actions);header.append(th);body.append(document.createElement('td'));
    });
    renderBuilderControls();
  }
  async function builderAction(action){
    if(builderBusy||taskRunning()||currentTask.maintenance)return;
    builderBusy=true;$('builderError').hidden=true;
    try{renderTask();await action()}catch(error){showBuilderError(error)}finally{builderBusy=false;renderTask()}
  }
  for(const kind of ['pptx','xlsx'])$('builderChoose'+(kind==='pptx'?'Pptx':'Xlsx')).onclick=()=>builderAction(async()=>{
    const result=await api('/api/template-builder/source','POST',{kind});if(result.cancelled)return;
    builderFields=result.fields.map(value=>({value}));builderFilename=result.filename;drawBuilder();
  });
  $('builderReset').onclick=()=>{if(builderBusy||taskRunning())return;builderFields=[];builderFilename='';$('builderError').hidden=true;drawBuilder()};
  $('builderAddField').onclick=()=>{
    if(builderFields.length>=16384){showBuilderError(Error('模板最多 16384 列。'));return}
    let value;do{value=`新字段${++builderSerial}`}while(builderFields.some(field=>field.value.trim()===value));
    builderFields.push({value});drawBuilder();$('builderHeaders').lastElementChild.querySelector('input').focus();
  };
  $('builderDownload').onclick=()=>builderAction(async()=>{
    const problem=builderProblem();if(problem)throw Error(problem);
    const result=await api('/api/template-builder/download','POST',{fields:builderFields.map(field=>field.value.trim())});
    const bytes=Uint8Array.from(atob(result.content),char=>char.charCodeAt(0));
    const url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    const link=document.createElement('a');link.href=url;link.download=result.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('空白模板已生成，请在下载文件中查看');
  });
  const beforeBuilderCanRun=canRun;canRun=item=>!builderBusy&&!isTemplateBuilder(item)&&beforeBuilderCanRun(item);
  const beforeBuilderRenderTask=renderTask;renderTask=()=>{
    beforeBuilderRenderTask();const local=isTemplateBuilder(selected());document.body.classList.toggle('template-builder-view',local);builderPanel.hidden=!local;renderBuilderControls();
    if(local)document.querySelector('.fine-print').textContent='识别结果只作为候选字段，下载前可修改或删除。';
    if(builderBusy){$('runButton').disabled=true;resetTaskButton.disabled=true;exitButton.disabled=true}
  };
  drawBuilder();
