/* Kizuna app shell: wordmark, navigation, a live activity indicator shared by every page, artwork fallbacks and
   time formatting that follows the Settings clock preference. Pages call Kizuna.shell('now'|'discover'|'weekly'|'settings'). */
(()=>{
const token=document.querySelector('meta[name="k-token"]')?.content||'';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,data){
  const r=await fetch('/api/stremio/'+path,{method:data===undefined?'GET':'POST',cache:'no-store',
    headers:{'X-MyAnimeWatcher':token,'Content-Type':'application/json'},...(data===undefined?{}:{body:JSON.stringify(data)})});
  let d;try{d=await r.json()}catch{throw new Error('Kizuna restarted. Reload this page.')}
  if(!r.ok||d.error)throw new Error(d.error||'The request failed. Try again.');return d}
const NAV=[['now','/','Now'],['discover','/stremio','Discover'],['weekly','/weekly','Weekly'],['settings','/settings','Settings']];
let prefs={clock:'auto'};
const ready=api('settings').then(d=>{prefs=d.settings;document.dispatchEvent(new CustomEvent('kizuna:settings',{detail:d}));return d}).catch(()=>null);
const hour12=()=>prefs.clock==='12h'?true:prefs.clock==='24h'?false:undefined;
function time(ts,opts={}){return new Date(ts*1000).toLocaleString([],{hour:'2-digit',minute:'2-digit',hour12:hour12(),...opts})}
function day(ts){const d=new Date(ts*1000),n=new Date(),t=new Date(n);t.setDate(n.getDate()+1);
  const label=d.toDateString()===n.toDateString()?'Today':d.toDateString()===t.toDateString()?'Tomorrow':d.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short'});
  return label+', '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:hour12()})}
function ago(s){s=Math.max(0,s);return s<90?'just now':s<3600?Math.round(s/60)+' min ago':s<86400?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago'}

function shell(page){
  const top=document.querySelector('.k-top');if(!top)return;
  top.innerHTML=`<div class="k-top-in"><a class="k-brand" href="/" aria-label="Kizuna home"><span class="k-seal" aria-hidden="true">絆</span><span class="k-word">Kizuna</span></a>
  <nav class="k-nav" aria-label="Main">${NAV.map(([id,href,label])=>`<a href="${href}"${id===page?' aria-current="page"':''}>${label}</a>`).join('')}</nav>
  <a class="k-activity" id="kActivity" href="/" aria-live="polite"><span class="k-state" data-state="idle"></span><span>Checking…</span></a></div>`;
  activity();setInterval(activity,4000);
}
async function activity(){
  const box=document.getElementById('kActivity');if(!box)return;
  let st=null,jb=null;
  try{[st,jb]=await Promise.all([fetch('/api/status',{cache:'no-store'}).then(r=>r.json()),api('job').catch(()=>null)])}
  catch{return set('failed','Kizuna is not responding','/')}
  const job=jb&&jb.job,batch=jb&&jb.batch;
  if(job&&job.status==='running'&&job.phase!=='processing')
    return set(job.paused?'paused':'downloading',`<b>${job.paused?'Paused':'Downloading'}</b> ${esc(job.name)}${job.pct!=null?` · ${Math.round(job.pct)}%`:''}`,'/stremio',!job.paused);
  if(batch&&['running','stopping'].includes(batch.status)){const c=batch.counts||{};
    return set('downloading',`<b>Season download</b> ${esc(batch.name)} · ${(c.done||0)+(c.existing||0)} of ${batch.total||'?'}`,'/stremio',true)}
  if(st.job)return set('processing',`<b>Arabic</b> ${esc(st.job.name)} · ${Math.round(st.job.pct||0)}%`,'/',true);
  if(job&&job.status==='running')return set('processing',`<b>Arabic</b> ${esc(job.name)}`,'/',true);
  const n=(st.attention||[]).length;
  if(n)return set('failed',`<b>${n}</b> need${n===1?'s':''} attention`,'/#attention');
  const w=(st.pending||[]).length;
  if(w)return set('waiting',`<b>${w}</b> waiting for Arabic`,'/#pending');
  set(st.tone==='bad'?'failed':'ready',st.tone==='bad'?'<b>Agent stopped</b>':'All caught up','/');
  function set(state,html,href,live){box.href=href;const dot=box.firstElementChild;dot.dataset.state=state;dot.classList.toggle('live',!!live);
    if(box.lastElementChild.innerHTML!==html)box.lastElementChild.innerHTML=html}
}
/* artwork: fade in when loaded; a broken image becomes a calm placeholder with the title's initial */
document.addEventListener('load',e=>{if(e.target.tagName==='IMG')e.target.classList.add('in')},true);
document.addEventListener('error',e=>{const img=e.target;if(img.tagName!=='IMG')return;
  const art=img.closest('.k-art,.poster,.suggestion,.art');if(!art)return;
  const title=(art.closest('[data-title]')?.dataset.title||art.textContent||'').trim();
  const fb=document.createElement('span');fb.className=img.closest('.k-art')?'k-fallback':'placeholder';fb.textContent=(title[0]||'絆').toUpperCase();
  fb.setAttribute('aria-hidden','true');img.replaceWith(fb)},true);
window.Kizuna={api,esc,shell,time,day,ago,ready,get prefs(){return prefs}};
})();
