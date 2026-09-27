// Small local proxy so the browser does not fan out requests to poe.ninja.
// Requires Node.js 18+ (built-in fetch); no package installation is needed.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const ROOT = __dirname;
const PORT = Number(process.env.PORT || 4173);
const cache = new Map();
const MIME = { '.html':'text/html; charset=utf-8', '.css':'text/css; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.json':'application/json; charset=utf-8', '.svg':'image/svg+xml' };
const UA = 'WitchcraftBuildPlanner/2.0 (local community build planner)';
async function cached(key, ttl, fn) { const hit=cache.get(key); if(hit && Date.now()-hit.at<ttl) return hit.value; const value=await fn(); cache.set(key,{at:Date.now(),value}); return value; }
async function json(url) { const r=await fetch(url,{headers:{Accept:'application/json','User-Agent':UA}}); if(!r.ok) throw new Error(`Upstream returned ${r.status}`); return r.json(); }
async function market() { return cached('market',5*60*1000,async()=>{
  const leagues=await json('https://poe.ninja/poe1/api/economy/leagues');
  if(!Array.isArray(leagues)||!leagues.length) throw new Error('No active Path of Exile 1 trade league');
  const league=leagues[0].id||leagues[0].name;
  const base='https://poe.ninja/poe1/api/economy';
  const urls=[`${base}/stash/current/currency/overview?league=${encodeURIComponent(league)}&type=Currency`,...['UniqueWeapon','UniqueArmour','UniqueAccessory','UniqueFlask','UniqueJewel'].map(type=>`${base}/stash/current/item/overview?league=${encodeURIComponent(league)}&type=${type}`)];
  const responses=await Promise.all(urls.map(async url=>{try{return await json(url)}catch(error){return {error:error.message,lines:[]}}}));
  const divine=responses[0].lines?.find(x=>/divine orb/i.test(x.currencyTypeName||''));
  const prices={}; for(let i=1;i<responses.length;i++) for(const item of responses[i].lines||[]){ const price=Number(item.chaosValue??item.primaryValue??item.chaosEquivalent); if(item.name&&price>0)(prices[item.name]??=[]).push(price); }
  return {league,leagueName:leagues[0].name,divineChaos:Number(divine?.chaosEquivalent)||null,prices,refreshed:new Date().toISOString(),errors:responses.slice(1).map(x=>x.error).filter(Boolean)};
 }); }
function attrs(text){const o={};for(const m of text.matchAll(/([\w:-]+)="([^"]*)"/g))o[m[1]]=m[2];return o}
async function passiveData(buildId){return cached(`tree:${buildId}`,60*60*1000,async()=>{
  const rawR=await fetch(`https://pobb.in/${encodeURIComponent(buildId)}/raw`,{headers:{'User-Agent':'Mozilla/5.0 WitchcraftBuildPlanner/2.0','Accept':'text/plain'}});
  if(!rawR.ok)throw new Error(`Path of Building export returned ${rawR.status}`);
  const raw=(await rawR.text()).trim(); if(!raw||raw.startsWith('<')) throw new Error('Could not read the public Path of Building export');
  const payload=Buffer.from(raw.replace(/-/g,'+').replace(/_/g,'/'),'base64');
  const xml=zlib.inflateSync(payload).toString('utf8');
  const treeBody=xml.match(/<Tree\b([^>]*)>([\s\S]*?)<\/Tree>/i);
  if(!treeBody)throw new Error('This Path of Building export does not include a passive tree');
  const specs=[...treeBody[2].matchAll(/<Spec\b([^>]*)>/gi)].map(m=>attrs(m[1]));
  const active=Math.max(0,Math.min(specs.length-1,Number(attrs(treeBody[1]).activeSpec||1)-1));
  const selected=specs[active]||specs[0]; const allocated=(selected?.nodes||'').split(',').filter(Boolean);
  const tree=await cached('official-tree',60*60*1000,()=>json('https://raw.githubusercontent.com/grindinggear/skilltree-export/master/data.json'));
  const groups={}; for(const [id,g] of Object.entries(tree.groups||{}))groups[id]={x:g.x,y:g.y,nodes:g.nodes,orbits:g.orbits};
  const nodes={}; for(const [id,n] of Object.entries(tree.nodes||{}))nodes[id]={g:n.group,o:n.orbit,i:n.orbitIndex,out:n.out,name:n.name,k:n.isKeystone?1:0,t:n.isNotable?1:0,s:n.isJewelSocket?1:0};
  return {groups,nodes,allocated,constants:tree.constants||{},leagueTreeVersion:tree.treeVersion||null,characterClass:tree.characterData?.[selected?.classId]?.name||'Witch',specName:selected?.title||'Build tree'};
 });}
function send(res,status,body,type='application/json; charset=utf-8'){res.writeHead(status,{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(type.startsWith('application/json')?JSON.stringify(body):body)}
const server=http.createServer(async(req,res)=>{const pathname=new URL(req.url,'http://localhost').pathname;
  try{
    if(pathname==='/api/market'){send(res,200,await market());return}
    if(pathname==='/api/passives'){const id=new URL(req.url,'http://localhost').searchParams.get('build');if(!id||!/^[A-Za-z0-9_-]{4,20}$/.test(id))throw new Error('Invalid build identifier');send(res,200,await passiveData(id));return}
    const requested=pathname==='/'?'index.html':pathname.slice(1);const full=path.resolve(ROOT,requested);if(!full.startsWith(ROOT+path.sep)){send(res,404,'Not found','text/plain; charset=utf-8');return}
    const ext=path.extname(full);if(!['.html','.css','.js','.json','.svg'].includes(ext)){send(res,404,'Not found','text/plain; charset=utf-8');return}
    fs.readFile(full,(err,data)=>{if(err){send(res,404,'Not found','text/plain; charset=utf-8');return}send(res,200,data,MIME[ext])});
  }catch(error){send(res,502,{error:String(error.message||error)})}
});
server.listen(PORT,'127.0.0.1',()=>console.log(`Witchcraft planner: http://127.0.0.1:${PORT}`));
