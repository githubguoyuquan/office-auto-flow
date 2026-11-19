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
  const recognitionPanel=document.createElement('div');recognitionPanel.className='builder-card';
  recognitionPanel.innerHTML='<h3>复杂表格识别预览</h3><p>核对横纵方向、多级表头和原始位置。每表最多预览 200 行、80 列；每个候选显示前 5 条记录。正式 PPTX 提取会自动整理结构明确的表格；歧义表格需先拆分确认。</p><div class="builder-actions"><button type="button" class="ghost" id="previewTablesXlsx">预览 XLSX</button><button type="button" class="ghost" id="previewTablesPptx">预览 PPTX</button></div><div id="recognitionResults" aria-live="polite"></div>';
  builderPanel.prepend(recognitionPanel);
  function drawRecognition(result){
    const root=$('recognitionResults');root.replaceChildren();
    const description=document.createElement('p');description.textContent=result.filename+' · '+result.notice;root.append(description);
    for(const warning of result.warnings){const p=document.createElement('p');p.textContent=warning;root.append(p)}
    for(const block of result.tables){
      const section=document.createElement('section'),heading=document.createElement('h4');
      heading.textContent=block.location+' · '+({ready:'结构候选明确',review:'需要确认',unrecognized:'未识别结构'}[block.status]);section.append(heading);
      for(const warning of block.warnings){const p=document.createElement('p');p.textContent=warning;section.append(p)}
      for(const candidate of block.candidates){
        const details=document.createElement('details'),summary=document.createElement('summary');
        summary.textContent=(candidate.orientation==='rows'?'按行读取':'按列读取')+' · 表头 '+(candidate.header_start+1)+'–'+(candidate.header_end+1)+' · 样本 '+candidate.record_count_in_sample+' 条';details.append(summary);
        if(candidate.issues.length){const p=document.createElement('p');p.textContent=candidate.issues.join('；');details.append(p)}
        const wrap=document.createElement('div');wrap.style.overflow='auto';const table=document.createElement('table');table.style.cssText='border-collapse:collapse;font-size:12px';
        const head=document.createElement('tr');for(const field of candidate.fields.filter(Boolean)){const th=document.createElement('th');th.textContent=field;th.style.padding='8px';head.append(th)}table.append(head);
        candidate.records.forEach((record,index)=>{const row=document.createElement('tr');record.forEach((value,column)=>{const cell=document.createElement('td');cell.textContent=value;cell.style.cssText='padding:8px;border:1px solid var(--line);white-space:pre-wrap';const source=candidate.sources[index][column];cell.title='源位置：第 '+(source[0]+1)+' 行，第 '+(source[1]+1)+' 列';row.append(cell)});table.append(row)});
        wrap.append(table);details.append(wrap);section.append(details);
      }
      if(block.candidate_count>block.candidates.length){const p=document.createElement('p');p.textContent='候选较多，仅展示前 10 种，请先拆分表区。';section.append(p)}
      root.append(section);
    }
  }
  for(const kind of ['xlsx','pptx'])$('previewTables'+(kind==='xlsx'?'Xlsx':'Pptx')).onclick=()=>builderAction(async()=>{
    const result=await api('/api/table-recognition/preview','POST',{kind});if(!result.cancelled)drawRecognition(result);
  });

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
    $('builderCount').textContent=`${builderFields.length} 个字段`+(builderFields.length?' · '+(builderProblem()||'已就绪，可下载'):' · 先选择文件或新增字段');
    $('builderDownload').title=builderProblem()||'下载空白 XLSX 模板';
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
  for(const kind of ['pptx','xlsx']){
    const button=document.createElement('button');button.type='button';button.className='ghost';button.id='builderNetwork'+kind;button.textContent='网络 '+kind.toUpperCase();
    $('builderChoose'+(kind==='pptx'?'Pptx':'Xlsx')).after(button);
    button.onclick=()=>builderAction(async()=>{const paths=await networkPaths(kind);if(!paths)return;const result=await api('/api/template-builder/source','POST',{kind,paths});builderFields=result.fields.map(value=>({value}));builderFilename=result.filename;drawBuilder()});
  }
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
    if(builderBusy){resetTaskButton.disabled=true;exitButton.disabled=true}
  };
  drawBuilder();
