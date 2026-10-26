import { test, expect, Page, Locator } from '@playwright/test';
import { readFileSync, writeFileSync, mkdirSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, openSync, closeSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(import.meta.dirname, '../..');
const out = resolve(root, 'audit/landing');
const base = 'http://127.0.0.1:8421/';
const repo = 'https://github.com/nickthelegend/hasami';
const record = (path: string) => JSON.parse(readFileSync(resolve(root, path), 'utf8'));
const hero = record('deployments/runs/20261021T053112874Z.json');
const replay = record('deployments/runs/20261021T053203432Z.json');
const after = ['173703', '174610', '175048', '175539'];
const before = ['165535', '170448', '170928', '171443'];
const bench = (time: string) => record(`deployments/bench/20261021T${time}Z.json`);
const norm = (s: string) => s.replace(/\s+/g, ' ').trim();
const ids = [...Array.from({length:21}, (_, i) => `L${String(i+1).padStart(3,'0')}`), ...Array.from({length:30}, (_, i) => `N${String(i+1).padStart(3,'0')}`), 'K004', 'K005'];
const sections: Record<string,string> = { K004:'#live',K005:'#apparatus', L003:'#how', L004:'#proof', L005:'#fridge', L006:'.goal-figs .gfig:nth-child(2)', L007:'.rover', L008:'#live', L009:'#live', L011:'#run', L012:'#run', L017:'#how', L018:'#proof', L019:'#fridge', L020:'#live', L021:'#run', N003:'#cut',N004:'#sheet',N005:'#replay',N006:'#twin',N007:'#apparatus',N009:'#replay',N010:'#replay',N011:'#replay',N012:'#replay',N013:'#replay',N014:'#replay',N015:'#twin',N016:'#cut',N017:'#sheet',N018:'#sheet',N019:'#replay',N020:'#twin',N021:'#apparatus',N022:'#run',N025:'#cut',N026:'#run',N027:'#sheet',N028:'#replay',N029:'#twin',N030:'#apparatus'};

let ownedServer: ChildProcess | null = null;
const scratch = mkdtempSync(resolve(tmpdir(), 'hasami-final-landing-'));
async function stopServer() {
  if (!ownedServer || ownedServer.exitCode !== null) return;
  const child = ownedServer;
  const ended = new Promise<void>(r => child.once('exit', () => r()));
  child.kill('SIGTERM'); await ended; ownedServer = null;
}
async function startServer(dashboard = false) {
  await stopServer();
  const fd = openSync(resolve(scratch, 'server.log'), 'a');
  const env = { ...process.env, HASAMI_HOST:'127.0.0.1', HASAMI_PORT:'8420', HASAMI_RUNS_DIR:resolve(scratch,'runs'), HASAMI_TWINS_DIR:resolve(scratch,'twins') };
  for (const key of ['MODEL_API_KEY','HASAMI_REALCAM','HASAMI_SO101_PORT','HASAMI_DISTURB']) delete env[key];
  ownedServer = spawn(resolve(root,'.venv/bin/python'), dashboard ? ['-m','hasami_server'] : ['-m','http.server','8421','--bind','127.0.0.1','--directory',resolve(root,'packages/landing')], { cwd:root,env,stdio:['ignore',fd,fd] });
  closeSync(fd);
  const url = dashboard ? 'http://127.0.0.1:8420/api/status' : base+'index.html';
  for (let i=0;i<180;i++) {
    if (ownedServer.exitCode !== null) throw Error('Owned server exited');
    try {if ((await fetch(url)).ok) return;} catch {}
    await new Promise(r=>setTimeout(r,500));
  }
  throw Error('Owned server startup timeout');
}
test.beforeAll(async()=>{ await startServer(); });
test.afterAll(async()=>{ await stopServer(); rmSync(scratch,{recursive:true,force:true}); });

function provenance(kind: string) { return JSON.parse(execFileSync(resolve(root,'.venv/bin/python'),[resolve(root,'audit/tests/landing-provenance.py'),kind],{encoding:'utf8'})); }
async function contains(p: Page, selector: string, texts: string[]) {
  for (const text of texts) await expect(p.locator(selector)).toContainText(text);
}
async function fit(p: Page, selector: string) {
  const boxes = await p.locator(selector).evaluateAll(els => els.filter(el=>el.checkVisibility()).map(el => {const r=el.getBoundingClientRect();return {text:el.textContent?.trim().slice(0,70),x:r.x,right:r.right,width:r.width,scroll:el.scrollWidth,client:el.clientWidth}}));
  const width = p.viewportSize()!.width;
  for (const b of boxes) { expect(b.width,JSON.stringify(b)).toBeGreaterThan(0);expect(b.x,JSON.stringify(b)).toBeGreaterThanOrEqual(-1);expect(b.right,JSON.stringify(b)).toBeLessThanOrEqual(width+1); }
  const clipped = await p.locator(selector).evaluateAll(els => {
    const bad: any[]=[];
    for (const el of els) {
      const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
      let node: Node|null;
      while((node=walker.nextNode())) {
        if(!node.textContent?.trim() || !node.parentElement?.checkVisibility())continue;
        const range=document.createRange();range.selectNodeContents(node);
        for(const r of range.getClientRects())if(r.width>0&&(r.left < -1||r.right>innerWidth+1))bad.push({text:node.textContent.trim().slice(0,90),left:r.left,right:r.right});
      }
    }
    return bad;
  });
  expect(clipped,'visible text must fit the viewport').toEqual([]);
  expect(await p.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}
async function image(p: Page, src: string, size?: number[]) {
  const img=p.locator(`img[src="${src}"]`); await img.scrollIntoViewIfNeeded();
  await expect.poll(()=>img.evaluate(async(el: HTMLImageElement)=>{await el.decode();return [el.naturalWidth,el.naturalHeight]})).toEqual(size ?? await img.evaluate(async(el: HTMLImageElement)=>{await el.decode();return [el.naturalWidth,el.naturalHeight]}));
  expect((await img.getAttribute('alt'))!.length).toBeGreaterThan(15);
  const response=await p.request.get(base+src);expect(response.status()).toBe(200);
}
async function svgImages(p: Page) {
  const imgs=p.locator('svg image[href="assets/frame-hero.webp"]'); await expect(imgs).toHaveCount(2);
  const decoded=await imgs.evaluateAll(async els=>Promise.all(els.map(async el=>{const im=new Image();im.src=el.getAttribute('href')!;await im.decode();return [im.naturalWidth, im.naturalHeight,el.parentElement?.getAttribute('role'),el.parentElement?.getAttribute('aria-label')]})));
  for(const d of decoded){expect(d.slice(0,2)).toEqual([640,480]);expect(d[2]).toBe('img');expect(String(d[3]).length).toBeGreaterThan(40);}
  expect((await p.request.get(base+'assets/frame-hero.webp')).status()).toBe(200);
}
async function anchor(p: Page, link: Locator, target: string, heading?: string) {
  await link.click();await expect(p).toHaveURL(new RegExp(`#${target}$`));
  await p.waitForTimeout(100);
  const headingLocator=heading?p.locator(`#${target}`).getByRole('heading',{name:heading,exact:false}).first():p.locator(`#${target}`);
  if(heading) await expect(headingLocator).toBeVisible();
  await expect.poll(async()=>{const b=await headingLocator.boundingBox();const h=await p.locator('header').boundingBox();return !!b&&b.y>=h!.height-1&&b.y<p.viewportSize()!.height;},{timeout:6000}).toBe(true);
  const box=await headingLocator.boundingBox(); const header=await p.locator('header').boundingBox();
  expect(box!.y,`#${target} below sticky header`).toBeGreaterThanOrEqual(header!.height-1);
  expect(box!.y).toBeLessThan(p.viewportSize()!.height);
}
async function skip(p: Page) {
  await p.keyboard.press('Tab');await expect(p.getByRole('link',{name:'Skip to content'})).toBeFocused();await p.keyboard.press('Enter');await expect(p).toHaveURL(/#main$/);
}
async function anchors(p: Page) {
  const bad=await p.locator('a[href^="#"]').evaluateAll(els=>els.map(el=>el.getAttribute('href')!).filter(h=>h.length<2||document.querySelectorAll(`[id="${h.slice(1)}"]`).length!==1));expect(bad).toEqual([]);
}
async function codeLinks(p: Page) {for(const name of ['Code','Get the code','GitHub'])await expect(p.getByRole('link',{name,exact:true})).toHaveAttribute('href',repo);}
async function quickstart(p: Page) {await contains(p,'#run pre',["uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e '.[dev]' -c constraints.txt",'(cd packages/dashboard && pnpm install && pnpm build)','python -m hasami_server']);await contains(p,'.run-note',['Then open http://127.0.0.1:8420/.','about 150 MB','SAM 2.1 weights']);}
async function motion(p: Page, reduced: boolean, evidence: any) {
  await p.emulateMedia({reducedMotion:reduced?'reduce':'no-preference'}); await p.reload({waitUntil:'load'});
  const sample=()=>p.locator('#blade').getAttribute('transform');const initial=await sample();
  if(reduced){expect(await p.locator('#reveal-p').getAttribute('stroke-dashoffset')).toBe('0');expect(await p.evaluate(()=>getComputedStyle(document.documentElement).scrollBehavior)).toBe('auto');await p.waitForTimeout(1100);expect(await sample()).toBe(initial);await p.waitForTimeout(2600);expect(await sample()).toBe(initial);evidence.motion={reduced,initial,final:await sample()};}
  else {await p.waitForTimeout(1150);const moving=await sample();expect(moving).not.toBe(initial);expect(moving).toContain('rotate(');await p.waitForTimeout(2550);const final=await sample();expect(final).not.toBe(moving);await p.waitForTimeout(700);expect(await sample()).toBe(final);const expected=await p.locator('#track').evaluate((el: any)=>{const len=el.getTotalLength();const a=el.getPointAtLength(.999*len),b=el.getPointAtLength(Math.min(len,.999*len+.5));return `translate(${a.x} ${a.y}) rotate(${Math.atan2(b.y-a.y,b.x-a.x)*180/Math.PI})`});expect(final).toBe(expected);evidence.motion={reduced,initial,moving,final};}
}
async function live(p: Page){await contains(p,'#live',['What runs today','Live','Setup required','Muse planning and SAM 3.1','MODEL_API_KEY','HASAMI_SO101_PORT']);await expect(p.locator('.wait code')).toHaveText(['MODEL_API_KEY','HASAMI_SO101_PORT']);}
async function heroValues(p: Page){await contains(p,'.vf',['red cube · IoU 0.953','tray · 0.999','203.6 mm','−40.2 mm','0.14 mm']);const a=hero.steps[0].attempts[0];expect(a.pick.target.center).toEqual([.2036,-.0402,.0125]);expect(a.localisation_error_mm).toBe(.14);expect(a.pick.iou_vs_truth.toFixed(3)).toBe('0.953');expect(a.place.iou_vs_truth.toFixed(3)).toBe('0.999');}
async function how(p:Page){await expect(p.locator('#how h2')).toHaveText('Three cuts from a frame to a closed gripper');await expect(p.locator('.steps3 li')).toHaveCount(3);expect((await p.locator('.steps3 li').allTextContents()).map(norm).map(s=>s.match(/^\d{2}(?:Cut it out|Locate it|Pick it up)/)?.[0])).toEqual(['01Cut it out','02Locate it','03Pick it up']);await image(p,'assets/illo-cutline.webp');await contains(p,'.how-illo figcaption',['Illustration']);}
async function proof(p:Page){expect((await p.locator('.nums b').allTextContents()).map(norm)).toEqual(['146/160','1106/1111','0.14 mm']);await expect(p.locator('.nums span')).toHaveText(['Tasks','Checks','Error']);const clean=Object.values(bench(after[0]).families) as any[];expect(clean.reduce((n,f)=>n+f.success,0)).toBe(146);expect(clean.reduce((n,f)=>n+f.ran,0)).toBe(160);const families=after.flatMap(t=>Object.values(bench(t).families)) as any[];expect(families.reduce((n,f)=>n+f.verify.agree,0)).toBe(1106);expect(families.reduce((n,f)=>n+f.verify.checks,0)).toBe(1111);await p.getByRole('link',{name:'See the evidence'}).click();await expect(p).toHaveURL(base+'evidence.html#sheet');await p.waitForTimeout(800);await expect(p.locator('#sheet h2')).toBeInViewport();}
async function fridge(p: Page){await expect(p.locator('#fridge h2')).toHaveText('Next: bring me the juice from the fridge');await image(p,'assets/illo-fridge.webp');await contains(p,'.goal-figs .gfig:first-child figcaption',['Illustration','A generated image of the goal','the real rover has not done this yet']);}
async function fetchRun(p:Page){await image(p,'assets/fridge-fetch.webp',[1936,520]);await contains(p,'.goal-figs .gfig:nth-child(2) figcaption',['Recorded run','Simulated today','30 of 30 fetches']);const d=record('deployments/fetch/20261026T110634Z.json');expect(Object.keys(d.summary)).toEqual(['juice carton','can','bottle']);for(const s of Object.values(d.summary) as any[]) {expect(s.ok).toBe(10);expect(s.runs).toBe(10);expect(s.check_agrees).toBe(10);expect(s.checks).toBe(10);}const run=record('deployments/runs/20261026T111544704Z.json');expect(run.ok).toBe(true);expect(run.steps[0].attempts[0].pick.truth_label).toBe('orange juice carton');}
async function rover(p:Page){await image(p,'assets/illo-rover.webp');await contains(p,'.rover figcaption',['Illustration','not a photo']);await expect(p.locator('.parts li > b')).toHaveText(['Base','Arm','Runtime','Find','Reach']);await contains(p,'.parts',['Printed PETG','Six MG996R','phone','SAM 2.1','Not built yet']);}
async function notebookLinks(p:Page){for(const selector of ['header .toplinks a[href="evidence.html"]','.acts a[href="evidence.html"]','footer a[href="evidence.html"]','footer a[href="evidence.html#sheet"]']){await p.goto(base+'index.html');const href=await p.locator(selector).getAttribute('href');await expect(p.locator(selector)).toBeVisible();await p.locator(selector).click();await expect(p).toHaveURL(base+href);await expect(p.locator(href!.includes('#sheet')?'#sheet h2':'h1')).toBeInViewport();}}
async function notebookNavigation(p:Page){await anchors(p);await anchor(p,p.getByRole('link',{name:'Read the results sheet'}),'sheet','Measured in the sim');const names=['Notebook','Results','Replay','Twin','Apparatus'],targets=['cut','sheet','replay','twin','apparatus'];for(let i=0;i<targets.length;i++) await anchor(p,p.locator('footer').getByRole('link',{name:names[i],exact:true}),targets[i]);await anchor(p,p.getByRole('link',{name:'Run it locally'}),'run');await p.getByRole('link',{name:'Hasami home'}).click();await expect(p).toHaveURL(base);await expect(p.locator('h1')).toHaveText('SAM cuts it out. The arm picks it up.');}
async function cut(p:Page){await expect(p.locator('.entry')).toHaveCount(4);await contains(p,'.entries',['Setup required: MODEL_API_KEY','(353, 261)','465 px','0.953','701 ms','444 of 465 px','26.2 mm','held · 19.7 mm']);await expect(p.locator('.jaws')).toHaveAttribute('aria-label',/Top view/);const a=hero.steps[0].attempts[0];const phases=await p.locator('.armlog li').evaluateAll(els=>els.map(el=>[el.querySelector('span')!.textContent,el.querySelector('.v')!.textContent]));expect(phases).toEqual(a.phases.map((ph:any)=>[ph.phase,`${ph.error_mm.toFixed(1)} mm`]));expect(a.pick.point).toEqual([353,261]);expect(a.pick.mask_pixels).toBe(465);expect(a.pick.target.pixels).toBe(444);expect(Math.round(a.pick.latency_ms)).toBe(701);expect(a.grasp.width_mm).toBe(26.2);expect(a.grasp_checks[0].jaw_gap_mm).toBe(19.7);}
async function familyRows(p:Page,e:any){const rows=await p.locator('#sheet table').nth(1).locator('tbody tr:not(.grp)').evaluateAll(els=>els.map(el=>Array.from(el.querySelectorAll('td')).map(td=>td.textContent!.trim())));expect(rows).toHaveLength(32);let i=0;for(const t of after){const d=bench(t);for(const f of Object.values(d.families) as any[]){const r=rows[i++];expect(r.slice(0,5)).toEqual([f.family,d.planner==='scripted'?'scripted plan, hints 20 px off (no model)':'simulated-click planner',`${f.success}/${f.ran}`,`${f.steps_ok}/${f.steps}`,String(f.retries)]);const failures=Object.fromEntries([...r[5].matchAll(/([a-z]+) (\d+)/g)].map(m=>[m[1],Number(m[2])]));expect(failures).toEqual(f.failures);expect(r[6]).toBe(`${f.verify.agree}/${f.verify.checks}`);expect(r[7]).toBe(f.iou_mean.toFixed(3));expect(r[8]).toBe(`${Math.round(f.seconds)} s`);}}await contains(p,'#sheet .tally',['146/160','76/80','1106/1111']);e.familyRows=rows;}
const total=(d:any,key:string)=>Object.values(d.families).reduce((n:number,f:any)=>n+(f[key]??0),0);
const ver=(d:any,key:string)=>Object.values(d.families).reduce((n:number,f:any)=>n+f.verify[key],0);
async function comparisonRows(p:Page,e:any){const rows=await p.locator('#sheet table').nth(0).locator('tbody tr:not(.grp)').evaluateAll(els=>els.map(el=>Array.from(el.querySelectorAll('td')).map(td=>td.textContent!.trim())));expect(rows).toHaveLength(4);for(let i=0;i<4;i++){const a=bench(before[i]),b=bench(after[i]),r=rows[i];expect(r[1]).toBe(`0-${(Object.values(b.families)[0] as any).seeds.at(-1)} × 8 families`);expect(r[2]).toBe(`${total(a,'success')}/${total(a,'ran')}`);expect(r[3]).toBe(`${total(b,'success')}/${total(b,'ran')}`);expect(r[4]).toBe(`${total(a,'retries')} → ${total(b,'retries')}`);const f:Record<string,number>={};for(const fam of Object.values(b.families) as any[])for(const [k,v]of Object.entries(fam.failures))f[k]=(f[k]??0)+Number(v);expect(Object.fromEntries([...r[5].split(' (was')[0].matchAll(/([a-z]+) (\d+)/g)].map(m=>[m[1],Number(m[2])]))).toEqual(f);expect(r[6]).toBe(`${total(b,'steps_implausible')} step${total(b,'steps_implausible')===1?'':'s'}`);expect(r[7]).toBe(`${ver(b,'agree')}/${ver(b,'checks')}`);expect(r[8]).toBe(`${before[i]}Z → ${after[i]}Z`);}expect(before.reduce((n,t)=>n+ver(bench(t),'agree'),0)).toBe(1172);expect(before.reduce((n,t)=>n+ver(bench(t),'checks'),0)).toBe(1175);await contains(p,'#sheet .sheet-foot',['1172 of 1175']);e.comparisonRows=rows;}
async function replaySummary(p:Page){await expect(p.locator('#replay .frame')).toHaveCount(6);await expect(p.locator('#replay .retry')).toHaveCount(3);await expect(p.locator('#replay .verdict.no')).toHaveCount(3);await expect(p.locator('#replay .verdict.ok')).toHaveCount(3);await contains(p,'.replay-foot',['3/3','3 retries','6/6','0.859','0.706']);expect(replay.placed).toBe(3);expect(replay.verify.agree).toBe(6);expect(replay.verify.checks).toBe(6);expect(replay.iou.mean.toFixed(3)).toBe('0.859');expect(replay.iou.min.toFixed(3)).toBe('0.706');for(let i=0;i<3;i++){const a=replay.steps[i].attempts;expect(a).toHaveLength(2);expect(a[0].injected.offset_mm).toBe(6);expect(a[0].verify.ok).toBe(false);expect(a[1].verify.ok).toBe(true);await expect(p.locator('.contact .col').nth(i).locator('figcaption').first()).toContainText(`descend off by ${a[0].phases.find((x:any)=>x.phase==='descend').error_mm.toFixed(1)} mm`);await expect(p.locator('.contact .col').nth(i).locator('figcaption').last()).toContainText(`IoU ${a[1].pick.iou_vs_truth.toFixed(3)}`);}}
async function twin(p:Page){await image(p,'assets/twin-sbs.webp',[1440,568]);await contains(p,'.twin-fig figcaption',['synthetic','MuJoCo render, seed 1','Right: the twin','A real desk photo has not been measured yet']);const s=record('deployments/twins/accuracy-20261021T164701Z.json').summary,t=s.true_fov,d=s.assumed_fov_70;await contains(p,'#twin',['86/86','1.2 mm','1.6 mm','16/20','0 false positives','83/86','colour 86/86','centre max 4.9 mm','centre mean 3.3 mm']);expect([t.found,t.objects,t.false_positives,t.kind_correct,t.colour_correct,t.centre_err_mm.mean,t.height_err_mm.mean.toFixed(1),t.centre_err_mm.max.toFixed(1),d.found,d.centre_err_mm.mean.toFixed(1),s.clear_desk_on_twin.ok,s.clear_desk_on_twin.run]).toEqual([86,86,0,83,86,1.2,'1.6','4.9',83,'3.3',16,20]);}
async function apparatus(p:Page){await expect(p.locator('ul.apparatus li')).toHaveCount(9);await expect(p.locator('ul.apparatus .stamp')).toHaveText(['running','running','Setup required: MODEL_API_KEY','Setup required: MODEL_API_KEY','running','running','pending · a real photo','pending · ruler check','Setup required: HASAMI_SO101_PORT']);await contains(p,'.notes',['MuJoCo simulation','physics state','Click an object','SAM 2.1 runs locally']);}

for(const id of ids)for(const width of [1280,375])test(`${id} ${width}x${width===375?812:800}`,async({page,browser},testInfo)=>{
  await page.setViewportSize({width,height:width===375?812:800});
  const evidence:any={id,browser:browser.version(),viewport:page.viewportSize(),started:new Date().toISOString(),navigations:[],console:[],pageErrors:[],requests:[],failedRequests:[],checks:[]};
  page.on('framenavigated',frame=>{if(frame===page.mainFrame())evidence.navigations.push(frame.url())});page.on('console',m=>evidence.console.push({type:m.type(),text:m.text()}));page.on('pageerror',e=>evidence.pageErrors.push(e.message));
  page.on('response',r=>evidence.requests.push({url:r.url(),status:r.status(),method:r.request().method()}));page.on('requestfailed',r=>evidence.failedRequests.push({url:r.url(),error:r.failure()?.errorText}));
  const notebook=id.startsWith('N')||id==='K005';await page.goto(base+(notebook?'evidence.html':'index.html'),{waitUntil:'networkidle'});
  let result='FAIL';
  evidence.expected=readFileSync(resolve(root,'AUDIT.md'),'utf8').split('\n').find(line=>line.startsWith(`| ${id} |`))?.split(' | ')[2];
  try{
    switch(id){
      case 'L001':await expect(page.locator('h1')).toHaveText('SAM cuts it out. The arm picks it up.');await svgImages(page);await heroValues(page);break;
      case 'L002':evidence.provenance=provenance('hero');await svgImages(page);await contains(page,'.vf-cap',['recorded run','SAM 2.1']);break;
      case 'L003':case 'L017':await how(page);await fit(page,'.steps3 li,.how-illo');break;
      case 'L004':case 'L018':await proof(page);break;
      case 'L005':await fridge(page);break;
      case 'L006':evidence.provenance=provenance('fetch');await fetchRun(page);break;
      case 'L007':await rover(page);break;
      case 'K004':case 'L008':await live(page);break;
      case 'L009':case 'L020':await live(page);for(const name of ['Open the live dashboard','local dashboard']) {await page.goto(base+'index.html');const link=page.getByRole('link',{name,exact:true});await expect(link).toHaveAttribute('href','http://127.0.0.1:8420/');await startServer(true);await link.click();await expect(page).toHaveURL('http://127.0.0.1:8420/');await expect(page.locator('h1')).toHaveText('Viewfinder / overhead');await page.waitForTimeout(600);await page.goto('about:blank');await startServer();}await page.goto(base+'index.html');await fit(page,'#live,.wait,.acts');break;
      case 'L010':await notebookLinks(page);break;
      case 'L011':await quickstart(page);break;
      case 'L012':await contains(page,'footer',['Hasami — 鋏, scissors','MIT','Copyright (c) 2026 Nivesh Gajengi','not affiliated with Meta']);await codeLinks(page);break;
      case 'L013':await skip(page);await anchors(page);for(const target of ['top','main','how','proof','fridge','live','run'])await expect(page.locator(`[id="${target}"]`)).toHaveCount(1);await anchor(page,page.locator('.brand'),'top');break;
      case 'L014':await motion(page,false,evidence);break;
      case 'L015':await motion(page,true,evidence);break;
      case 'L016':await heroValues(page);await svgImages(page);await fit(page,'h1,.acts,.shot,.loupe,.readout,.readout dd');break;
      case 'L019':await fridge(page);await fetchRun(page);await rover(page);await fit(page,'.goal-figs,.rover,.parts .stamp');break;
      case 'L021':await skip(page);await notebookLinks(page);await page.goto(base+'index.html');await quickstart(page);await fit(page,'#run pre,.legal');await motion(page,false,evidence);evidence.ordinaryMotion=evidence.motion;await motion(page,true,evidence);break;
      case 'N001':await expect(page.locator('h1')).toHaveText('The lab notebook: every run, every number.');await contains(page,'.lede',['Every number here is SAM 2.1 with a clicked or scripted plan, not Muse']);await contains(page,'.readout',['203.6','−40.2','12.5','25.0 mm','−24.9°','0.14 mm']);break;
      case 'N002':case 'N003':case 'N004':case 'N005':case 'N006':case 'N007':{const i=Number(id.slice(1))-2;const targets=['top','cut','sheet','replay','twin','apparatus'],headings=['The lab notebook:','From a sentence to a closed gripper','Measured in the sim','Missed on purpose, found again','One photo with an A4 sheet','What runs, what waits for a key'];if(width===1280)await anchor(page,page.locator('.index a').nth(i),targets[i],headings[i]);else{await expect(page.locator('.index')).toBeHidden();if(i===0){await page.goto(base+'evidence.html#top');await expect(page.locator('h1')).toBeInViewport();}else await anchor(page,page.locator('footer a[href="#'+targets[i]+'"]'),targets[i],headings[i]);}break;}
      case 'N008':evidence.provenance=provenance('hero');await svgImages(page);await heroValues(page);await contains(page,'.vf-cap',['20261021T053112874Z']);break;
      case 'N009':case 'N010':case 'N011':case 'N012':case 'N013':case 'N014':{const n=Number(id.slice(1))-9,s=Math.floor(n/2),a=n%2;evidence.provenance=provenance(`replay-s${s}a${a+1}`);await image(page,`assets/replay-s${s}a${a+1}.webp`,[300,150]);const f=page.locator('#replay .frame').nth(n);await expect(f.locator('img')).toHaveAttribute('alt',`Overhead frame after step ${s+1}, attempt ${a+1}`);await expect(f.locator('.verdict')).toHaveText(a?'verified':'missed');expect(replay.steps[s].attempts[a].verify.ok).toBe(Boolean(a));break;}
      case 'N015':case 'N020':case 'N029':await twin(page);await fit(page,'.twin-fig,#twin .tally,#twin .sheet-foot');break;
      case 'N016':await cut(page);break;
      case 'N017':await familyRows(page,evidence);break;
      case 'N018':await comparisonRows(page,evidence);break;
      case 'N019':await replaySummary(page);break;
      case 'K005':await apparatus(page);await expect(page.locator('.entries .stamp-setup')).toHaveText(['Setup required: MODEL_API_KEY','SAM 3.1 · Setup required: MODEL_API_KEY']);await fit(page,'.apparatus .stamp,.entry .stamp');break;
      case 'N021':await apparatus(page);break;
      case 'N022':await anchor(page,page.getByRole('link',{name:'Run it locally'}),'run');await quickstart(page);break;
      case 'N023':await skip(page);await notebookNavigation(page);await page.goto(base+'evidence.html');await codeLinks(page);break;
      case 'N024':await motion(page,false,evidence);evidence.ordinaryMotion=evidence.motion;await motion(page,true,evidence);break;
      case 'N025':await svgImages(page);await cut(page);await fit(page,'.shot,.loupe,.readout,.entry,.armlog li');break;
      case 'N026':if(width===375)await expect(page.locator('.index')).toBeHidden();await skip(page);await notebookNavigation(page);break;
      case 'N027':await familyRows(page,evidence);await comparisonRows(page,evidence);if(width===375){await expect(page.locator('.res').first()).toHaveCSS('display','block');for(const td of await page.locator('.res tr:not(.grp) td').all())expect(await td.getAttribute('data-l')).toBeTruthy();}await fit(page,'.res tr:not(.grp),.res td');break;
      case 'N028':await replaySummary(page);for(let s=0;s<3;s++)for(let a=1;a<=2;a++)await image(page,`assets/replay-s${s}a${a}.webp`,[300,150]);await fit(page,'.frame,.retry,.replay-foot');break;
      case 'N030':await apparatus(page);await quickstart(page);await fit(page,'.apparatus li,.stamp,.notes,#run pre,.legal');await motion(page,true,evidence);break;
    }
    if(page.url().includes('evidence.html')!==notebook)await page.goto(base+(notebook?'evidence.html':'index.html'));
    if(sections[id])await page.locator(sections[id]).evaluate(el=>el.scrollIntoView({block:'start',behavior:'instant'}));
    await page.waitForTimeout(150);
    expect(evidence.console.filter((m:any)=>m.type==='error')).toEqual([]);expect(evidence.pageErrors).toEqual([]);expect(evidence.failedRequests).toEqual([]);expect(evidence.requests.filter((r:any)=>r.status>=400)).toEqual([]);
    expect(evidence.requests.filter((r:any)=>!r.url.startsWith(base)&&!r.url.startsWith('http://127.0.0.1:8420/')&&!r.url.startsWith('https://fonts.'))).toEqual([]);
    await fit(page,'body');
    const shot=resolve(out,`${id}-${width}.jpg`);await page.screenshot({path:shot,type:'jpeg',quality:45});expect(statSync(shot).size).toBeLessThanOrEqual(200000);
    evidence.screenshots=[`${id}-${width}.jpg`];
    const extras: Record<string,string> = {K005:'.entries .stamp-setup',L016:'.stage',L017:'.steps3 li',L019:'.gfig',L021:'#run pre',N025:'.stage,.entry,.armlog',N027:'.res tbody tr:not(.grp)',N028:'.frame',N029:'.twin-fig,.twin-sheet',N030:'.apparatus li,.notes,#run pre'};
    if(width===375&&extras[id]) {
      const elements=await page.locator(extras[id]).all();
      for(let index=0;index<elements.length;index++) {
        await elements[index].evaluate(el=>el.scrollIntoView({block:'start',behavior:'instant'}));
        const name=`${id}-${width}-${index+1}.jpg`;const path=resolve(out,name);
        await page.screenshot({path,type:'jpeg',quality:45});expect(statSync(path).size).toBeLessThanOrEqual(200000);evidence.screenshots.push(name);
      }
    }
    evidence.headings=await page.locator('h1,h2.h').allTextContents();evidence.url=page.url();evidence.documentWidth=await page.evaluate(()=>document.documentElement.scrollWidth);result='PASS';
  }finally{evidence.status=result;evidence.assertions=testInfo.errors.map(e=>e.message);evidence.finished=new Date().toISOString();mkdirSync(out,{recursive:true});writeFileSync(resolve(out,`${id}-${width}.json`),JSON.stringify(evidence,null,2)+'\n');}
});
