const $ = id => document.getElementById(id);
const state = {sessionId:null,sessionStartedEpochMs:null,serverClockOffsetMs:0,stream:null,timer:null,snapshots:[],selected:null,selectedFrames:[],multiFrame:false,region:null,selecting:false,pinned:false,busy:false,captureInFlight:false,lastCaptureAt:0,messages:[],allMessages:[],chats:[],classes:[],decks:[],uploadingDeck:false,deckUploadProgress:'',deckError:null,activeChatId:null,activeClassId:null,chatSelectionReady:false,deletingChatId:null,suppressStageClick:false,viewerZoom:1,viewerSelecting:false};

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
  $('deckPickerCount').textContent=toggle.checked?`${selected.size}/${decks.length} · text only`:'Off · text only';
  for(const deck of decks){
    const label=document.createElement('label');label.className='deck-choice';label.title=deck.filename;
    const input=document.createElement('input');input.type='checkbox';input.checked=!disabledDeckIds().includes(deck.deck_id);input.disabled=!toggle.checked;input.setAttribute('aria-label',`Use ${deck.filename}`);
    input.onchange=()=>{const disabled=new Set(disabledDeckIds());if(input.checked)disabled.delete(deck.deck_id);else disabled.add(deck.deck_id);
      localStorage.setItem(deckKey('disabled-decks'),JSON.stringify([...disabled]));state.deckError=null;renderDeckStatus()};
    const name=document.createElement('span');name.className='deck-choice-name';name.textContent=deck.filename;
    const count=document.createElement('span');count.className='deck-choice-count';count.textContent=`${deck.slide_count} slide${deck.slide_count===1?'':'s'}`;
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
    const file=files[index];state.deckUploadProgress=`Reading PPT ${index+1} of ${files.length}: ${file.name}`;renderDeckStatus();
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
  $('snapshot').classList.toggle('active',!!state.selected);
  $('stageEmpty').hidden=!!state.stream||!!state.selected;
  $('stageBadge').textContent=live?'● LIVE CAMERA':state.selected?`SNAPSHOT · ${elapsed(state.selected.captured_at_ms)}`:state.stream?'● LIVE CAMERA':'Waiting for camera';
  $('selectRegion').disabled=!state.snapshots.length;
  $('expandImage').hidden=!state.selected;
  $('useLatest').hidden=!state.pinned;
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
function renderMoments(){
  const target=$('moments');target.replaceChildren();$('momentCount').textContent=`${state.snapshots.length} recent snapshot${state.snapshots.length===1?'':'s'}`;
  $('multiFrameToggle').disabled=!state.snapshots.length;
  $('multiFrameToggle').setAttribute('aria-pressed',String(state.multiFrame));
  $('multiFrameToggle').textContent=state.multiFrame?'Clear frames':'Select frames';
  if(!state.snapshots.length){const p=document.createElement('div');p.className='moment-placeholder';p.textContent='Snapshots will appear here every 4 seconds.';target.append(p);return}
  const visible=[...state.selectedFrames,...state.snapshots.filter(shot=>!state.selectedFrames.some(item=>item.snapshot_id===shot.snapshot_id))];
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
  if(!state.selected)return;
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
  if(state.snapshots.length)selectSnapshot(state.snapshots[0]);
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
      else{state.messages=[];$('question').value='';renderChats();renderMessages();if(state.snapshots.length)selectSnapshot(state.snapshots[0])}
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
function switchChat(chatId){
  if(state.busy||chatId===state.activeChatId)return;
  state.activeChatId=chatId;localStorage.setItem(`lecturelens-chat:${state.sessionId}`,chatId);
  const chat=state.chats.find(item=>item.chat_id===chatId);if(chat){state.activeClassId=chat.class_id;state.deckError=null;localStorage.setItem(`lecturelens-class:${state.sessionId}`,chat.class_id)}
  state.messages=state.allMessages.filter(message=>message.chat_id===chatId);
  $('question').value='';renderChats();renderMessages();renderDeckStatus();
  if(state.messages.length)showMessageSnapshot(state.messages[state.messages.length-1]);
  else if(state.snapshots.length)selectSnapshot(state.snapshots[0]);
}
async function createChat(classId=state.activeClassId){
  if(state.busy)return;
  const chat=await api(`/api/sessions/${state.sessionId}/chats`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({class_id:classId})});
  state.chats.unshift(chat);state.activeChatId=chat.chat_id;
  selectClass(chat.class_id);
  localStorage.setItem(`lecturelens-chat:${state.sessionId}`,chat.chat_id);
  state.messages=[];$('question').value='';
  if(state.snapshots.length)selectSnapshot(state.snapshots[0]);
  renderChats();renderMessages();return chat;
}
async function refresh(){
  if(!state.sessionId)return;
  try{const requestedAt=Date.now(),data=await api(`/api/sessions/${state.sessionId}`);
    state.sessionStartedEpochMs=data.started_epoch_ms;
    state.serverClockOffsetMs=data.server_epoch_ms-Math.round((requestedAt+Date.now())/2);
    window.LectureLensSession={id:state.sessionId,startedEpochMs:state.sessionStartedEpochMs};
    window.dispatchEvent(new CustomEvent('lecturelens:session-ready',{detail:window.LectureLensSession}));
    state.snapshots=data.snapshots;state.decks=data.decks||[];
    if(!state.selected&&state.snapshots.length)selectSnapshot(state.snapshots[0]);
    else if(state.selected&&state.stream&&!state.pinned&&!state.region&&!state.selecting&&state.selected.snapshot_id!==state.snapshots[0]?.snapshot_id)selectSnapshot(state.snapshots[0]);
    renderMoments();
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
    const messages=data.messages.filter(message=>message.chat_id===state.activeChatId);
    if(messages.length!==state.messages.length||messages.some((message,index)=>message.message_id!==state.messages[index]?.message_id)){
      state.messages=messages;renderMessages();
    }
    renderChats();renderDeckStatus();
  }catch(e){status(e.message,true)}
}
async function capture(reason='timer'){
  if(!state.stream||!$('video').videoWidth||state.captureInFlight)return;
  state.captureInFlight=true;state.lastCaptureAt=Date.now();
  const canvas=$('captureCanvas'),video=$('video');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.82));
  if(!blob){state.captureInFlight=false;return}const form=new FormData();form.append('image',blob,'snapshot.jpg');form.append('source','camera');form.append('capture_reason',reason);
  try{const shot=await api(`/api/sessions/${state.sessionId}/snapshots`,{method:'POST',body:form});
    state.snapshots.unshift(shot);state.snapshots=state.snapshots.slice(0,12);
    if(!state.pinned&&!state.region&&!state.selecting&&!state.multiFrame)selectSnapshot(shot);else renderMoments();
    status(`${reason==='caption'?'Caption-linked snapshot':'Capturing'} · ${elapsed(shot.captured_at_ms)}`)
  }catch(e){status(e.message,true)}finally{state.captureInFlight=false}
}
function stop(){clearInterval(state.timer);state.timer=null;state.stream?.getTracks().forEach(t=>{t.onended=null;t.stop()});state.stream=null;$('startCamera').disabled=false;$('stopCamera').disabled=true;status('Camera off');showMedia()}
async function start(){
  if(!window.isSecureContext){status(`Camera blocked: ${location.origin} is not a secure context. Open localhost on this device or use HTTPS.`,true);return}
  if(!navigator.mediaDevices?.getUserMedia){status(`Camera API unavailable at ${location.origin}. Check Chrome camera permissions and whether this page is inside another app.`,true);return}
  $('startCamera').disabled=true;status('Requesting camera permission…');
  try{const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:'environment',width:{ideal:1280},height:{ideal:720}}});
    state.stream=stream;const video=$('video');video.srcObject=stream;await video.play();
    stream.getVideoTracks()[0].onended=stopCombined;$('startCamera').disabled=true;$('stopCamera').disabled=false;
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
  if(state.stream&&window.LectureLensAudio?.isListening?.())status('Audio + video live · snapshots every 4 seconds');
  else if(state.stream)status('Video live · microphone captions are not active',true);
}
function stopCombined(){stop();void window.LectureLensAudio?.stop?.();status('Audio + video capture stopped')}
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
  $('viewerImageFrame').classList.toggle('selecting',selecting);
  $('viewerSelectRegion').textContent=selecting?'Drag over image':'Select region';
  $('viewerSelectRegion').setAttribute('aria-pressed',String(selecting));
}
function openImageViewer(){
  if(!state.selected||state.selecting)return;
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
function renderMessages(){document.body.classList.toggle('chat-active',state.messages.length>0||state.busy);const root=$('conversation');root.replaceChildren();if(!state.messages.length){root.innerHTML='<div class="welcome"><span class="welcome-icon">✳</span><strong>Clarity, one question at a time.</strong><p>Your answers will appear here with the snapshot they used.</p></div>';return}
  for(const message of state.messages){const entry=document.createElement('article');entry.className='entry';const q=document.createElement('div');q.className='entry-question';q.textContent=message.question;
    const a=document.createElement('div');a.className='entry-answer';renderRichText(a,message.result.answer);
    const meta=document.createElement('div');meta.className='entry-meta frame-evidence';
    const evidenceIds=message.snapshot_ids?.length?message.snapshot_ids:(message.snapshot_id?[message.snapshot_id]:[]);
    for(const [index,ident] of evidenceIds.entries()){
      const shot=state.snapshots.find(s=>s.snapshot_id===ident)||state.selectedFrames.find(s=>s.snapshot_id===ident);
      const frame=document.createElement('button');frame.type='button';frame.className='evidence-frame';frame.title=`Open frame F${index+1}`;
      const thumb=document.createElement('img');thumb.src=`/api/snapshots/${ident}/image`;thumb.alt=`Evidence frame F${index+1}`;
      const label=document.createElement('span');label.textContent=`F${index+1}${shot?` · ${elapsed(shot.captured_at_ms)}`:''}${index===0&&message.region?' · R1':''}`;
      frame.append(thumb,label);frame.onclick=()=>showMessageSnapshot(message,ident);meta.append(frame);
    }
    if(!evidenceIds.length){const label=document.createElement('span');label.textContent='Audio transcript context';meta.append(label)}
    const transcriptTimes=message.result.source_timestamps||[];
    if(transcriptTimes.length){const label=document.createElement('span');label.textContent=`Transcript ${transcriptTimes.map(item=>`${elapsed(item.start_ms)}–${elapsed(item.end_ms)}`).join(', ')}`;meta.append(label)}
    entry.append(q,a,meta);
    for(const [title,list] of [['Visible',message.result.visible_evidence],['Slides',message.result.slide_references],['Explanation',message.result.inferred_explanation],['Uncertain',message.result.uncertainties]]){if(Array.isArray(list)&&list.length){const line=document.createElement('div');line.className='evidence';const strong=document.createElement('strong');strong.textContent=title+' · ';const content=document.createElement('span');renderRichText(content,list.join(' · '));line.append(strong,content);entry.append(line)}}root.append(entry)}root.scrollTop=root.scrollHeight;
}
function showQuestionState(question,message,error=false){
  const root=$('conversation');const pending=document.createElement('article');pending.className='entry pending-entry';
  const q=document.createElement('div');q.className='entry-question';q.textContent=question;
  const answer=document.createElement('div');answer.className=error?'error':'entry-answer';answer.textContent=message;
  pending.append(q,answer);root.append(pending);root.scrollTop=root.scrollHeight;
  return answer;
}
async function ask(question,snapshotId=null,regionOverride=undefined,deckIdsOverride=undefined,frameIdsOverride=undefined){if(state.busy)return;
  if(!state.activeChatId){try{await createChat()}catch(e){status(e.message,true);return}}
  const chatId=state.activeChatId;
  const deckIds=deckIdsOverride===undefined?selectedDeckIds():deckIdsOverride;
  if(deckIds.length>10){showQuestionState(question,'Choose up to 10 PPTs for one question.',true);return}
  state.busy=true;document.body.classList.add('chat-active');$('askButton').disabled=true;$('askButton').firstChild.textContent='Thinking… ';
  document.querySelectorAll('.preset').forEach(button=>button.disabled=true);
  const frameIds=frameIdsOverride===undefined?(state.multiFrame?state.selectedFrames.map(item=>item.snapshot_id):null):frameIdsOverride;
  const shot=snapshotId?{snapshot_id:snapshotId}:state.multiFrame?state.selectedFrames[0]:state.selected||state.snapshots[0]||null;
  const region=shot?(regionOverride===undefined?state.region:regionOverride):null;
  const answerLanguage=$('captionLanguage')?.value||$('language').value;
  const askedAtMs=Math.max(0,Date.now()+state.serverClockOffsetMs-(state.sessionStartedEpochMs||Date.now()));
  const root=$('conversation');if(!state.messages.length)root.replaceChildren();
  const pending=showQuestionState(question,frameIds?.length>1?`Looking at ${frameIds.length} selected frames${deckIds.length?' and PPTs':''}…`:shot?(deckIds.length?'Looking at the selected capture and PPTs…':'Looking at the selected capture…'):'Listening to the recent transcript…');
  try{const message=await api(`/api/sessions/${state.sessionId}/ask`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,language:answerLanguage,snapshot_id:shot?.snapshot_id||null,snapshot_ids:frameIds,region,asked_at_ms:askedAtMs,chat_id:chatId,selected_deck_ids:deckIds})});state.messages.push(message);state.allMessages.push(message);
    const chat=state.chats.find(item=>item.chat_id===chatId);if(chat&&chat.title==='New chat')chat.title=question.slice(0,64);
    renderChats();renderMessages();$('question').value=''}
  catch(e){pending.className='error';pending.textContent=e.message;
    const retry=document.createElement('button');retry.type='button';retry.className='secondary-button retry-button';retry.textContent='Retry question ↻';
    retry.onclick=()=>{pending.parentElement?.remove();ask(question,shot?.snapshot_id||null,region,deckIds,frameIds)};
    pending.parentElement.append(retry);pending.parentElement.classList.add('failed-entry');
  }
  finally{state.busy=false;$('askButton').disabled=false;$('askButton').firstChild.textContent='Ask ';document.querySelectorAll('.preset').forEach(button=>button.disabled=false)}
}
async function init(){const params=new URLSearchParams(location.search);state.sessionId=params.get('session');
  applyTheme(document.documentElement.dataset.theme==='dark'?'dark':'light');
  $('themeToggle').onclick=()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';applyTheme(theme);localStorage.setItem('lecturelens-theme',theme)};
  try{if(state.sessionId)await refresh();else{const result=await api('/api/sessions',{method:'POST'});state.sessionId=result.session_id;history.replaceState({},'',`?session=${state.sessionId}`);await refresh()}
    if(!state.chats.length)await createChat();
    else if(state.messages.length)showMessageSnapshot(state.messages[state.messages.length-1]);
    $('sessionStatus').textContent='Session ready'}
  catch(e){$('sessionStatus').textContent=e.message;return}
  $('startCamera').onclick=startCombined;$('stopCamera').onclick=stopCombined;
  $('connectPhone').onclick=openPhoneJoin;
  $('copyPhoneLink').onclick=async()=>{await navigator.clipboard.writeText($('phoneLink').href);$('copyPhoneLink').firstChild.textContent='Copied '};
  $('uploadSlides').onclick=()=>$('slideFiles').click();
  $('slideFiles').onchange=()=>uploadSlides([...$('slideFiles').files]);
  $('uploadDeck').onclick=()=>$('slideFile').click();
  $('slideFile').onchange=()=>uploadDeck([...$('slideFile').files]);
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
  $('useLatest').onclick=()=>{if(state.snapshots.length)selectSnapshot(state.snapshots[0]);};
  $('clearRegion').onclick=()=>{state.region=null;$('clearRegion').hidden=true;showMedia()};
  $('stage').onclick=()=>{if(state.suppressStageClick){state.suppressStageClick=false;return}openImageViewer()};
  $('stage').onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&!state.selecting){e.preventDefault();openImageViewer()}};
  $('expandImage').onclick=e=>{e.stopPropagation();openImageViewer()};
  $('viewerClose').onclick=()=>$('imageViewer').close();
  $('viewerZoomIn').onclick=()=>setViewerZoom(state.viewerZoom+.5);
  $('viewerZoomOut').onclick=()=>setViewerZoom(state.viewerZoom-.5);
  $('viewerImage').onload=layoutViewerImage;
  $('viewerSelectRegion').onclick=()=>setViewerSelecting(!state.viewerSelecting);
  $('viewerClearRegion').onclick=()=>{state.region=null;drawViewerSelection();$('viewerClearRegion').hidden=true;$('clearRegion').hidden=true;showMedia()};
  $('imageViewer').onclose=()=>{viewerDragStart=null;setViewerSelecting(false)};
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
  setInterval(refresh,5000);
}
init();
