const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const profile=path.resolve('.tmp-tests/chrome');fs.mkdirSync(profile,{recursive:true});
fs.rmSync(path.join(profile,'DevToolsActivePort'),{force:true});
const browser=spawn(process.env.AUTO_AGENTS_TEST_CHROME,['--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
 '--disable-background-networking','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
let socket;
async function wait(fn){for(let i=0;i<300;i++){try{const v=await fn();if(v)return v}catch{}await sleep(200)}throw new Error('Browser condition timed out')}
async function main(){
 const portFile=path.join(profile,'DevToolsActivePort');
 const port=await wait(()=>fs.existsSync(portFile)&&fs.readFileSync(portFile,'utf8').split('\n')[0]);
 const pages=await (await fetch('http://127.0.0.1:'+port+'/json/list')).json();
 socket=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
 await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j});
 let next=0;const pending=new Map();
 socket.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result)}};
 const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}))});
 const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value};
 const click=async id=>{const pos=await evaluate(`(()=>{const r=document.querySelector('${id}').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...pos});await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...pos})};
 await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url:process.argv[2]});
 await wait(()=>evaluate("document.querySelector('#state')?.dataset.loaded==='true'"));
 if(process.argv[3]==='create'){
  assert(await evaluate("!!document.querySelector('#create')"));await click('#create');
  await wait(()=>evaluate("document.querySelector('#state')?.dataset.project==='project-1'"));
 }else{
  assert(await evaluate("document.querySelector('#state').dataset.project==='project-1'"));
  assert(!await evaluate("!!document.querySelector('#create')"));await click('#generate');
  await wait(()=>evaluate("document.querySelector('#state')?.dataset.task==='local-provider-1'"));
 }
 const observation=await evaluate("fetch('/api/state').then(r=>r.json())");
 assert.equal(observation.creates,1);assert.equal(observation.submits,process.argv[3]==='create'?0:1);
 const image=await send('Page.captureScreenshot',{format:'png'});
 const evidence='.auto-agents/docs/acceptance/runtime';fs.mkdirSync(evidence,{recursive:true});
 fs.writeFileSync(evidence+'/browser.png',Buffer.from(image.data,'base64'));
 fs.writeFileSync(evidence+'/observation.json',JSON.stringify(observation));
 console.log(JSON.stringify(observation));
}
main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{
 if(socket)socket.close();browser.kill('SIGTERM');await sleep(400);
});
