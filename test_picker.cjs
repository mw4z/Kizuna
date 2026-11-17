// Exercise the actual picker handlers with a small DOM stub; no browser dependency.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/stremio.html','utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
class Element{
 constructor(){this.children=[];this.attrs={};this.events={};this.dataset={};this.value='';this.hidden=true}
 append(...nodes){this.children.push(...nodes)}
 replaceChildren(...nodes){this.children=[...nodes]}
 setAttribute(key,value){this.attrs[key]=value}
 removeAttribute(key){delete this.attrs[key]}
 addEventListener(key,handler){this.events[key]=handler}
 scrollIntoView(options){this.scrolled=options}
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id)};
const timers=new Map(),requests=[],opened=[],documentEvents={};let timerId=0;
const context={
 $:get,document:{addEventListener:(key,fn)=>documentEvents[key]=fn},URLSearchParams,AbortController,
 setTimeout:fn=>{timers.set(++timerId,fn);return timerId},clearTimeout:id=>timers.delete(id),
 el:(tag,text,cls)=>Object.assign(new Element(),{tag,textContent:text,className:cls}),
 api:(path,data,signal)=>new Promise((resolve,reject)=>requests.push({path,data,signal,resolve,reject})),
 openTitle:(title,row)=>opened.push(title),
};
vm.createContext(context);
vm.runInContext(html.split('// Title suggestions')[1].split('// End title suggestions')[0],context);
const key=key=>{const e={key,prevented:false,preventDefault(){this.prevented=true}};get('query').events.keydown(e);return e};
const type=value=>{get('query').value=value;get('query').events.input({})};
const fire=()=>{assert.equal(timers.size,1);const [id,fn]=[...timers][0];timers.delete(id);return fn()};
(async()=>{
 get('type').value='series';type('c');assert.equal(timers.size,0);
 type('co');type('con');assert.equal(timers.size,1);assert.equal(requests.length,0);
 const old=fire();assert.match(requests[0].path,/q=con/);
 type('cona');assert.equal(requests[0].signal.aborted,true);const current=fire();
 const title={key:'conan',name:'Detective Conan',type:'series',year:'1996'};
 requests[1].resolve({results:[title]});await current;
 requests[0].resolve({results:[{name:'Stale result'}]});await old;
 assert.equal(get('suggestions').children[0].children[0].children[0].textContent,'Detective Conan');
 assert.equal(key('ArrowDown').prevented,true);assert.equal(key('Enter').prevented,true);
 assert.equal(opened[0].key,'conan');assert.equal(get('query').value,'Detective Conan');assert.equal(get('suggestions').hidden,true);
 context.showSuggestions([title,{...title,key:'last'}]);key('ArrowUp');key('Enter');assert.equal(opened[1].key,'last');
 context.showSuggestions([title]);key('Escape');assert.equal(get('suggestions').hidden,true);
 assert.equal(key('Enter').prevented,false);
 type('hunter');const dismissed=fire();documentEvents.pointerdown({target:{closest:()=>null}});
 requests[2].resolve({results:[title]});await dismissed;assert.equal(get('suggestions').hidden,true);
 vm.runInContext(html.match(/^function matchesSource\(s\).*$/m)[0],context);
 get('audioFilter').value='ar';get('resolution').value='1080p';get('hdr').value='any-hdr';
 const release={audio_languages:['ar'],quality:'1080p',dynamic_range:'HDR10'};
 assert.equal(context.matchesSource(release),true);
 assert.equal(context.matchesSource({...release,audio_languages:['ja']}),false);
 assert.equal(context.matchesSource({...release,dynamic_range:'SDR'}),false);
 get('audioFilter').value='und';get('hdr').value='';assert.equal(context.matchesSource({...release,audio_languages:[]}),true);
 assert.equal(context.matchesSource(release),false);
 vm.runInContext(html.match(/^function showSkeletons\(container.*$/m)[0],context);
 const loading=new Element();context.showSkeletons(loading,true);assert.equal(loading.children.length,6);
 assert.equal(loading.attrs['aria-busy'],'true');assert.ok(loading.children.every(c=>c.attrs['aria-hidden']==='true'));
 context.showSkeletons(loading,false);assert.equal(loading.children.length,3);
 assert.ok(!html.includes('id="type"'));assert.match(requests[0].path,/type=all/);
 context.titles=[{name:'A film',type:'movie'},{name:'A show',type:'series'},{name:'An anime',type:'anime'}];
 vm.runInContext(html.match(/^function renderTitles\(\).*$/m)[0],context);context.renderTitles();
 assert.deepEqual(get('results').children.map(n=>n.children[0].textContent),['Movies','Series','Anime']);
 vm.runInContext(html.match(/^function etaLabel\(seconds\).*$/m)[0],context);
 assert.equal(context.etaLabel(60),'About 1 min left');assert.equal(context.etaLabel(3660),'About 1h 1m left');
 assert.equal(context.etaLabel(null),'Estimating time left…');
 assert.ok(html.includes('Back to dashboard')&&html.includes('downloadSeason')&&html.includes('downloadShow'));
 assert.ok(html.indexOf('id="bulkControls"')<html.indexOf('id="sourcesSection"'));
 context.selected={key:'selected-source'};context.jobBusy=false;context.batchBusy=false;context.inspectVersion=0;context.currentType='series';context.episodeChoices=new Set();
 get('season').value='2';
 vm.runInContext(html.match(/^function updateDownloadButtons\(\).*$/m)[0],context);
 vm.runInContext(html.match(/^function resetEpisodeSelection\(\).*$/m)[0],context);
 context.updateDownloadButtons();assert.equal(get('downloadSeason').disabled,false);
 assert.equal(get('downloadSeasonHere').disabled,false);
 assert.equal(get('downloadSeasonHere').textContent,'Download Season 2');
 context.Date=Date;context.currentType='series';
 vm.runInContext(html.match(/^function showJob\(j\).*$/m)[0],context);
 context.showJob({name:'Show',season:1,episode:13,status:'running',stage:'Downloading',pct:49.2,started:Date.now()/1000});
 assert.equal(get('jobName').textContent,'Show · S01E13');
 context.showJob({name:'Show',status:'done',path:'Show - S00E01.mkv',pct:100});
 assert.equal(get('jobName').textContent,'Show · S00E01');
 context.currentType='movie';context.updateDownloadButtons();assert.equal(get('downloadSeasonHere').hidden,true);
 context.showJob({name:'Movie',status:'done',pct:100});assert.equal(get('jobName').textContent,'Movie');
 // Library list refreshes in place (a library added after the page opened must appear, selection kept by name)
 vm.runInContext(html.match(/^function option\(select,value,label\).*$/m)[0],context);
 vm.runInContext(html.match(/^function setLibraries\(list\).*$/m)[0],context);
 vm.runInContext(html.match(/^function destinationNote\(\).*$/m)[0],context);
 context.libraries=[{id:'Anime:0',name:'Anime'},{id:'Movies:1',name:'Movies'},{id:'Weekly Anime:2',name:'Weekly Anime'}];
 get('library').replaceChildren();get('library').selectedOptions=[{textContent:'Weekly Anime'}];
 context.setLibraries([{id:'Anime:0',name:'Anime'},{id:'Movies:1',name:'Movies'},{id:'Series:2',name:'Series'},{id:'Weekly Anime:3',name:'Weekly Anime'}]);
 assert.deepEqual(get('library').children.map(o=>o.textContent),['Anime','Movies','Series','Weekly Anime']);
 assert.equal(get('library').value,'Weekly Anime:3','selection kept by name although its id changed');
 for (const [lib, cleaned] of [['Anime', true], ['Weekly Anime', true], ['Series', false], ['Movies', false]]) {
  get('library').selectedOptions=[{textContent:lib}];context.destinationNote();
  assert.equal(/Recycle Bin/.test(get('destinationNote').textContent), cleaned, lib + ' cleanup note');
 }
 // Cancel only while downloading: the server refuses it once Arabic preparation started
 context.showJob({name:'Show',status:'running',stage:'Downloading',pct:40,started:Date.now()/1000});assert.equal(get('jobCancel').hidden,false);
 context.showJob({name:'Show',status:'running',phase:'processing',stage:'Preparing Arabic',pct:90,started:Date.now()/1000});assert.equal(get('jobCancel').hidden,true);
 context.showJob({name:'Show',status:'running',stage:'Downloading',pct:40,started:Date.now()/1000});
 assert.equal(get('jobPause').hidden,false);assert.equal(get('jobPause').textContent,'❚❚');
 context.showJob({name:'Show',status:'running',paused:true,stage:'Paused',pct:40,started:Date.now()/1000});
 assert.equal(get('jobPause').textContent,'▶');assert.equal(get('jobEta').textContent,'Paused');assert.equal(get('jobCancel').hidden,false);
 vm.runInContext(html.match(/\$\('jobPause'\)\.onclick=.*?\};$/m)[0],context);const beforeP=requests.length;get('jobPause').onclick();
 assert.equal(requests[beforeP].path,'download-resume','paused job resumes');
 context.showJob({name:'Show',status:'running',phase:'processing',stage:'Preparing Arabic',pct:90,started:Date.now()/1000});
 assert.equal(get('jobPause').hidden,true,'no pause during Arabic preparation');
 context.showJob({name:'Show',status:'done',pct:100});assert.equal(get('jobCancel').hidden,true);assert.equal(get('jobPause').hidden,true);
 assert.equal(get('jobDismiss').hidden,false,'finished job can be dismissed');
 context.showJob({name:'Show',status:'failed',stage:'The saved file is incomplete',pct:99});assert.equal(get('jobDismiss').hidden,false,'failed job can be dismissed');
 context.showJob({name:'Show',status:'running',stage:'Downloading',pct:40,started:Date.now()/1000});assert.equal(get('jobDismiss').hidden,true);
 vm.runInContext(html.match(/\$\('jobDismiss'\)\.onclick=.*?\};$/m)[0],context);const before=requests.length;get('jobDismiss').onclick();
 assert.equal(get('job').hidden,true);assert.equal(requests[before].path,'download-dismiss','dismiss is saved on the server');
 // Dismiss keeps the same finished batch hidden across polls; a new or resumed batch shows again
 context.dismissedBatch=false;
 vm.runInContext(html.match(/^function showBatch\(b\).*$/m)[0],context);
 const batch={name:'Show',scope:'show',stage:'Done',status:'done',created:111,total:2,problems:[],
  counts:{pending:0,running:0,done:2,existing:0,unavailable:0,failed:0,waiting:0}};
 context.showBatch(batch);assert.equal(get('batch').hidden,false);
 vm.runInContext(html.match(/\$\('batchDismiss'\)\.onclick=.*?\};$/m)[0],context);const beforeB=requests.length;get('batchDismiss').onclick();
 assert.equal(requests[beforeB].path,'batch-dismiss','batch dismiss is saved on the server');
 assert.equal(get('batch').hidden,true);
 context.showBatch({...batch});assert.equal(get('batch').hidden,true,'same batch must stay dismissed on the next poll');
 context.showBatch({...batch,status:'running'});assert.equal(get('batch').hidden,false,'resumed batch shows again');
 context.showBatch({...batch,created:222});assert.equal(get('batch').hidden,false,'a new batch shows');
 context.currentType='series';
 context.sources=[{key:'inline-release',quality:'1080p',dynamic_range:'SDR',name:'Release',description:'Test',audio_languages:[]}];
 context.selected={key:'inline-release'};
 get('audioFilter').value='';get('resolution').value='';get('hdr').value='';
 vm.runInContext(html.match(/^function renderSources\(\).*$/m)[0],context);context.renderSources();
 assert.ok(get('releases').children[0].children.includes(get('selection')));
 assert.match(html,/\.release>\.selection\{grid-column:1\/-1/);
 assert.equal(get('downloadSeason').textContent,'Download Season 2');
 context.resetEpisodeSelection();assert.equal(get('downloadSeason').disabled,true);
 assert.equal(context.selected,null);assert.match(get('bulkHelp').textContent,/Check tracks/);
 context.selected={key:'old-release'};
 context.message=()=>{};
 context.currentType='series';
 vm.runInContext(html.slice(html.indexOf('async function checkSource(s,b){'),html.indexOf('function destinationNote()')),context);
 const inlineRow=new Element(),checkButton=new Element();checkButton.closest=()=>inlineRow;
 const inspection=context.checkSource({key:'new-release'},checkButton);
 assert.ok(inlineRow.children.includes(get('selectionLoading')));
 assert.equal(context.selected,null);assert.equal(get('downloadSeason').disabled,true);
 const pending=requests.at(-1);assert.equal(pending.path,'inspect');
 pending.reject(Error('unavailable'));await inspection;
 assert.equal(context.selected,null);assert.equal(get('downloadShow').disabled,true);
 context.selected={key:'selected-release'};context.fillerLabels={filler:'Filler',mixed:'Mixed canon/filler'};
 context.videos=[{id:'filler',season:2,episode:1,name:'Filler',released:'2020-01-01'},
 {id:'mixed',season:2,episode:2,name:'Mixed',released:'2020-01-02'},
 {id:'unknown',season:2,episode:3,name:'Unknown'},
 {id:'future',season:2,episode:4,name:'Future',released:'2099-01-01'},
 {id:'other',season:3,episode:1,name:'Other season'}];
 for(const name of ['releasedEpisode','renderEpisodeChecks','chooseEpisodes'])vm.runInContext(html.match(new RegExp('^function '+name+'\\(.*$','m'))[0],context);
 context.chooseEpisodes('all');assert.deepEqual([...context.episodeChoices],[1,2,3]);
 assert.equal(get('episodeChecks').children.length,4);
 assert.equal(get('episodeChecks').children[0].children[2].textContent,'Filler');
 assert.equal(get('episodeChecks').children[3].children[0].disabled,true);
 context.chooseEpisodes('skip');assert.deepEqual([...context.episodeChoices],[2,3]);
 assert.equal(get('downloadSelected').textContent,'Download selected episodes (2)');
 context.chooseEpisodes('clear');assert.equal(context.episodeChoices.size,0);assert.equal(get('downloadSelected').disabled,true);
 context.matchMedia=()=>({matches:false});context.requestAnimationFrame=fn=>fn();
 vm.runInContext(html.match(/^function moveTo\(id\).*$/m)[0],context);
 get('results').hidden=false;context.moveTo('results');assert.equal(get('results').scrolled.behavior,'smooth');
 context.matchMedia=()=>({matches:true});context.moveTo('results');assert.equal(get('results').scrolled.behavior,'auto');
 get('results').hidden=true;get('results').scrolled=null;context.moveTo('results');assert.equal(get('results').scrolled,null);
 context.matchMedia=()=>({matches:false});context.searchVersion=0;
 vm.runInContext(html.slice(html.indexOf("$('searchForm').onsubmit=async"),html.indexOf('function renderTitles()')),context);
 const searchAction=get('searchForm').onsubmit({preventDefault(){}});
 requests.at(-1).resolve({results:[{name:'Result',type:'series'}],failed_addons:0});await searchAction;
 assert.equal(get('results').scrolled.behavior,'smooth');
 vm.runInContext(html.match(/^async function startBatch\(scope\).*$/m)[0],context);
 context.showBatch=()=>{get('batch').hidden=false};context.episodeChoices.add(2);
 const batchAction=context.startBatch('selected');
 assert.equal(requests.at(-1).data.episodes,'[2]');requests.at(-1).resolve({batch:{}});await batchAction;
 assert.equal(get('batch').scrolled.behavior,'smooth');
 get('job').scrolled=null;context.showJob({name:'Movie',status:'done',pct:100});assert.equal(get('job').scrolled,null);
 context.actionMessage=get('message');context.messageHost=new Element();context.messageHost.isConnected=true;
 get('message').hidden=false;
 vm.runInContext(html.match(/^function message\(text.*$/m)[0],context);
 context.message('Rejected placeholder clip',true);
 assert.ok(context.messageHost.children.includes(get('message')));
 assert.equal(get('message').className,'error');assert.equal(get('message').scrolled.behavior,'smooth');
 console.log('Picker checks passed: suggestions, keyboard selection, combined filters, skeletons, grouped global results, ETA labels and batch controls.');
})().catch(error=>{console.error(error);process.exitCode=1});
