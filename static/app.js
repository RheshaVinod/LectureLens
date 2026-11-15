const $ = id => document.getElementById(id);
const state = {sessionId:null,sessionStartedEpochMs:null,serverClockOffsetMs:0,stream:null,timer:null,recordingTimer:null,recordingStartedAt:null,snapshots:[],selected:null,selectedFrames:[],multiFrame:false,region:null,selecting:false,pinned:false,busy:false,captureInFlight:false,visualGeneration:0,lastCaptureAt:0,messages:[],allMessages:[],chats:[],classes:[],decks:[],uploadingDeck:false,deckUploadProgress:'',deckError:null,activeChatId:null,activeClassId:null,chatSelectionReady:false,deletingChatId:null,suppressStageClick:false,viewerZoom:1,viewerSelecting:false};
const voiceQuestionState = {recognition:null,starting:false,stopping:false,base:'',resumeCaptions:false,error:null};
const voiceLanguages = {en:'en-US',es:'es-ES',fr:'fr-FR',zh:'zh-CN',hi:'hi-IN',ko:'ko-KR',ja:'ja-JP',de:'de-DE',pt:'pt-BR',ar:'ar-SA'};
const readAloudState = {source:null,context:null,controller:null,messageId:null,resumeCaptions:false,starting:false,captionPause:null,request:0};
const readAloudCache = new Map();

function cameraConstraints(deviceId=$('camera')?.value){
  return {audio:false,video:{...(deviceId?{deviceId:{exact:deviceId}}:{facingMode:{ideal:'environment'}}),width:{ideal:1280},height:{ideal:720}}};
}
async function refreshCameras(){
  const select=$('camera');
  if(!select||!navigator.mediaDevices?.enumerateDevices)return;
  const selected=select.value||localStorage.getItem('lecturelens-camera-device')||'';
  try{
    const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='videoinput');
    select.replaceChildren(new Option('System default',''));
    devices.forEach((device,index)=>select.add(new Option(device.label||`Camera ${index+1}`,device.deviceId)));
    select.value=[...select.options].some(option=>option.value===selected)?selected:'';
    select.disabled=!devices.length;
  }catch{select.disabled=false}
}
async function attachCameraStream(stream){
  const previous=state.stream;
  state.stream=stream;
  const video=$('video');video.srcObject=stream;await video.play();
  stream.getVideoTracks()[0].onended=stopCombined;
  previous?.getTracks().forEach(track=>{track.onended=null;track.stop()});
  await refreshCameras();
  const selected=$('camera').value;if(selected)localStorage.setItem('lecturelens-camera-device',selected);else localStorage.removeItem('lecturelens-camera-device');
  showMedia();
}
async function switchCamera(){
  const selected=$('camera').value;
  if(selected)localStorage.setItem('lecturelens-camera-device',selected);else localStorage.removeItem('lecturelens-camera-device');
  if(!state.stream)return;
  $('camera').disabled=true;status('Switching camera…');
  try{
    const stream=await navigator.mediaDevices.getUserMedia(cameraConstraints(selected));
    await attachCameraStream(stream);await capture('timer');status('Camera switched · audio remains live');
  }catch(error){
    await refreshCameras();
    const detail=error.name==='NotReadableError'?'That camera is busy in another app.':`Could not switch camera: ${error.message}`;
    status(detail,true);
  }finally{$('camera').disabled=false}
}

function applyTheme(theme){
  document.documentElement.dataset.theme=theme;
  const button=$('themeToggle');
  const next=theme==='dark'?'light':'dark';
  button.setAttribute('aria-label',`Switch to ${next} mode`);
  button.title=`Switch to ${next} mode`;
  button.firstElementChild.textContent=theme==='dark'?'☀':'☾';
}
function status(message, error=false){ $('captureStatus').textContent=message; $('captureStatus').style.color=error?'#a34534':''; if(error||message.startsWith('Requesting'))$('stageBadge').textContent=message; }
async function api(path, options={}){
  const response=await fetch(path,options);
  let data; try{data=await response.json()}catch{data={detail:'Unexpected server response'}}
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Request failed');
  return data;
}
function elapsed(ms){const s=Math.floor(ms/1000);return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`}
function deckKey(suffix){return `lecturelens-${suffix}:${state.sessionId}:${state.activeClassId}`}
function disabledDeckIds(){try{return JSON.parse(localStorage.getItem(deckKey('disabled-decks'))||'[]')}catch{return []}}
function currentDecks(){return state.decks.filter(item=>item.class_id===state.activeClassId)}
function selectedDeckIds(){
  if(!$('useDeck').checked)return [];
  const disabled=new Set(disabledDeckIds());
  return currentDecks().filter(deck=>!disabled.has(deck.deck_id)).map(deck=>deck.deck_id);
}
function renderDeckStatus(){
  const element=$('deckStatus');
  const decks=currentDecks();
  const toggle=$('useDeck');toggle.disabled=!decks.length;
  toggle.checked=!!decks.length&&localStorage.getItem(deckKey('use-deck'))!=='false';
  const selected=new Set(selectedDeckIds());
  const picker=$('deckPicker'),list=$('deckList');picker.hidden=!decks.length;list.replaceChildren();
  $('deckPickerCount').textContent=toggle.checked?`${selected.size}/${decks.length} selected`:'Off';
  for(const deck of decks){
    const label=document.createElement('label');label.className='deck-choice';label.title=deck.filename;
    const input=document.createElement('input');input.type='checkbox';input.checked=!disabledDeckIds().includes(deck.deck_id);input.disabled=!toggle.checked;input.setAttribute('aria-label',`Use ${deck.filename}`);
    input.onchange=()=>{const disabled=new Set(disabledDeckIds());if(input.checked)disabled.delete(deck.deck_id);else disabled.add(deck.deck_id);
      localStorage.setItem(deckKey('disabled-decks'),JSON.stringify([...disabled]));state.deckError=null;renderDeckStatus()};
    const name=document.createElement('span');name.className='deck-choice-name';name.textContent=deck.filename;
    const count=document.createElement('span');count.className='deck-choice-count';
    const unit=deck.kind==='slides'?'slide':deck.kind==='pdf'?'page':deck.kind==='image'?'image':'section';
    count.textContent=`${deck.slide_count} ${unit}${deck.slide_count===1?'':'s'}`;
    label.append(input,name,count);list.append(label);
  }
  const message=state.uploadingDeck?state.deckUploadProgress:state.deckError||'';
  element.hidden=!message;element.textContent=message;element.classList.toggle('error',!!state.deckError&&!state.uploadingDeck);
  $('uploadDeck').disabled=state.uploadingDeck;
}
async function uploadDeck(files){
  if(!files.length||!state.sessionId||!state.activeClassId)return;
  const classId=state.activeClassId;
  state.uploadingDeck=true;state.deckError=null;renderDeckStatus();
  const failures=[];
  try{for(let index=0;index<files.length;index++){
    const file=files[index];state.deckUploadProgress=`Reading file ${index+1} of ${files.length}: ${file.name}`;renderDeckStatus();
    const form=new FormData();form.append('file',file);form.append('class_id',classId);
    try{const deck=await api(`/api/sessions/${state.sessionId}/decks`,{method:'POST',body:form});state.decks.unshift(deck)}
    catch(error){failures.push(`${file.name}: ${error.message}`)}
  }}finally{state.uploadingDeck=false;state.deckUploadProgress='';state.deckError=failures.length?failures.join(' · '):null;$('slideFile').value='';renderDeckStatus()}
}
async function uploadSlides(files){
  if(!files.length)return;
  const form=new FormData();for(const file of files)form.append('files',file);
  $('uploadSlides').disabled=true;status('Uploading lecture slides…');
  try{const data=await api(`/api/sessions/${state.sessionId}/slides`,{method:'POST',body:form});
    state.snapshots=[...data.slides,...state.snapshots];if(data.slides.length)selectSnapshot(data.slides[0],true);renderMoments();
    status(`${data.slides.length} slide${data.slides.length===1?'':'s'} added to lecture context`)
  }catch(error){status(error.message,true)}finally{$('uploadSlides').disabled=false;$('slideFiles').value=''}
}
async function openPhoneJoin(){
  const dialog=$('phoneDialog');$('connectPhone').disabled=true;
  try{const join=await api(`/api/sessions/${state.sessionId}/phone-join`);
    $('phoneQr').src=`${join.qr_url}?v=${Date.now()}`;$('phoneLink').href=join.capture_url;$('phoneLink').textContent=join.capture_url;
    $('phoneWarning').hidden=!join.localhost_warning;dialog.showModal()
  }catch(error){status(error.message,true)}finally{$('connectPhone').disabled=false}
}
function renderRichText(element,text){
  const source=String(text??'').replace(/\\\(/g,'$').replace(/\\\)/g,'$').replace(/\\\[/g,'$$').replace(/\\\]/g,'$$');
  if(!window.marked||!window.DOMPurify||!window.renderMathInElement){element.textContent=source;return}
  const html=window.marked.parse(source,{gfm:true,breaks:false});
  element.innerHTML=window.DOMPurify.sanitize(html,{
    ALLOWED_TAGS:['p','strong','em','ul','ol','li','code','pre','blockquote','br','h3','h4'],
    ALLOWED_ATTR:[]
  });
  window.renderMathInElement(element,{
    delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],
    throwOnError:false,trust:false,
    ignoredTags:['script','noscript','style','textarea','pre','code']
  });
}
function showMedia(){
  const live=!!state.stream && !state.pinned && !state.region && !state.selecting;
  $('video').classList.toggle('active',live);
  $('snapshot').classList.toggle('active',!!state.selected&&!live);
  $('stageEmpty').hidden=!!state.stream||!!state.selected;
  $('stageBadge').textContent=live?'● LIVE CAMERA':state.selected?`SNAPSHOT · ${elapsed(state.selected.captured_at_ms)}`:state.stream?'● LIVE CAMERA':'Waiting for camera';
  $('selectRegion').disabled=!state.selected;
  $('expandImage').hidden=!state.selected;
  $('useLatest').hidden=!chatSnapshots().length||!!state.selected&&!state.pinned;
  $('selectedMoment').textContent=state.multiFrame&&state.selectedFrames.length?`${state.selectedFrames.length} selected frame${state.selectedFrames.length===1?'':'s'} · F1 ${elapsed(state.selectedFrames[0].captured_at_ms)}${state.region?' · R1 on F1':''}`:state.pinned&&state.selected?`Pinned capture · ${elapsed(state.selected.captured_at_ms)}${state.region?' · R1':''}`:state.selected?'Latest capture':'Live transcript context · camera optional';
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
function chatSnapshots(){
  const chat=state.chats.find(item=>item.chat_id===state.activeChatId);
  if(!chat)return [];
  const startedAt=Math.max(0,chat.created_at_ms-state.sessionStartedEpochMs);
  const referenced=new Set(state.allMessages.filter(message=>message.chat_id===chat.chat_id)
    .flatMap(message=>message.snapshot_ids?.length?message.snapshot_ids:(message.snapshot_id?[message.snapshot_id]:[])));
  return state.snapshots.filter(shot=>shot.captured_at_ms>startedAt||referenced.has(shot.snapshot_id));
}
function renderMoments(){
  const target=$('moments');target.replaceChildren();
  const shots=chatSnapshots();
  const visible=[...state.selectedFrames,...(state.selected?[state.selected]:[]),...shots]
    .filter((shot,index,items)=>items.findIndex(item=>item.snapshot_id===shot.snapshot_id)===index);
  $('momentCount').textContent=`${visible.length} recent snapshot${visible.length===1?'':'s'}`;
  $('multiFrameToggle').disabled=!visible.length;
  $('multiFrameToggle').setAttribute('aria-pressed',String(state.multiFrame));
  $('multiFrameToggle').textContent=state.multiFrame?'Clear frames':'Select frames';
  if(!visible.length){const p=document.createElement('div');p.className='moment-placeholder';p.textContent='New captures will appear here.';target.append(p);return}
  for(const shot of visible){const frameIndex=state.multiFrame?state.selectedFrames.findIndex(item=>item.snapshot_id===shot.snapshot_id):-1;
    const button=document.createElement('button');button.className='moment'+(state.selected?.snapshot_id===shot.snapshot_id?' active':'')+(frameIndex>=0?' frame-selected':'');
    button.title=state.multiFrame?`${frameIndex>=0?'Remove':'Add'} snapshot at ${elapsed(shot.captured_at_ms)}${frameIndex<0?' (up to 4)':''}`:`Snapshot at ${elapsed(shot.captured_at_ms)}`;
    button.setAttribute('aria-pressed',String(frameIndex>=0));
    const img=document.createElement('img');img.src=shot.image_url;img.alt=`Snapshot at ${elapsed(shot.captured_at_ms)}`;
    const label=document.createElement('span');label.textContent=elapsed(shot.captured_at_ms);button.append(img,label);
    if(frameIndex>=0){const badge=document.createElement('b');badge.className='frame-number';badge.textContent=`F${frameIndex+1}`;button.append(badge)}
    button.onclick=()=>state.multiFrame?toggleFrame(shot):selectSnapshot(shot,true);target.append(button)}
}
function selectSnapshot(shot,pinned=false,keepFrames=false){
  if(!keepFrames){state.multiFrame=false;state.selectedFrames=[]}
  state.selected=shot;state.pinned=pinned;state.region=null;state.selecting=false;$('selectRegion').textContent='Select region';$('stage').classList.remove('selecting');$('snapshot').src=shot.image_url;$('clearRegion').hidden=true;showMedia();renderMoments()
}
function clearChatVisualContext(){
  state.visualGeneration++;
  state.selected=null;state.selectedFrames=[];state.multiFrame=false;state.pinned=false;state.region=null;state.selecting=false;
  $('snapshot').removeAttribute('src');$('clearRegion').hidden=true;
  $('selectRegion').textContent='Select region';$('stage').classList.remove('selecting');
  showMedia();renderMoments();
}
function toggleFrame(shot){
  const index=state.selectedFrames.findIndex(item=>item.snapshot_id===shot.snapshot_id);
  if(index>=0){
    if(state.selectedFrames.length===1)return;
    state.selectedFrames.splice(index,1);
    if(index===0)selectSnapshot(state.selectedFrames[0],true,true);
  }else{
    if(state.selectedFrames.length>=4){status('Choose up to 4 frames per question',true);return}
    state.selectedFrames.push(shot);
  }
  showMedia();renderMoments();
}
function startFrameSelection(){
  if(!state.selected){
    const latest=chatSnapshots()[0];
    if(!latest)return;
    selectSnapshot(latest,true);
  }
  if(state.multiFrame){state.multiFrame=false;state.selectedFrames=[]}
  else{state.multiFrame=true;state.selectedFrames=[state.selected];state.pinned=true}
  showMedia();renderMoments();
}
function renderChats(){
  const list=$('chatList');list.replaceChildren();
  for(const lectureClass of state.classes){
    const group=document.createElement('section');group.className='class-group';
    const heading=document.createElement('div');heading.className='class-heading';
    const name=document.createElement('button');name.type='button';name.className='class-name';name.textContent=lectureClass.name;
    name.setAttribute('aria-current',lectureClass.class_id===state.activeClassId?'true':'false');
    name.onclick=()=>openClass(lectureClass.class_id);
    const add=document.createElement('button');add.type='button';add.className='class-add-chat';add.textContent='＋';add.title=`New chat in ${lectureClass.name}`;add.setAttribute('aria-label',add.title);
    add.onclick=()=>createChat(lectureClass.class_id).catch(error=>status(error.message,true));
    heading.append(name,add);group.append(heading);
    const chats=state.chats.filter(chat=>chat.class_id===lectureClass.class_id);
    if(!chats.length){const empty=document.createElement('p');empty.className='chat-list-empty';empty.textContent='No chats yet';group.append(empty)}
    for(const chat of chats){
      const row=document.createElement('div');row.className='chat-row';
      const button=document.createElement('button');button.type='button';button.className='chat-item';button.title=chat.title;
      button.setAttribute('aria-current',chat.chat_id===state.activeChatId?'page':'false');
      const title=document.createElement('span');title.className='chat-item-title';title.textContent=chat.title;
      button.append(title);button.onclick=()=>switchChat(chat.chat_id);
      const move=document.createElement('select');move.className='chat-move';move.title=`Move ${chat.title} to another class`;
      move.setAttribute('aria-label',move.title);
      for(const optionClass of state.classes){const option=document.createElement('option');option.value=optionClass.class_id;option.textContent=optionClass.name;move.append(option)}
      move.value=chat.class_id;move.onchange=()=>moveChat(chat,move.value);
      const remove=document.createElement('button');remove.type='button';remove.className='chat-delete';
      remove.title=`Delete ${chat.title}`;remove.setAttribute('aria-label',remove.title);
      remove.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/></svg>';
      remove.onclick=()=>requestDeleteChat(chat);
      row.append(button,move,remove);group.append(row);
    }
    list.append(group);
  }
}
function selectClass(classId){
  state.activeClassId=classId;state.deckError=null;localStorage.setItem(`lecturelens-class:${state.sessionId}`,classId);renderChats();renderDeckStatus();
}
function openClass(classId){
  if(state.busy)return;
  selectClass(classId);
  const latest=state.chats.find(chat=>chat.class_id===classId);
  if(latest){switchChat(latest.chat_id);return}
  state.activeChatId=null;state.messages=[];
  localStorage.removeItem(`lecturelens-chat:${state.sessionId}`);
  $('question').value='';renderChats();renderMessages();renderDeckStatus();
  clearChatVisualContext();
}
async function moveChat(chat,classId){
  try{await api(`/api/sessions/${state.sessionId}/chats/${chat.chat_id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({class_id:classId})});
    chat.class_id=classId;if(chat.chat_id===state.activeChatId)selectClass(classId);else renderChats();
  }catch(error){status(error.message,true);renderChats()}
}
function requestDeleteChat(chat){
  if(state.busy)return;
  state.deletingChatId=chat.chat_id;
  $('deleteChatDescription').textContent=`“${chat.title}” and its questions and answers will be removed from this lecture.`;
  $('deleteChatDialog').showModal();
}
async function deleteChat(){
  const chatId=state.deletingChatId;if(!chatId)return;
  $('confirmDeleteChat').disabled=true;
  try{
    await api(`/api/sessions/${state.sessionId}/chats/${chatId}`,{method:'DELETE'});
    state.chats=state.chats.filter(chat=>chat.chat_id!==chatId);
    state.allMessages=state.allMessages.filter(message=>message.chat_id!==chatId);
    $('deleteChatDialog').close();
    if(state.activeChatId===chatId){
      state.activeChatId=null;localStorage.removeItem(`lecturelens-chat:${state.sessionId}`);
      const next=state.chats.find(chat=>chat.class_id===state.activeClassId);
      if(next)switchChat(next.chat_id);
      else{state.messages=[];$('question').value='';renderChats();renderMessages();clearChatVisualContext()}
    }else renderChats();
  }catch(error){$('deleteChatDescription').textContent=error.message}
  finally{$('confirmDeleteChat').disabled=false}
}
async function showMessageSnapshot(message,clickedSnapshotId=null){
  if(!message)return;
  try{
    const ids=message.snapshot_ids?.length?message.snapshot_ids:[message.snapshot_id];
    const shots=await Promise.all(ids.map(ident=>state.snapshots.find(s=>s.snapshot_id===ident)||api(`/api/snapshots/${ident}`)));
    if(state.activeChatId!==message.chat_id)return;
    const primary=shots[0];selectSnapshot(primary,true);
    if(ids.length>1){state.multiFrame=true;state.selectedFrames=shots}
    if(clickedSnapshotId&&clickedSnapshotId!==primary.snapshot_id){
      state.selected=shots.find(item=>item.snapshot_id===clickedSnapshotId)||primary;
      state.selectedFrames=[state.selected,...shots.filter(item=>item.snapshot_id!==clickedSnapshotId)];
      $('snapshot').src=state.selected.image_url;
      state.region=null;
    }else state.region=message.region;
    showMedia();renderMoments();
  }catch(e){status(e.message,true)}
}
async function openMessageRegion(message){
  if(!message.region||!message.snapshot_id)return;
  await showMessageSnapshot(message);
  if(state.selected?.snapshot_id!==message.snapshot_id)return;
  openImageViewer();
  $('viewerCaption').textContent=`R1 · Snapshot ${elapsed(state.selected.captured_at_ms)}`;
  showRegionDetail(message);
}
function switchChat(chatId){
  if(state.busy||chatId===state.activeChatId)return;
  state.activeChatId=chatId;localStorage.setItem(`lecturelens-chat:${state.sessionId}`,chatId);
  const chat=state.chats.find(item=>item.chat_id===chatId);if(chat){state.activeClassId=chat.class_id;state.deckError=null;localStorage.setItem(`lecturelens-class:${state.sessionId}`,chat.class_id)}
  state.messages=state.allMessages.filter(message=>message.chat_id===chatId);
  $('question').value='';renderChats();renderMessages();renderDeckStatus();
  if(state.messages.length)showMessageSnapshot(state.messages[state.messages.length-1]);
  else clearChatVisualContext();
}
async function createChat(classId=state.activeClassId,{preserveVisualContext=false}={}){
  if(state.busy)return;
  const chat=await api(`/api/sessions/${state.sessionId}/chats`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({class_id:classId})});
  state.chats.unshift(chat);state.activeChatId=chat.chat_id;
  selectClass(chat.class_id);
  localStorage.setItem(`lecturelens-chat:${state.sessionId}`,chat.chat_id);
  state.messages=[];$('question').value='';
  if(!preserveVisualContext){
    clearChatVisualContext();
    if(state.stream)void capture('manual');
  }
  renderChats();renderMessages();return chat;
}
async function refresh(){
  if(!state.sessionId)return;
  try{const requestedAt=Date.now(),data=await api(`/api/sessions/${state.sessionId}`);
    const initialLoad=!state.chatSelectionReady;
    state.sessionStartedEpochMs=data.started_epoch_ms;
    state.serverClockOffsetMs=data.server_epoch_ms-Math.round((requestedAt+Date.now())/2);
    window.LectureLensSession={id:state.sessionId,startedEpochMs:state.sessionStartedEpochMs};
    window.dispatchEvent(new CustomEvent('lecturelens:session-ready',{detail:window.LectureLensSession}));
    state.snapshots=data.snapshots;state.decks=data.decks||[];
    state.chats=data.chats;state.classes=data.classes;
    if(!state.chatSelectionReady){
      const saved=localStorage.getItem(`lecturelens-class:${state.sessionId}`);
      const savedChat=localStorage.getItem(`lecturelens-chat:${state.sessionId}`);
      state.activeClassId=state.classes.find(lectureClass=>lectureClass.class_id===saved)?.class_id||state.chats.find(chat=>chat.chat_id===savedChat)?.class_id||state.classes[0]?.class_id||null;
      state.activeChatId=state.chats.find(chat=>chat.chat_id===savedChat&&chat.class_id===state.activeClassId)?.chat_id||state.chats.find(chat=>chat.class_id===state.activeClassId)?.chat_id||null;
      state.chatSelectionReady=true;
    }else if(state.activeChatId&&!state.chats.some(chat=>chat.chat_id===state.activeChatId)){
      state.activeChatId=state.chats.find(chat=>chat.class_id===state.activeClassId)?.chat_id||null;
    }
    state.allMessages=data.messages;
    const latestForChat=chatSnapshots()[0];
    if(state.selected&&state.stream&&!state.pinned&&!state.region&&!state.selecting&&latestForChat&&state.selected.snapshot_id!==latestForChat.snapshot_id)selectSnapshot(latestForChat);
    renderMoments();showMedia();
    const messages=data.messages.filter(message=>message.chat_id===state.activeChatId);
    if(messages.length!==state.messages.length||messages.some((message,index)=>message.message_id!==state.messages[index]?.message_id)){
      state.messages=messages;renderMessages();
    }
    renderChats();renderDeckStatus();
    if(initialLoad){
      if(messages.length)void showMessageSnapshot(messages[messages.length-1]);
      else clearChatVisualContext();
    }
  }catch(e){status(e.message,true)}
}
async function capture(reason='timer'){
  if(!state.stream||!$('video').videoWidth||state.captureInFlight)return;
  const visualGeneration=state.visualGeneration;
  state.captureInFlight=true;state.lastCaptureAt=Date.now();
  const canvas=$('captureCanvas'),video=$('video');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.82));
  if(!blob){state.captureInFlight=false;return}const form=new FormData();form.append('image',blob,'snapshot.jpg');form.append('source','camera');form.append('capture_reason',reason);
  try{const shot=await api(`/api/sessions/${state.sessionId}/snapshots`,{method:'POST',body:form});
    state.snapshots.unshift(shot);state.snapshots=state.snapshots.slice(0,12);
    if(visualGeneration===state.visualGeneration&&!state.pinned&&!state.region&&!state.selecting&&!state.multiFrame)selectSnapshot(shot);else renderMoments();
    status(`${reason==='caption'?'Caption-linked snapshot':'Capturing'} · ${elapsed(shot.captured_at_ms)}`)
  }catch(e){status(e.message,true)}finally{state.captureInFlight=false}
}
function updateRecordingButton(){
  if(!state.recordingStartedAt)return;
  $('startCamera').innerHTML=`<span class="recording-dot">●</span> Live ${elapsed(Date.now()-state.recordingStartedAt)}`;
}
function startRecordingIndicator(){
  if(state.recordingTimer)return;
  state.recordingStartedAt=Date.now();updateRecordingButton();state.recordingTimer=setInterval(updateRecordingButton,1000);
  $('startCamera').classList.add('recording');$('startCamera').disabled=true;$('stopCamera').disabled=false;
}
function stopRecordingIndicator(){
  clearInterval(state.recordingTimer);state.recordingTimer=null;state.recordingStartedAt=null;
  $('startCamera').classList.remove('recording');$('startCamera').innerHTML='Start lecture <span>↗</span>';
  $('startCamera').disabled=false;$('stopCamera').disabled=true;
}
function setTranscriptCollapsed(collapsed){
  document.querySelector('.audio-card').classList.toggle('transcript-collapsed',collapsed);
  $('transcriptToggle').textContent=collapsed?'⌄':'⌃';
  $('transcriptToggle').setAttribute('aria-label',collapsed?'Show transcript':'Hide transcript');
  $('transcriptToggle').title=collapsed?'Show transcript':'Hide transcript';
  $('transcriptToggle').setAttribute('aria-expanded',String(!collapsed));
  localStorage.setItem('lecturelens-transcript-collapsed',String(collapsed));
}
function stop(){clearInterval(state.timer);state.timer=null;state.stream?.getTracks().forEach(t=>{t.onended=null;t.stop()});state.stream=null;status('Camera off');showMedia()}
async function start(){
  if(!window.isSecureContext){status(`Camera blocked: ${location.origin} is not a secure context. Open localhost on this device or use HTTPS.`,true);return}
  if(!navigator.mediaDevices?.getUserMedia){status(`Camera API unavailable at ${location.origin}. Check Chrome camera permissions and whether this page is inside another app.`,true);return}
  $('startCamera').disabled=true;status('Requesting camera permission…');
  try{const stream=await navigator.mediaDevices.getUserMedia(cameraConstraints());
    await attachCameraStream(stream);clearChatVisualContext();$('startCamera').disabled=true;$('stopCamera').disabled=false;
    status('Capturing every 4 seconds');showMedia();await capture('start');state.timer=setInterval(()=>void capture('timer'),4000)
  }catch(e){
    state.stream?.getTracks().forEach(track=>track.stop());state.stream=null;$('startCamera').disabled=false;
    const detail=e.name==='NotAllowedError'?'Camera permission was blocked. Allow camera access in your browser, then try again.':
      e.name==='NotFoundError'?'No camera found on this device.':
      e.name==='NotReadableError'?'Camera is busy in another app. Close it and try again.':
      `Camera unavailable: ${e.message}`;
    status(detail,true);showMedia();$('stageBadge').textContent=detail;
  }
}
async function startCombined(){
  $('startCamera').disabled=true;status('Requesting camera and microphone permission…');
  await Promise.all([start(),window.LectureLensAudio?.start?.()]);
  const listening=window.LectureLensAudio?.isListening?.();
  if(state.stream||listening)startRecordingIndicator();else stopRecordingIndicator();
  if(state.stream&&listening)status('Audio + video live · snapshots every 4 seconds');
  else if(state.stream)status('Video live · microphone captions are not active',true);
  else if(listening)status('Audio live · camera is unavailable',true);
}
function stopCombined(){stop();void window.LectureLensAudio?.stop?.();stopRecordingIndicator();status('Lecture capture stopped')}
function imagePoint(event){
  const box=$('stage').getBoundingClientRect(), shot=state.selected;
  const ratio=shot.width_px/shot.height_px,view=box.width/box.height;
  let w,h,l,t;if(view>ratio){h=box.height;w=h*ratio;l=(box.width-w)/2;t=0}else{w=box.width;h=w/ratio;l=0;t=(box.height-h)/2}
  return {x:Math.max(0,Math.min(1,(event.clientX-box.left-l)/w)),y:Math.max(0,Math.min(1,(event.clientY-box.top-t)/h))}
}
function setViewerZoom(zoom){
  state.viewerZoom=Math.max(1,Math.min(4,zoom));
  $('viewerZoomLabel').textContent=`${Math.round(state.viewerZoom*100)}%`;
  $('viewerZoomOut').disabled=state.viewerZoom===1;$('viewerZoomIn').disabled=state.viewerZoom===4;
  layoutViewerImage();
}
function layoutViewerImage(){
  const image=$('viewerImage'),viewport=$('viewerViewport'),frame=$('viewerImageFrame');
  if(!image.naturalWidth||!image.naturalHeight)return;
  const fit=Math.min((viewport.clientWidth-36)/image.naturalWidth,(viewport.clientHeight-36)/image.naturalHeight);
  frame.style.width=`${image.naturalWidth*fit*state.viewerZoom}px`;
  frame.style.height=`${image.naturalHeight*fit*state.viewerZoom}px`;
}
function drawViewerSelection(region=state.region){
  const overlay=$('viewerSelection');
  if(!region){overlay.hidden=true;return}
  Object.assign(overlay.style,{left:`${region.x*100}%`,top:`${region.y*100}%`,width:`${region.width*100}%`,height:`${region.height*100}%`});
  overlay.hidden=false;
}
function viewerPoint(event){
  const box=$('viewerImageFrame').getBoundingClientRect();
  return {x:Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),y:Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))};
}
function setViewerSelecting(selecting){
  state.viewerSelecting=selecting;
  if(selecting)hideRegionDetail();
  $('viewerImageFrame').classList.toggle('selecting',selecting);
  $('viewerSelectRegion').textContent=selecting?'Drag over image':'Select region';
  $('viewerSelectRegion').setAttribute('aria-pressed',String(selecting));
}
let regionPreviewRequest=0;
function hideRegionDetail(){
  regionPreviewRequest++;
  $('viewerRegionPanel').hidden=true;
  $('imageViewer').classList.remove('has-region-detail');
  if($('imageViewer').open){
    if(state.selected)$('viewerCaption').textContent=`Snapshot · ${elapsed(state.selected.captured_at_ms)}`;
    setViewerZoom(1);
  }
}
function regionAnswerExcerpt(message){
  const answer=message.result?.answer||'';
  const marker=answer.indexOf('[[R1]]');
  if(marker<0)return message.question;
  const preceding=answer.slice(0,marker).replace(/\[\[[FAM]\d+\]\]/g,'').trim();
  const paragraph=preceding.split(/\n\s*\n/).pop()||preceding;
  return (paragraph.length>320?`…${paragraph.slice(-320)}`:paragraph).trim()||message.question;
}
function showRegionDetail(message){
  const request=++regionPreviewRequest;
  const panel=$('viewerRegionPanel');panel.hidden=false;
  $('imageViewer').classList.add('has-region-detail');
  const excerpt=$('viewerRegionExcerpt');excerpt.replaceChildren();
  const marked=message.result?.answer?.includes('[[R1]]');
  $('viewerRegionCopyLabel').textContent=marked?'FROM THE ANSWER':'QUESTION ABOUT THIS REGION';
  renderRichText(excerpt,regionAnswerExcerpt(message));
  const preview=$('viewerRegionCrop');preview.removeAttribute('src');
  const image=new Image();
  image.onload=()=>{
    if(request!==regionPreviewRequest||!$('imageViewer').open)return;
    const region=message.region;
    const x=Math.max(0,Math.floor(region.x*image.naturalWidth));
    const y=Math.max(0,Math.floor(region.y*image.naturalHeight));
    const width=Math.max(1,Math.min(image.naturalWidth-x,Math.ceil(region.width*image.naturalWidth)));
    const height=Math.max(1,Math.min(image.naturalHeight-y,Math.ceil(region.height*image.naturalHeight)));
    const scale=Math.min(1,720/Math.max(width,height));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
    canvas.getContext('2d').drawImage(image,x,y,width,height,0,0,canvas.width,canvas.height);
    preview.src=canvas.toDataURL('image/jpeg',.9);
  };
  image.src=`/api/snapshots/${message.snapshot_id}/image`;
  setViewerZoom(1);
  $('viewerSelection').classList.remove('region-reveal');
  void $('viewerSelection').offsetWidth;
  $('viewerSelection').classList.add('region-reveal');
}
function openImageViewer(){
  if(!state.selected||state.selecting)return;
  hideRegionDetail();
  state.pinned=true;showMedia();
  setViewerSelecting(false);
  $('viewerImage').src=state.selected.image_url;
  $('viewerCaption').textContent=`Snapshot · ${elapsed(state.selected.captured_at_ms)}`;
  $('viewerClearRegion').hidden=!state.region;
  drawViewerSelection();
  $('imageViewer').showModal();setViewerZoom(1);
}
let dragStart=null;
let viewerDragStart=null;
function openSource(source){
  const dialog=$('sourceViewer');
  $('sourceViewerLabel').textContent=`${source.label} · ${source.kind==='frame'?'Board frame':source.kind==='transcript'?'Lecture caption':'Uploaded material'}`;
  $('sourceViewerTitle').textContent=source.title;
  const picture=$('sourceViewerImage');picture.hidden=!source.image_url;
  if(source.image_url)picture.src=source.image_url;else picture.removeAttribute('src');
  const excerpt=$('sourceViewerExcerpt');excerpt.hidden=!source.excerpt||!!source.image_url;excerpt.textContent=source.excerpt||'';
  dialog.showModal();
}
function linkAnswerSources(element,sources,message){
  const byLabel=new Map(sources.map(source=>[source.label,source]));
  if(!byLabel.size&&!message.region)return;
  const walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT);
  const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  for(const node of nodes){
    if(node.parentElement?.closest('code,pre,.katex'))continue;
    const text=node.textContent;const matches=[...text.matchAll(/\[\[([FAM]\d+|R1)\]\]/g)];if(!matches.length)continue;
    const fragment=document.createDocumentFragment();let position=0;
    for(const match of matches){
      fragment.append(document.createTextNode(text.slice(position,match.index)));
      const source=byLabel.get(match[1]);
      if(source||(match[1]==='R1'&&message.region)){const button=document.createElement('button');button.type='button';button.className='source-ref';
        const selectedRegion=match[1]==='R1';
        button.textContent=selectedRegion?'R1':source.label;
        if(selectedRegion)button.classList.add('region-ref');
        button.title=selectedRegion?'Open highlighted region R1 in frame F1':`View ${source.title}`;
        button.setAttribute('aria-label',button.title);
        button.onclick=selectedRegion?()=>void openMessageRegion(message):()=>source.label==='F1'&&message.region?void openMessageRegion(message):openSource(source);
        fragment.append(button)}
      else fragment.append(document.createTextNode(match[0]));
      position=match.index+match[0].length;
    }
    fragment.append(document.createTextNode(text.slice(position)));node.replaceWith(fragment);
  }
}
function updateReadAloudButtons(){
  document.querySelectorAll('.read-aloud-button').forEach(button=>{
    const active=button.dataset.messageId===readAloudState.messageId;
    button.disabled=(readAloudState.starting&&!active)||voiceQuestionState.starting||!!voiceQuestionState.recognition||!(window.AudioContext||window.webkitAudioContext);
    button.classList.toggle('is-reading',active);
    button.setAttribute('aria-pressed',String(active));
    const label=active?(readAloudState.starting?'Cancel':'Stop'):'Listen';
    button.setAttribute('aria-label',active?`${label} answer audio`:'Read answer aloud');
    button.title=active?`${label} answer audio`:'Read answer aloud';
    button.querySelector('span').textContent=label;
  });
  if($('voiceQuestion'))$('voiceQuestion').disabled=readAloudState.starting||voiceQuestionState.starting||voiceQuestionState.stopping||state.busy;
}
async function stopReadAloud(resumeCaptions=true,announcement='Stopped reading answer'){
  if(!readAloudState.messageId)return false;
  const shouldResume=readAloudState.resumeCaptions;
  const pause=readAloudState.captionPause;
  readAloudState.request++;
  readAloudState.controller?.abort();readAloudState.controller=null;
  if(readAloudState.source){readAloudState.source.onended=null;try{readAloudState.source.stop()}catch{}readAloudState.source.disconnect()}
  readAloudState.source=null;readAloudState.messageId=null;readAloudState.resumeCaptions=false;
  readAloudState.captionPause=null;readAloudState.starting=false;
  $('readAloudStatus').textContent=announcement;
  if(shouldResume&&resumeCaptions&&state.stream){
    readAloudState.starting=true;updateReadAloudButtons();
    if(pause)await pause.catch(()=>{});
    try{await window.LectureLensAudio?.start?.()}catch(error){console.warn('Could not resume captions:',error)}
    readAloudState.starting=false;
  }
  updateReadAloudButtons();
  return shouldResume;
}
async function readAnswerAloud(message){
  if(!(window.AudioContext||window.webkitAudioContext))return;
  if(voiceQuestionState.starting||voiceQuestionState.recognition)return;
  if(readAloudState.messageId===message.message_id){await stopReadAloud();return}
  if(readAloudState.starting)return;
  const context=readAloudState.context||(readAloudState.context=new (window.AudioContext||window.webkitAudioContext)());
  const ready=context.resume();
  const inheritedPause=await stopReadAloud(false);
  const request=++readAloudState.request;
  readAloudState.messageId=message.message_id;readAloudState.starting=true;
  readAloudState.resumeCaptions=inheritedPause;
  readAloudState.controller=new AbortController();updateReadAloudButtons();
  const note=document.querySelector(`.read-aloud-button[data-message-id="${message.message_id}"]`)?.parentElement?.querySelector('.read-aloud-feedback');
  if(note)note.hidden=true;
  $('readAloudStatus').textContent='Preparing natural voice…';
  try{
    await ready;
    let audio=readAloudCache.get(message.message_id);
    if(!audio){
      const response=await fetch(`/api/sessions/${state.sessionId}/messages/${message.message_id}/speech`,{method:'POST',signal:readAloudState.controller.signal});
      if(!response.ok){let detail='Voice generation failed';try{detail=(await response.json()).detail||detail}catch{}throw new Error(detail)}
      audio=await response.arrayBuffer();
      readAloudCache.set(message.message_id,audio);
      if(readAloudCache.size>8)readAloudCache.delete(readAloudCache.keys().next().value);
    }
    if(request!==readAloudState.request)return;
    const buffer=await context.decodeAudioData(audio.slice(0));
    if(request!==readAloudState.request)return;
    const captionsWereOn=inheritedPause||!!window.LectureLensAudio?.isListening?.();
    readAloudState.resumeCaptions=captionsWereOn;
    if(window.LectureLensAudio?.isListening?.()){
      readAloudState.captionPause=window.LectureLensAudio.stop();
      await readAloudState.captionPause;
      readAloudState.captionPause=null;
    }
    if(request!==readAloudState.request)return;
    const source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);
    source.onended=()=>{if(readAloudState.source===source)void stopReadAloud(true,'Finished reading answer')};
    readAloudState.source=source;readAloudState.starting=false;readAloudState.controller=null;
    updateReadAloudButtons();$('readAloudStatus').textContent='Reading answer aloud';source.start();
  }catch(error){
    if(request!==readAloudState.request||error.name==='AbortError')return;
    const detail=error.message||'Voice playback failed';
    if(note){note.textContent=detail;note.hidden=false}
    await stopReadAloud(true,detail);
  }
}
function renderMessages(){
  if(readAloudState.messageId&&!state.messages.some(message=>message.message_id===readAloudState.messageId))void stopReadAloud();
  document.body.classList.toggle('chat-active',state.messages.length>0||state.busy);const root=$('conversation');root.replaceChildren();if(!state.messages.length){root.innerHTML='<div class="welcome"><span class="welcome-icon">✳</span><strong>Clarity, one question at a time.</strong><p>Your answers will appear here with the snapshot they used.</p></div>';return}
  for(const message of state.messages){const entry=document.createElement('article');entry.className='entry';const q=document.createElement('div');q.className='entry-question';q.textContent=message.question;
    const a=document.createElement('div');a.className='entry-answer';renderRichText(a,message.result.answer);linkAnswerSources(a,message.result.sources||[],message);
    if(message.region&&!message.result.answer?.includes('[[R1]]')){
      const reference=document.createElement('button');reference.type='button';reference.className='region-fallback-link';
      reference.textContent='View selected region · R1 ↗';reference.setAttribute('aria-label','Open highlighted region R1 in frame F1');
      reference.onclick=()=>void openMessageRegion(message);a.append(reference);
    }
    const meta=document.createElement('div');meta.className='entry-meta frame-evidence';
    const evidenceIds=message.snapshot_ids?.length?message.snapshot_ids:(message.snapshot_id?[message.snapshot_id]:[]);
    for(const [index,ident] of evidenceIds.entries()){
      const shot=state.snapshots.find(s=>s.snapshot_id===ident)||state.selectedFrames.find(s=>s.snapshot_id===ident);
      const frame=document.createElement('button');frame.type='button';frame.className='evidence-frame';frame.title=index===0&&message.region?'Open frame F1 with highlighted region R1':`Open frame F${index+1}`;
      const thumb=document.createElement('img');thumb.src=`/api/snapshots/${ident}/image`;thumb.alt=`Evidence frame F${index+1}`;
      const label=document.createElement('span');label.textContent=`F${index+1}${shot?` · ${elapsed(shot.captured_at_ms)}`:''}${index===0&&message.region?' · R1':''}`;
      frame.append(thumb,label);frame.onclick=()=>index===0&&message.region?void openMessageRegion(message):void showMessageSnapshot(message,ident);meta.append(frame);
    }
    if(!evidenceIds.length&&!message.result.sources?.length){const label=document.createElement('span');label.textContent='Audio or uploaded file context';meta.append(label)}
    const transcriptTimes=message.result.source_timestamps||[];
    if(transcriptTimes.length){const label=document.createElement('span');label.textContent=`Transcript ${transcriptTimes.map(item=>`${elapsed(item.start_ms)}–${elapsed(item.end_ms)}`).join(', ')}`;meta.append(label)}
    const actions=document.createElement('div');actions.className='answer-actions';
    const readButton=document.createElement('button');readButton.type='button';readButton.className='read-aloud-button';readButton.dataset.messageId=message.message_id;
    readButton.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4Z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/></svg><span>Listen</span>';
    readButton.onclick=()=>void readAnswerAloud(message);actions.append(readButton);
    const readFeedback=document.createElement('span');readFeedback.className='read-aloud-feedback';readFeedback.hidden=true;actions.append(readFeedback);
    entry.append(q,a,actions);
    if(meta.childNodes.length&&!message.result.sources?.length)entry.append(meta);
    if(message.result.sources?.length){const list=document.createElement('div');list.className='source-list';
      const heading=document.createElement('strong');heading.className='source-list-title';heading.textContent='Sources';list.append(heading);
      for(const source of message.result.sources){const card=document.createElement('button');card.type='button';card.className='source-card';const linkedRegion=source.label==='F1'&&!!message.region;if(linkedRegion)card.classList.add('region-source-card');card.title=linkedRegion?'Open frame F1 with highlighted region R1':`Open ${source.title}`;
        if(source.image_url){const image=document.createElement('img');image.src=source.image_url;image.alt='';card.append(image)}
        const copy=document.createElement('span');copy.className='source-card-copy';const title=document.createElement('strong');title.textContent=`${source.label}${linkedRegion?' · R1':''} · ${source.title}`;copy.append(title);
        if(source.excerpt&&!source.image_url){const excerpt=document.createElement('span');excerpt.className='source-card-excerpt';excerpt.textContent=source.excerpt;copy.append(excerpt)}
        card.append(copy);card.onclick=linkedRegion?()=>void openMessageRegion(message):()=>openSource(source);list.append(card)}entry.append(list)}
    for(const [title,list] of [['Visible',message.result.visible_evidence],['Files',message.result.sources?.length?[]:message.result.slide_references],['Explanation',message.result.inferred_explanation],['Uncertain',message.result.uncertainties]]){if(Array.isArray(list)&&list.length){const line=document.createElement('div');line.className='evidence';const strong=document.createElement('strong');strong.textContent=title+' · ';const content=document.createElement('span');renderRichText(content,list.join(' · '));line.append(strong,content);entry.append(line)}}root.append(entry)}root.scrollTop=root.scrollHeight;updateReadAloudButtons();
}
function showQuestionState(question,message,error=false){
  const root=$('conversation');const pending=document.createElement('article');pending.className='entry pending-entry';
  const q=document.createElement('div');q.className='entry-question';q.textContent=question;
  const answer=document.createElement('div');answer.className=error?'error':'entry-answer';answer.textContent=message;
  pending.append(q,answer);root.append(pending);root.scrollTop=root.scrollHeight;
  return answer;
}
function voiceQuestionStatus(message,error=false){
  const label=$('voiceQuestionStatus');label.textContent=message;label.hidden=!message;label.classList.toggle('is-error',error);
}
function voiceQuestionControls(listening=false){
  const button=$('voiceQuestion');button.disabled=readAloudState.starting||voiceQuestionState.starting||voiceQuestionState.stopping||state.busy;
  button.classList.toggle('is-listening',listening);
  button.setAttribute('aria-pressed',String(listening));
  button.setAttribute('aria-label',listening?'Stop listening to your question':'Speak your question');
  button.title=listening?'Stop listening':'Speak your question';
  $('question').readOnly=listening;
  $('askButton').disabled=listening||state.busy;
  updateReadAloudButtons();
}
async function startVoiceQuestion(){
  if(voiceQuestionState.recognition){
    if(voiceQuestionState.stopping)return;
    voiceQuestionState.stopping=true;voiceQuestionControls(true);voiceQuestionStatus('Finishing your question…');voiceQuestionState.recognition.stop();return
  }
  if(voiceQuestionState.starting||state.busy)return;
  const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!Recognition){voiceQuestionStatus('Voice questions need Chrome speech recognition. You can still type your question.',true);return}
  if(!window.isSecureContext){voiceQuestionStatus('Microphone access needs localhost or HTTPS.',true);return}
  if(readAloudState.starting)return;
  voiceQuestionState.starting=true;voiceQuestionState.error=null;
  const inheritedPause=await stopReadAloud(false);
  voiceQuestionState.resumeCaptions=inheritedPause||!!window.LectureLensAudio?.isListening?.();
  voiceQuestionControls();voiceQuestionStatus('Preparing microphone…');
  try{
    if(voiceQuestionState.resumeCaptions)await window.LectureLensAudio.stop();
    const recognition=new Recognition();
    recognition.lang=voiceLanguages[$('captionLanguage')?.value||$('language').value]||'en-US';
    recognition.continuous=true;recognition.interimResults=true;recognition.maxAlternatives=1;
    voiceQuestionState.base=$('question').value.trim();
    recognition.onresult=event=>{
      const final=[],interim=[];
      for(let index=0;index<event.results.length;index++){const result=event.results[index];const text=result[0]?.transcript?.trim();if(text)(result.isFinal?final:interim).push(text)}
      $('question').value=[voiceQuestionState.base,...final,...interim].filter(Boolean).join(' ');
      voiceQuestionStatus(interim.length?'Listening · words may still change':'Listening · tap the mic to finish');
    };
    recognition.onerror=event=>{
      if(event.error==='no-speech')return;
      voiceQuestionState.error=event.error==='not-allowed'||event.error==='service-not-allowed'?'Microphone blocked. Allow microphone access in Chrome and try again.':
        event.error==='network'?'Speech recognition is unavailable right now. Check your connection and try again.':
        `Voice input failed (${event.error}). Try again or type your question.`;
      voiceQuestionStatus(voiceQuestionState.error,true);
    };
    recognition.onend=async()=>{
      if(voiceQuestionState.recognition!==recognition)return;
      voiceQuestionState.recognition=null;voiceQuestionState.stopping=false;
      if(!voiceQuestionState.error)voiceQuestionStatus($('question').value.trim()?'Question ready · review it, then press Ask':'No speech heard · tap the mic to try again');
      if(voiceQuestionState.resumeCaptions&&state.stream){
        voiceQuestionState.starting=true;voiceQuestionControls();
        try{await window.LectureLensAudio?.start?.()}catch(error){console.warn('Could not resume captions:',error)}
        voiceQuestionState.starting=false;
      }
      voiceQuestionState.resumeCaptions=false;
      voiceQuestionControls();
      $('question').focus();
    };
    recognition.start();
    voiceQuestionState.recognition=recognition;voiceQuestionState.starting=false;
    if(state.selected&&!state.pinned){state.pinned=true;showMedia()}
    voiceQuestionControls(true);voiceQuestionStatus('Listening · tap the mic to finish');
  }catch(error){
    voiceQuestionState.starting=false;voiceQuestionControls();
    voiceQuestionStatus(error.name==='NotAllowedError'?'Microphone blocked. Allow microphone access in Chrome and try again.':`Could not start voice input: ${error.message}`,true);
    if(voiceQuestionState.resumeCaptions&&state.stream)await window.LectureLensAudio?.start?.();
    voiceQuestionState.resumeCaptions=false;
  }
}
async function ask(question,snapshotId=null,regionOverride=undefined,deckIdsOverride=undefined,frameIdsOverride=undefined){if(state.busy)return;
  if(!state.activeChatId){try{await createChat(state.activeClassId,{preserveVisualContext:true})}catch(e){status(e.message,true);return}}
  const chatId=state.activeChatId;
  const deckIds=deckIdsOverride===undefined?selectedDeckIds():deckIdsOverride;
  if(deckIds.length>10){showQuestionState(question,'Choose up to 10 files for one question.',true);return}
  state.busy=true;document.body.classList.add('chat-active');voiceQuestionControls();$('askButton').firstChild.textContent='Thinking… ';
  document.querySelectorAll('.preset').forEach(button=>button.disabled=true);
  const frameIds=frameIdsOverride===undefined?(state.multiFrame?state.selectedFrames.map(item=>item.snapshot_id):null):frameIdsOverride;
  const shot=snapshotId?{snapshot_id:snapshotId}:state.multiFrame?state.selectedFrames[0]:state.selected||null;
  const region=shot?(regionOverride===undefined?state.region:regionOverride):null;
  const answerLanguage=$('language').value;
  const askedAtMs=Math.max(0,Date.now()+state.serverClockOffsetMs-(state.sessionStartedEpochMs||Date.now()));
  const root=$('conversation');if(!state.messages.length)root.replaceChildren();
  const pending=showQuestionState(question,frameIds?.length>1?`Looking at ${frameIds.length} selected frames${deckIds.length?' and files':''}…`:shot?(deckIds.length?'Looking at the selected capture and files…':'Looking at the selected capture…'):deckIds.length?'Reading selected files and transcript…':'Listening to the recent transcript…');
  try{const message=await api(`/api/sessions/${state.sessionId}/ask`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,language:answerLanguage,snapshot_id:shot?.snapshot_id||null,snapshot_ids:frameIds,region,asked_at_ms:askedAtMs,chat_id:chatId,selected_deck_ids:deckIds})});state.messages.push(message);state.allMessages.push(message);
    const chat=state.chats.find(item=>item.chat_id===chatId);if(chat&&chat.title==='New chat')chat.title=question.slice(0,64);
    renderChats();renderMessages();$('question').value='';voiceQuestionStatus('')}
  catch(e){pending.className='error';pending.textContent=e.message;
    const retry=document.createElement('button');retry.type='button';retry.className='secondary-button retry-button';retry.textContent='Retry question ↻';
    retry.onclick=()=>{pending.parentElement?.remove();ask(question,shot?.snapshot_id||null,region,deckIds,frameIds)};
    pending.parentElement.append(retry);pending.parentElement.classList.add('failed-entry');
  }
  finally{state.busy=false;voiceQuestionControls(!!voiceQuestionState.recognition);$('askButton').firstChild.textContent='Ask ';document.querySelectorAll('.preset').forEach(button=>button.disabled=false)}
}
async function init(){const params=new URLSearchParams(location.search);state.sessionId=params.get('session');
  applyTheme(document.documentElement.dataset.theme==='dark'?'dark':'light');
  setTranscriptCollapsed(localStorage.getItem('lecturelens-transcript-collapsed')==='true');
  $('transcriptToggle').onclick=()=>setTranscriptCollapsed(!document.querySelector('.audio-card').classList.contains('transcript-collapsed'));
  $('themeToggle').onclick=()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';applyTheme(theme);localStorage.setItem('lecturelens-theme',theme)};
  try{if(state.sessionId)await refresh();else{const result=await api('/api/sessions',{method:'POST'});state.sessionId=result.session_id;history.replaceState({},'',`?session=${state.sessionId}`);await refresh()}
    if(!state.chats.length)await createChat();
    else if(state.messages.length)showMessageSnapshot(state.messages[state.messages.length-1]);
    $('sessionStatus').textContent='Session ready'}
  catch(e){$('sessionStatus').textContent=e.message;return}
  $('startCamera').onclick=startCombined;$('stopCamera').onclick=stopCombined;
  $('camera').onchange=switchCamera;
  await refreshCameras();
  navigator.mediaDevices?.addEventListener?.('devicechange',refreshCameras);
  $('connectPhone').onclick=openPhoneJoin;
  $('copyPhoneLink').onclick=async()=>{await navigator.clipboard.writeText($('phoneLink').href);$('copyPhoneLink').firstChild.textContent='Copied '};
  $('uploadSlides').onclick=()=>{$('uploadMenu').open=false;$('slideFiles').click()};
  $('slideFiles').onchange=()=>uploadSlides([...$('slideFiles').files]);
  $('uploadDeck').onclick=()=>{$('uploadMenu').open=false;$('slideFile').click()};
  $('slideFile').onchange=()=>uploadDeck([...$('slideFile').files]);
  document.addEventListener('click',event=>{if($('uploadMenu').open&&!$('uploadMenu').contains(event.target))$('uploadMenu').open=false});
  window.addEventListener('lecturelens:transcript',()=>{if(state.stream&&Date.now()-state.lastCaptureAt>=2000)void capture('caption')});
  window.addEventListener('lecturelens:translation-language',event=>{const language=event.detail?.language;if(language&&[...$('language').options].some(option=>option.value===language))$('language').value=language});
  $('useDeck').onchange=()=>{localStorage.setItem(deckKey('use-deck'),String($('useDeck').checked));state.deckError=null;renderDeckStatus()};
  $('sidebarToggle').onclick=()=>{
    const collapsed=document.body.classList.toggle('sidebar-collapsed');
    $('sidebarToggle').setAttribute('aria-expanded',String(!collapsed));
    $('sidebarToggle').setAttribute('aria-label',collapsed?'Expand chat sidebar':'Collapse chat sidebar');
    $('sidebarToggle').title=collapsed?'Expand sidebar':'Collapse sidebar';
    localStorage.setItem('lecturelens-sidebar-collapsed',String(collapsed));
  };
  const savedSidebar=localStorage.getItem('lecturelens-sidebar-collapsed');
  if(savedSidebar==='true'||(savedSidebar===null&&matchMedia('(max-width: 950px)').matches))$('sidebarToggle').click();
  $('cancelDeleteChat').onclick=()=>$('deleteChatDialog').close();
  $('confirmDeleteChat').onclick=deleteChat;
  $('deleteChatDialog').onclose=()=>{state.deletingChatId=null};
  $('addClass').onclick=()=>{$('classForm').hidden=false;$('addClass').hidden=true;$('className').focus()};
  $('cancelClass').onclick=()=>{$('classForm').hidden=true;$('addClass').hidden=false;$('classError').hidden=true;$('className').value=''};
  $('classForm').onsubmit=async event=>{event.preventDefault();$('classError').hidden=true;
    try{const lectureClass=await api(`/api/sessions/${state.sessionId}/classes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('className').value.trim()})});
      state.classes.push(lectureClass);openClass(lectureClass.class_id);$('cancelClass').click()}
    catch(error){$('classError').textContent=error.message;$('classError').hidden=false}
  };
  $('copyLink').onclick=async()=>{await navigator.clipboard.writeText(location.href);$('copyLink').textContent='Copied ✓';setTimeout(()=>$('copyLink').textContent='Copy link ↗',1800)};
  $('selectRegion').onclick=()=>{state.selecting=!state.selecting;if(state.selecting)state.pinned=true;$('stage').classList.toggle('selecting',state.selecting);$('selectRegion').textContent=state.selecting?'Drag to select':'Select region';showMedia()};
  $('multiFrameToggle').onclick=startFrameSelection;
  $('useLatest').onclick=()=>{const latest=chatSnapshots()[0];if(latest)selectSnapshot(latest)};
  $('clearRegion').onclick=()=>{state.region=null;$('clearRegion').hidden=true;showMedia()};
  $('stage').onclick=()=>{if(state.suppressStageClick){state.suppressStageClick=false;return}openImageViewer()};
  $('stage').onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&!state.selecting){e.preventDefault();openImageViewer()}};
  $('expandImage').onclick=e=>{e.stopPropagation();openImageViewer()};
  $('viewerClose').onclick=()=>$('imageViewer').close();
  $('sourceViewerClose').onclick=()=>$('sourceViewer').close();
  $('sourceViewer').onclick=event=>{if(event.target===$('sourceViewer'))$('sourceViewer').close()};
  $('viewerZoomIn').onclick=()=>setViewerZoom(state.viewerZoom+.5);
  $('viewerZoomOut').onclick=()=>setViewerZoom(state.viewerZoom-.5);
  $('viewerImage').onload=layoutViewerImage;
  $('viewerSelectRegion').onclick=()=>setViewerSelecting(!state.viewerSelecting);
  $('viewerClearRegion').onclick=()=>{state.region=null;hideRegionDetail();drawViewerSelection();$('viewerClearRegion').hidden=true;$('clearRegion').hidden=true;showMedia()};
  $('imageViewer').onclose=()=>{viewerDragStart=null;regionPreviewRequest++;setViewerSelecting(false)};
  $('imageViewer').onclick=e=>{if(e.target===$('imageViewer'))$('imageViewer').close()};
  $('viewerImageFrame').addEventListener('pointerdown',e=>{
    if(!state.viewerSelecting||!state.selected)return;
    e.preventDefault();viewerDragStart=viewerPoint(e);
    $('viewerImageFrame').setPointerCapture(e.pointerId);
    drawViewerSelection({x:viewerDragStart.x,y:viewerDragStart.y,width:0,height:0});
  });
  $('viewerImageFrame').addEventListener('pointermove',e=>{
    if(!viewerDragStart)return;
    const p=viewerPoint(e);
    drawViewerSelection({x:Math.min(p.x,viewerDragStart.x),y:Math.min(p.y,viewerDragStart.y),width:Math.abs(p.x-viewerDragStart.x),height:Math.abs(p.y-viewerDragStart.y)});
  });
  $('viewerImageFrame').addEventListener('pointerup',e=>{
    if(!viewerDragStart)return;
    const p=viewerPoint(e);
    const region={x:Math.min(p.x,viewerDragStart.x),y:Math.min(p.y,viewerDragStart.y),width:Math.abs(p.x-viewerDragStart.x),height:Math.abs(p.y-viewerDragStart.y)};
    viewerDragStart=null;
    if(region.width<.01||region.height<.01){drawViewerSelection();return}
    state.region=region;state.pinned=true;setViewerSelecting(false);
    $('viewerClearRegion').hidden=false;$('clearRegion').hidden=false;
    drawViewerSelection();showMedia();
  });
  $('viewerImageFrame').addEventListener('pointercancel',()=>{viewerDragStart=null;drawViewerSelection()});
  $('stage').addEventListener('pointerdown',e=>{if(!state.selecting||!state.selected)return;dragStart=imagePoint(e);$('stage').setPointerCapture(e.pointerId)});
  $('stage').addEventListener('pointermove',e=>{if(!dragStart)return;const p=imagePoint(e);state.region={x:Math.min(p.x,dragStart.x),y:Math.min(p.y,dragStart.y),width:Math.abs(p.x-dragStart.x),height:Math.abs(p.y-dragStart.y)};drawRegion()});
  $('stage').addEventListener('pointerup',e=>{if(!dragStart)return;dragStart=null;state.suppressStageClick=true;if(state.region.width<.01||state.region.height<.01){state.region=null;drawRegion();return}state.selecting=false;$('stage').classList.remove('selecting');$('selectRegion').textContent='Select region';$('clearRegion').hidden=false;showMedia()});
  window.addEventListener('resize',()=>{drawRegion();if($('imageViewer').open)layoutViewerImage()});
  new ResizeObserver(drawRegion).observe($('stage'));
  document.querySelectorAll('.preset').forEach(button=>button.onclick=()=>{
    if(button.textContent.includes('I missed that')&&state.messages.length){const last=state.messages[state.messages.length-1];
      const shot=state.snapshots.find(s=>s.snapshot_id===last.snapshot_id);showMessageSnapshot(last);
      const root=$('conversation');root.querySelector('.recap-entry')?.remove();
      const recap=document.createElement('article');recap.className='entry recap-entry';
      const heading=document.createElement('div');heading.className='entry-question';heading.textContent='I missed that';
      const answer=document.createElement('div');answer.className='entry-answer';renderRichText(answer,last.result.answer);
      const source=document.createElement('div');source.className='entry-meta';
      const image=document.createElement('img');if(last.snapshot_id){image.src=`/api/snapshots/${last.snapshot_id}/image`;image.alt='Snapshot for recalled explanation'}
      const label=document.createElement('span');label.textContent=`Recalled explanation · ${shot?elapsed(shot.captured_at_ms):'audio transcript'}${last.region?' · R1':''}`;
      if(last.snapshot_id)source.append(image);source.append(label);recap.append(heading,answer,source);root.append(recap);root.scrollTop=root.scrollHeight;return}
    ask(button.dataset.question)
  });
  $('askForm').onsubmit=e=>{e.preventDefault();const q=$('question').value.trim();if(q)ask(q)};
  $('voiceQuestion').onclick=()=>void startVoiceQuestion();
  setInterval(refresh,5000);
}
init();
