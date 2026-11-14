const $ = id => document.getElementById(id);
const state = {sessionId:null,stream:null,timer:null,snapshots:[],selected:null,region:null,selecting:false,pinned:false,busy:false,messages:[]};

function status(message, error=false){ $('captureStatus').textContent=message; $('captureStatus').style.color=error?'#a34534':''; if(error||message.startsWith('Requesting'))$('stageBadge').textContent=message; }
async function api(path, options={}){
  const response=await fetch(path,options);
  let data; try{data=await response.json()}catch{data={detail:'Unexpected server response'}}
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Request failed');
  return data;
}
function elapsed(ms){const s=Math.floor(ms/1000);return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`}
function showMedia(){
  const live=!!state.stream && !state.region && !state.selecting && state.selected?.snapshot_id===state.snapshots[0]?.snapshot_id;
  $('video').classList.toggle('active',live);
  $('snapshot').classList.toggle('active',!!state.selected);
  $('stageEmpty').hidden=!!state.stream||!!state.selected;
  $('stageBadge').textContent=live?'● LIVE CAMERA':state.selected?`SNAPSHOT · ${elapsed(state.selected.captured_at_ms)}`:state.stream?'● LIVE CAMERA':'Waiting for camera';
  $('selectRegion').disabled=!state.snapshots.length;
  drawRegion();
}
function drawRegion(){
  const el=$('selection'); if(!state.region||!state.selected){el.hidden=true;return}
  const image=$('snapshot'), stage=$('stage');
  const ratio=state.selected.width_px/state.selected.height_px, view=stage.clientWidth/stage.clientHeight;
  let width,height,left,top;
  if(view>ratio){height=stage.clientHeight;width=height*ratio;left=(stage.clientWidth-width)/2;top=0}
  else{width=stage.clientWidth;height=width/ratio;left=0;top=(stage.clientHeight-height)/2}
  Object.assign(el.style,{left:`${left+state.region.x*width}px`,top:`${top+state.region.y*height}px`,width:`${state.region.width*width}px`,height:`${state.region.height*height}px`});
  el.hidden=false;
}
function renderMoments(){
  const target=$('moments');target.replaceChildren();$('momentCount').textContent=`${state.snapshots.length} recent snapshot${state.snapshots.length===1?'':'s'}`;
  if(!state.snapshots.length){const p=document.createElement('div');p.className='moment-placeholder';p.textContent='Snapshots will appear here every 4 seconds.';target.append(p);return}
  for(const shot of state.snapshots){const button=document.createElement('button');button.className='moment'+(state.selected?.snapshot_id===shot.snapshot_id?' active':'');button.title=`Snapshot at ${elapsed(shot.captured_at_ms)}`;
    const img=document.createElement('img');img.src=shot.image_url;img.alt=button.title;const label=document.createElement('span');label.textContent=elapsed(shot.captured_at_ms);button.append(img,label);
    button.onclick=()=>selectSnapshot(shot,shot.snapshot_id!==state.snapshots[0].snapshot_id);target.append(button)}
}
function selectSnapshot(shot,pinned=false){state.selected=shot;state.pinned=pinned;state.region=null;state.selecting=false;$('selectRegion').textContent='Select a region';$('stage').classList.remove('selecting');$('snapshot').src=shot.image_url;$('clearRegion').hidden=true;showMedia();renderMoments()}
async function refresh(){
  if(!state.sessionId)return;
  try{const data=await api(`/api/sessions/${state.sessionId}`);state.snapshots=data.snapshots;
    if(!state.selected&&state.snapshots.length)selectSnapshot(state.snapshots[0]);
    else if(state.selected&&state.stream&&!state.pinned&&!state.region&&!state.selecting&&state.selected.snapshot_id!==state.snapshots[0]?.snapshot_id)selectSnapshot(state.snapshots[0]);
    renderMoments();
    if(data.messages.length!==state.messages.length){state.messages=data.messages;renderMessages()}
  }catch(e){status(e.message,true)}
}
async function capture(){
  if(!state.stream||!$('video').videoWidth)return;
  const canvas=$('captureCanvas'),video=$('video');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.82));
  if(!blob)return;const form=new FormData();form.append('image',blob,'snapshot.jpg');form.append('source','camera');
  try{const shot=await api(`/api/sessions/${state.sessionId}/snapshots`,{method:'POST',body:form});
    state.snapshots.unshift(shot);state.snapshots=state.snapshots.slice(0,12);
    if(!state.pinned&&!state.region&&!state.selecting)selectSnapshot(shot);else renderMoments();
    status(`Capturing · latest at ${elapsed(shot.captured_at_ms)}`)
  }catch(e){status(e.message,true)}
}
function stop(){clearInterval(state.timer);state.timer=null;state.stream?.getTracks().forEach(t=>t.stop());state.stream=null;$('startCamera').disabled=false;$('stopCamera').disabled=true;status('Camera off');showMedia()}
async function start(){
  if(!window.isSecureContext){status(`Camera blocked: ${location.origin} is not a secure context. Open localhost on this device or use HTTPS.`,true);return}
  if(!navigator.mediaDevices?.getUserMedia){status(`Camera API unavailable at ${location.origin}. Check Chrome camera permissions and whether this page is inside another app.`,true);return}
  $('startCamera').disabled=true;status('Requesting camera permission…');
  try{const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:'environment',width:{ideal:1280},height:{ideal:720}}});
    state.stream=stream;const video=$('video');video.srcObject=stream;await video.play();
    stream.getVideoTracks()[0].onended=stop;$('startCamera').disabled=true;$('stopCamera').disabled=false;
    status('Capturing every 4 seconds');showMedia();await capture();state.timer=setInterval(capture,4000)
  }catch(e){
    state.stream?.getTracks().forEach(track=>track.stop());state.stream=null;$('startCamera').disabled=false;
    const detail=e.name==='NotAllowedError'?'Camera permission was blocked. Allow camera access in your browser, then try again.':
      e.name==='NotFoundError'?'No camera found on this device.':
      e.name==='NotReadableError'?'Camera is busy in another app. Close it and try again.':
      `Camera unavailable: ${e.message}`;
    status(detail,true);showMedia();$('stageBadge').textContent=detail;
  }
}
function imagePoint(event){
  const box=$('stage').getBoundingClientRect(), shot=state.selected;
  const ratio=shot.width_px/shot.height_px,view=box.width/box.height;
  let w,h,l,t;if(view>ratio){h=box.height;w=h*ratio;l=(box.width-w)/2;t=0}else{w=box.width;h=w/ratio;l=0;t=(box.height-h)/2}
  return {x:Math.max(0,Math.min(1,(event.clientX-box.left-l)/w)),y:Math.max(0,Math.min(1,(event.clientY-box.top-t)/h))}
}
let dragStart=null;
function renderMessages(){const root=$('conversation');root.replaceChildren();if(!state.messages.length){root.innerHTML='<div class="welcome"><span class="welcome-icon">✳</span><strong>Clarity, one question at a time.</strong><p>Your answers will appear here with the snapshot they used.</p></div>';return}
  for(const message of state.messages){const entry=document.createElement('article');entry.className='entry';const q=document.createElement('div');q.className='entry-question';q.textContent=message.question;
    const a=document.createElement('div');a.className='entry-answer';a.textContent=message.result.answer;
    const meta=document.createElement('div');meta.className='entry-meta';const shot=state.snapshots.find(s=>s.snapshot_id===message.snapshot_id);const thumb=document.createElement('img');thumb.src=`/api/snapshots/${message.snapshot_id}/image`;thumb.alt='Evidence snapshot';const label=document.createElement('span');label.textContent=shot?`Snapshot ${elapsed(shot.captured_at_ms)}${message.region?' · Region R1':''}`:`Evidence snapshot${message.region?' · Region R1':''}`;
    meta.append(thumb,label);meta.onclick=()=>{if(shot){selectSnapshot(shot);state.region=message.region;drawRegion()}};meta.style.cursor='pointer';entry.append(q,a,meta);
    for(const [title,list] of [['Visible',message.result.visible_evidence],['Explanation',message.result.inferred_explanation],['Uncertain',message.result.uncertainties]]){if(Array.isArray(list)&&list.length){const line=document.createElement('div');line.className='evidence';const strong=document.createElement('strong');strong.textContent=title+' · ';line.append(strong,document.createTextNode(list.join(' · ')));entry.append(line)}}root.append(entry)}root.scrollTop=root.scrollHeight;
}
async function ask(question){if(state.busy)return;if(!state.snapshots.length){status('Start camera capture first',true);return}
  state.busy=true;$('askButton').disabled=true;$('askButton').firstChild.textContent='Thinking… ';const shot=state.selected||state.snapshots[0];
  try{const message=await api(`/api/sessions/${state.sessionId}/ask`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,language:$('language').value,snapshot_id:shot.snapshot_id,region:state.region})});state.messages.push(message);renderMessages();$('question').value=''}
  catch(e){const error=document.createElement('p');error.className='error';error.textContent=e.message;$('conversation').append(error);$('conversation').scrollTop=$('conversation').scrollHeight}
  finally{state.busy=false;$('askButton').disabled=false;$('askButton').firstChild.textContent='Ask about this moment '}
}
async function init(){const params=new URLSearchParams(location.search);state.sessionId=params.get('session');
  try{if(state.sessionId)await refresh();else{const result=await api('/api/sessions',{method:'POST'});state.sessionId=result.session_id;history.replaceState({},'',`?session=${state.sessionId}`)}$('sessionStatus').textContent='Session ready'}
  catch(e){$('sessionStatus').textContent=e.message;return}
  $('startCamera').onclick=start;$('stopCamera').onclick=stop;
  $('copyLink').onclick=async()=>{await navigator.clipboard.writeText(location.href);$('copyLink').textContent='Copied ✓';setTimeout(()=>$('copyLink').textContent='Copy link ↗',1800)};
  $('selectRegion').onclick=()=>{state.selecting=!state.selecting;if(state.selecting)state.pinned=true;$('stage').classList.toggle('selecting',state.selecting);$('selectRegion').textContent=state.selecting?'Drag on the snapshot':'Select a region';showMedia()};
  $('clearRegion').onclick=()=>{state.region=null;state.pinned=false;$('clearRegion').hidden=true;if(state.snapshots.length)selectSnapshot(state.snapshots[0]);else drawRegion()};
  $('stage').addEventListener('pointerdown',e=>{if(!state.selecting||!state.selected)return;dragStart=imagePoint(e);$('stage').setPointerCapture(e.pointerId)});
  $('stage').addEventListener('pointermove',e=>{if(!dragStart)return;const p=imagePoint(e);state.region={x:Math.min(p.x,dragStart.x),y:Math.min(p.y,dragStart.y),width:Math.abs(p.x-dragStart.x),height:Math.abs(p.y-dragStart.y)};drawRegion()});
  $('stage').addEventListener('pointerup',e=>{if(!dragStart)return;dragStart=null;if(state.region.width<.01||state.region.height<.01){state.region=null;drawRegion();return}state.selecting=false;$('stage').classList.remove('selecting');$('selectRegion').textContent='Select a region';$('clearRegion').hidden=false});
  window.addEventListener('resize',drawRegion);
  document.querySelectorAll('.preset').forEach(button=>button.onclick=()=>{
    if(button.textContent.includes('I missed that')&&state.messages.length){const last=state.messages[state.messages.length-1];
      const shot=state.snapshots.find(s=>s.snapshot_id===last.snapshot_id);if(shot){selectSnapshot(shot);state.region=last.region;drawRegion()}
      $('conversation').scrollTop=$('conversation').scrollHeight;return}
    ask(button.dataset.question)
  });
  $('askForm').onsubmit=e=>{e.preventDefault();const q=$('question').value.trim();if(q)ask(q)};
  setInterval(refresh,5000);
}
init();
