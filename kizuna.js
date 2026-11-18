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
/* ---- language: the server marks Arabic pages <html lang="ar" dir="rtl">; kizuna-ar.js holds the translations */
const LANG=document.documentElement.lang==='ar'?'ar':'en',AR=window.KIZUNA_AR,LOCALE=LANG==='ar'?'ar-u-nu-latn':[];
function tr(core){
  if(AR.exact[core]!==undefined)return AR.exact[core];
  for(const [re,rep] of AR.rules){const m=core.match(re);if(m)return typeof rep==='function'?rep(m,t):core.replace(re,rep)}
  if(core.includes(' · ')){const parts=core.split(' · '),out=parts.map(p=>tr(p.trim())??p);if(out.some((x,i)=>x!==parts[i]))return out.join(' · ')}
  const sents=core.match(/[^.!?…]+[.!?…]+|[^.!?…]+$/g);
  if(sents&&sents.length>1){const out=sents.map(s=>{s=s.trim();return tr(s)??s});if(out.some((x,i)=>x!==sents[i].trim()))return out.join(' ')}
  const p=core.match(/^(.*?)([.:…]+)$/);if(p&&AR.exact[p[1]]!==undefined)return AR.exact[p[1]]+p[2];
  return null}
/** Arabic for a UI string when the interface is Arabic; anything unknown (titles, names) is returned unchanged. */
function t(s){
  if(LANG!=='ar'||!AR||s==null)return s;
  const str=String(s),m=str.match(/^(\s*)([\s\S]*?)(\s*)$/);
  if(!m[2]||!/[A-Za-z]/.test(m[2]))return str;
  const out=tr(m[2].replace(/\s+/g,' '));return out==null?str:m[1]+out+m[3]}
// never translated: titles, release and file names, the log
const SKIP='bdi,pre,code,script,style,textarea,[translate=no],.notr,.poster strong,.suggestion strong,#title,#description,'+
  '.release strong,.release p,.release small,select#episode,.episode-check>span:first-of-type,'+
  'select#library,select#library_movie,select#library_series,select#library_anime,select#language,select#subtitle_language';   // your library names, language names
const ATTRS=['placeholder','aria-label','title'];
function translateTree(root){
  if(LANG!=='ar'||!AR||!root)return;
  if(root.nodeType===3){if(!root.parentElement?.closest(SKIP)){const v=t(root.data);if(v!==root.data)root.data=v}return}
  if(root.nodeType!==1||root.closest(SKIP))return;
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:n=>
    n.nodeType===1&&n.matches(SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
  const visit=n=>{if(n.nodeType===3){const v=t(n.data);if(v!==n.data)n.data=v}
    else for(const a of ATTRS){const v=n.getAttribute(a);if(v){const x=t(v);if(x!==v)n.setAttribute(a,x)}}};
  visit(root);for(let n=walker.nextNode();n;n=walker.nextNode())visit(n)}
if(LANG==='ar'&&AR){
  const start=()=>{translateTree(document.body);document.title=t(document.title);
    new MutationObserver(list=>{for(const m of list){
      if(m.type==='characterData')translateTree(m.target);
      else if(m.type==='attributes')translateTree(m.target.nodeType===1&&!m.target.closest(SKIP)?m.target:null);
      else m.addedNodes.forEach(translateTree)}}).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:ATTRS})};
  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',start):start()}

const NAV=[['now','/','Now'],['discover','/stremio','Discover'],['weekly','/weekly','Weekly'],['news','/news','News'],['settings','/settings','Settings']];
let prefs={clock:'auto'};
const ready=api('settings').then(d=>{prefs=d.settings;
  if(prefs.setup_done===false&&location.pathname!=='/setup'){location.replace('/setup');return d}   // first run: the setup wizard
  document.dispatchEvent(new CustomEvent('kizuna:settings',{detail:d}));return d}).catch(()=>null);
const hour12=()=>prefs.clock==='12h'?true:prefs.clock==='24h'?false:undefined;
function time(ts,opts={}){return new Date(ts*1000).toLocaleString(LOCALE,{hour:'2-digit',minute:'2-digit',hour12:hour12(),...opts})}
function day(ts){const d=new Date(ts*1000),n=new Date(),tm=new Date(n);tm.setDate(n.getDate()+1);
  const label=d.toDateString()===n.toDateString()?t('Today'):d.toDateString()===tm.toDateString()?t('Tomorrow'):d.toLocaleDateString(LOCALE,{weekday:'short',day:'numeric',month:'short'});
  return label+(LANG==='ar'?'، ':', ')+d.toLocaleTimeString(LOCALE,{hour:'2-digit',minute:'2-digit',hour12:hour12()})}
function ago(s){s=Math.max(0,s);return t(s<90?'just now':s<3600?Math.round(s/60)+' min ago':s<86400?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago')}

function shell(page){
  const top=document.querySelector('.k-top');if(!top)return;
  top.innerHTML=`<div class="k-top-in"><a class="k-brand" href="/" aria-label="Kizuna home"><span class="k-seal" aria-hidden="true">絆</span><span class="k-word">Kizuna</span></a>
  <nav class="k-nav" aria-label="${t('Main')}">${NAV.map(([id,href,label])=>`<a href="${href}"${id===page?' aria-current="page"':''}>${t(label)}</a>`).join('')}</nav>
  <a class="k-activity" id="kActivity" href="/"><span class="k-state" data-state="idle"></span><span>Checking…</span></a></div>`;
  activity();setInterval(activity,4000);
}
async function activity(){
  const box=document.getElementById('kActivity');if(!box)return;
  let st=null,jb=null;
  try{[st,jb]=await Promise.all([fetch('/api/status?light=1',{cache:'no-store'}).then(r=>r.json()),api('job').catch(()=>null)])}
  catch{return set('failed','Kizuna is not responding','/')}
  const job=jb&&jb.job,batch=jb&&jb.batch;
  if(job&&job.status==='running'&&job.phase!=='processing')
    return set(job.paused?'paused':'downloading',`<b>${t(job.paused?'Paused':'Downloading')}</b> <bdi>${esc(job.name)}</bdi>${job.pct!=null?` · ${Math.round(job.pct)}%`:''}`,'/stremio',!job.paused);
  if(batch&&['running','stopping'].includes(batch.status)){const c=batch.counts||{};
    return set('downloading',`<b>${t('Season download')}</b> <bdi>${esc(batch.name)}</bdi> · ${(c.done||0)+(c.existing||0)} of ${batch.total||'?'}`,'/stremio',true)}
  if(st.job)return set('processing',`<b>${t('Subtitles')}</b> <bdi>${esc(st.job.name)}</bdi> · ${Math.round(st.job.pct||0)}%`,'/',true);
  if(job&&job.status==='running')return set('processing',`<b>${t('Subtitles')}</b> <bdi>${esc(job.name)}</bdi>`,'/',true);
  const n=(st.attention||[]).length;
  if(n)return set('failed',`<b>${n}</b> ${t(n===1?'needs attention':'need attention')}`,'/#attention');
  const w=(st.pending||[]).length;
  if(w)return set('waiting',`<b>${w}</b> ${t('waiting for subtitles')}`,'/#pending');
  set(st.tone==='bad'?'failed':'ready',st.tone==='bad'?`<b>${t('Agent stopped')}</b>`:t('All caught up'),'/');
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
window.Kizuna={api,esc,shell,time,day,ago,ready,t,lang:LANG,get prefs(){return prefs}};
})();
