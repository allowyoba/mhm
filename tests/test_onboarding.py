"""Run background connection and security behavior with the browser API fixture."""

from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required")
class BrowserFlowTests(unittest.TestCase):
    def test_native_disconnect_blocks_and_explicit_disconnect_clears_proxy(self):
        javascript = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const firefox=process.argv[2]==='firefox';
let listener,onNativeMessage,onNativeDisconnect,currentProxy=null,coreRunning=false,forceLimit=false;
const storage={};
const ctx={console,setTimeout,clearTimeout,Map,Set,Date,importScripts(...files){for(const file of files){vm.runInContext(fs.readFileSync(require("path").join(require("path").dirname(process.argv[1]),file),"utf8"),ctx);}},
 chrome:{storage:{local:{get:async()=>storage,set:async x=>Object.assign(storage,x),remove:async()=>{}}},
 proxy:{settings:{set:async x=>{assert(firefox ? x.value.proxyType === "manual" : x.value.mode === "fixed_servers", "ProxyConfig must match the browser API");currentProxy=x.value;},clear:async()=>{currentProxy=null;},get:async()=>({levelOfControl:'controlled_by_this_extension'})}},
 alarms:{create:async()=>{},onAlarm:{addListener(){}}},tabs:{create:async()=>{}},
 runtime:{id:'mhm-test',lastError:null,getURL:x=>x,onMessage:{addListener(fn){listener=fn;}},onInstalled:{addListener(){}},sendMessage:async()=>{},
 connectNative(){return {onMessage:{addListener(fn){onNativeMessage=fn;}},onDisconnect:{addListener(fn){onNativeDisconnect=fn;}},postMessage(msg){
 let data={};
 if(msg.command==='connect'&&forceLimit){queueMicrotask(()=>onNativeMessage({ok:false,requestId:msg.requestId,errorCode:'TRAFFIC_LIMIT',error:'Traffic limit reached'}));return;}
 if(msg.command==='saveTrafficPolicy')data={budget:{blocked:false,warning:false}};
 if(msg.command==='connect'){coreRunning=true;data={status:'connected',profileId:msg.profileId,localPort:10808,revision:'a'};}
 else if(msg.command==='disconnect'){coreRunning=false;data={status:'disconnected'};}
 else if(msg.command==='status')data={status:coreRunning?'connected':'disconnected',localPort:10808};
 else if(msg.command==='bootstrap')data={profiles:[{id:'profile',name:'Test'}],status:{status:'disconnected'}};
 queueMicrotask(()=>onNativeMessage({ok:true,requestId:msg.requestId,...data}));
 }}}}}};
ctx.browser=ctx.chrome;
if(firefox)ctx.browser.runtime.getBrowserInfo=async()=>({name:"Firefox"});
const proxyPort=()=>firefox ? Number(currentProxy.http.split(":").pop()) : currentProxy.rules.singleProxy.port;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const send=action=>new Promise(resolve=>listener(action,{id:'mhm-test'},resolve));
(async()=>{
 await send({action:'initialize'});
 let result=await send({action:'connect',profileId:'profile'});
 assert(result.ok);assert.equal(proxyPort(),10808);
 onNativeDisconnect();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(proxyPort(),9,'unexpected disconnect must block browser');
 result=await send({action:'disconnect'});assert(result.ok);assert.equal(currentProxy,null);
 await send({action:'connect',profileId:'profile'});onNativeDisconnect();
 await new Promise(resolve=>setImmediate(resolve));
 await send({action:'preferences',preferences:{killSwitch:false}});
 assert.equal(currentProxy,null,'turning protection off restores connection');
 forceLimit=true;result=await send({action:'connect',profileId:'profile'});assert(!result.ok);assert.equal(proxyPort(),9);
 await send({action:'saveTrafficPolicy',policy:{enabled:false}});assert.equal(proxyPort(),9,'budget changes must not silently bypass the proxy');
 assert.equal(vm.runInContext('state.status',ctx),'blocked');
 await send({action:'disconnect'});assert.equal(currentProxy,null);
 assert(!Object.keys(storage).some(k=>/profilesCache|subscriptionsCache/.test(k)));
 assert.equal(listener({action:'ping'},{id:'another-extension'},()=>{}),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        for mode in ("chromium-browser-alias", "firefox"):
            with self.subTest(browser=mode):
                result = subprocess.run(
                    [
                        "node",
                        "-e",
                        javascript,
                        str(ROOT / "extension/service-worker.js"),
                        mode,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_latency_button_supports_groups_without_now_and_recovers_after_failure(
        self,
    ):
        javascript = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.handlers={};this.textContent='';this.disabled=false;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 setAttribute(){}
 addEventListener(event,handler){this.handlers[event]=handler;}
 get childElementCount(){return this.children.length;}
}
const root=new Element('div'), errors=[], targets=[];
let fail=false;
const group={name:'RU-ACTIVE',type:'LoadBalance',now:null,choices:[{name:'First'}]};
const ctx={document:{createElement:tag=>new Element(tag)},$:()=>root,t:key=>key,
 state:{profileId:'profile',revision:'1'},groupDelays:new Map(),groupDelayScope:null,
 run:async work=>{try{await work();}catch(error){errors.push(error.message);}},
 send:async(action,payload)=>{assert.equal(action,'proxyDelay');targets.push(payload.proxy);if(fail)throw new Error('offline');return {delay:37};},
 refreshGroups:async()=>ctx.renderGroups([group])};
vm.createContext(ctx);
const source=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(source.slice(source.indexOf('function renderGroups('),source.indexOf('async function refreshGroups(')),ctx);
const button=()=>root.children[0].children.find(node=>node.tag==='button');
const output=()=>root.children[0].children.find(node=>node.tag==='output');
(async()=>{
 ctx.renderGroups([group]);assert(!button().disabled,'No now value must not disable the group test');
 await button().handlers.click();assert.deepEqual(targets,['RU-ACTIVE']);assert.equal(output().textContent,'37 ms');
 fail=true;await button().handlers.click();assert(!button().disabled,'Error must restore the button');assert.deepEqual(errors,['offline']);
 ctx.state.revision='2';ctx.renderGroups([group]);assert.equal(output().textContent,'','New revisions must clear old results');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", javascript, str(ROOT / "extension/popup.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_toolbar_tracks_connection_and_stops_connecting_animation(self):
        javascript = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let icon,title,badge,tick,cleared=false;
const ctx={setInterval:fn=>{tick=fn;return 7;},clearInterval:id=>{assert.equal(id,7);cleared=true;},
chrome:{i18n:{getMessage:key=>key},action:{setIcon:value=>{icon=value.path;},setTitle:value=>{title=value.title;},setBadgeText:value=>{badge=value.text;},setBadgeBackgroundColor(){}}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+'\nthis.Toolbar=MhmToolbar;',ctx);
const toolbar=new ctx.Toolbar();
toolbar.update({status:'connecting'});assert(icon[16].includes('connecting-0'));tick();assert(icon[16].includes('connecting-1'));
toolbar.update({status:'connected',profileId:'p'},[{id:'p',name:'RU-ACTIVE'}]);assert(cleared);assert(icon[16].includes('connected'));assert(title.includes('RU-ACTIVE'));
toolbar.update({status:'limited'});assert(icon[16].includes('blocked'));assert.equal(badge,'!');
toolbar.update({status:'disconnected'});assert(icon[16].includes('disconnected'));assert.equal(badge,'');
"""
        result = subprocess.run(
            ["node", "-e", javascript, str(ROOT / "extension/background/action.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
