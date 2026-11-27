// Arabic interface completeness: every visible English string on the pages and every fixed message the agent shows
// must have an Arabic translation. Run: node test_i18n.cjs
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const ctx = {window: {}, LANG: 'ar'};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/kizuna-ar.js', 'utf8'), ctx);
const shell = fs.readFileSync(__dirname + '/kizuna.js', 'utf8');
const engine = shell.slice(shell.indexOf('function tr(core){'), shell.indexOf('// never translated'));
vm.runInContext('const AR=window.KIZUNA_AR;' + engine + ';this.t=t;', ctx);
const t = ctx.t;

// brand and technical names that stay as they are
const KEEP = new Set(['Kizuna', 'KIZUNA', 'Jellyfin', 'HDR', 'English', 'Weekly Anime', 'Kizuna home', 'Stremio', 'MKVToolNix']);
const missing = new Set();
const check = s => { s = s.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim(); if (/[A-Za-z]{2}/.test(s) && !KEEP.has(s) && t(s) === s) missing.add(s); };

for (const page of ['dashboard.html', 'stremio.html', 'weekly.html', 'settings.html', 'news.html', 'setup.html', 'family.html']) {
  const html = fs.readFileSync(__dirname + '/' + page, 'utf8').replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/g, '');
  for (const m of html.matchAll(/>([^<>]+)</g)) check(m[1]);
  for (const m of html.matchAll(/\b(?:placeholder|aria-label|title)="([^"]+)"/g)) check(m[1]);
}
// fixed (non-templated) messages from the agent and the servers
const py = ['agent.py', 'library_processing.py', 'processing_worker.py', 'stremio.py', 'stremio_batch.py', 'status.py', 'shows.py', 'settings.py', 'setup.py'];
const pat = /(?:show\([^,\n]+,\s*|stage=|(?:User|Source|Show|Settings|Setup)Error\(|headline, tone = |"reason": |reason=|"stage": )"([^"\n{]{6,})"/g;
for (const f of py) for (const m of fs.readFileSync(__dirname + '/' + f, 'utf8').matchAll(pat)) check(m[1]);

// a few composed messages built at run time
for (const s of ['Season 2 · 3 in library · latest S02E06', 'in 2d 13h', 'Ep 8 of 12', '26 titles found', 'Updated 2 min ago',
                 'Last ready: Blue Box · S02E06, 2 days ago.', 'Working on Blue Box · S02E06', '3 ready · 1 waiting for subtitles · 0 failed',
                 '120 downloadable releases found. Check one to choose its tracks.', 'Episode 8 airs Fri, Nov 20, 05:00 PM'])
  check(s);
// titles must never change, even when they look like UI words
assert.equal(t('Blue Box'), 'Blue Box');
assert.equal(t('Season 2 · Blue Box'), 'الموسم 2 · Blue Box');

if (missing.size) { console.error('Missing Arabic for:\n  ' + [...missing].sort().join('\n  ')); process.exit(1); }
console.log('Arabic interface complete: every page string and agent message has a translation.');
