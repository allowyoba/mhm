from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node required")
class BrowserSessionTests(unittest.TestCase):
    def test_last_window_stops_pending_connection_and_requires_new_session(self):
        script = r"""
const vm=require('vm'),fs=require('fs'),path=require('path'),assert=require('assert');
let listener,onRemoved,onCreated,onStartup,onAlarm,windowCount=1,core=false,ports=0,proxy=null,hold=false,release,holdProxy=false,releaseProxy,privateAccess=false;
const storage={mhmConnection:{active:true}};
const ctx={console,setTimeout,clearTimeout,Map,Set,Date,importScripts(...files){for(const file of files)vm.runInContext(fs.readFileSync(path.join(path.dirname(process.argv[1]),file),'utf8'),ctx);},
 chrome:{storage:{local:{get:async()=>storage,set:async x=>Object.assign(storage,x)}},
 windows:{getAll:async()=>Array.from({length:windowCount},()=>({type:'normal'})),onRemoved:{addListener:f=>onRemoved=f},onCreated:{addListener:f=>onCreated=f}},
 proxy:{settings:{set:async x=>{if(holdProxy&&x.value.http.endsWith(":10808"))await new Promise(resolve=>releaseProxy=resolve);proxy=x.value},clear:async()=>{proxy=null},get:async()=>({levelOfControl:'controlled_by_this_extension'})}},
 extension:{isAllowedIncognitoAccess:async()=>privateAccess},
 alarms:{create:async()=>{},onAlarm:{addListener:f=>onAlarm=f}},tabs:{create:async()=>{}},
 runtime:{id:'test',getBrowserInfo:async()=>({name:'Firefox'}),onMessage:{addListener:f=>listener=f},onInstalled:{addListener(){}},onStartup:{addListener:f=>onStartup=f},sendMessage:async()=>{},
 connectNative(){ports++;let message,disconnected;return {onMessage:{addListener:f=>message=f},onDisconnect:{addListener:f=>disconnected=f},disconnect(){core=false;disconnected();},postMessage(request){
 let data={};
 if(request.command==='bootstrap')data={profiles:[],status:{status:'disconnected'}};
 if(request.command==='connect'){core=true;data={status:'connected',profileId:'p',localPort:10808};}
 if(request.command==='saveSettings')data={settings:{excludedSubnets:request.excludedSubnets}};
 if(request.command==='saveMihomoProfile')data={profiles:[{id:'p',name:'First'}],profile:{id:'p'}};
 const reply=()=>message({ok:true,requestId:request.requestId,...data});
 if(request.command==='connect'&&hold)release=reply;else queueMicrotask(reply);
 }}}}}};
ctx.browser=ctx.chrome;vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const send=action=>new Promise(resolve=>listener(action,{id:'test'},resolve));
(async()=>{
 if(process.argv[2]==='no-windows'){windowCount=0;let result=await send({action:'initialize'});assert.equal(result.errorCode,'SESSION_CLOSED');assert.equal(ports,0,'a restarted background worker must not start a native host without browser windows');await onAlarm({name:'profile-updates'});assert.equal(ports,0);windowCount=1;onCreated();}
 await send({action:'initialize'});privateAccess=false;
 assert((await send({action:'saveMihomoProfile',name:'First',yaml:'rules: [MATCH,DIRECT]'})).ok,'saving a first profile needs no debugger or proxy access');
 let result=await send({action:'connect',profileId:'p'});assert.equal(result.errorCode,'FIREFOX_PRIVATE_ACCESS');assert(!core);
 privateAccess=true;result=await send({action:'connect',profileId:'p'});assert(result.ok);assert(core);
 await send({action:'saveSettings',excludedSubnets:['10.0.0.0/8','fd00::/8']});assert(proxy.passthrough.includes('10.0.0.0/8'));assert(proxy.passthrough.includes('fd00::/8'));assert.deepEqual(storage.mhmProxyExclusions,['10.0.0.0/8','fd00::/8']);
 windowCount=2;await onRemoved();assert(core,'closing one of several windows must keep the connection');
 hold=true;const connecting=send({action:'connect',profileId:'p'});await new Promise(setImmediate);
 windowCount=0;await onRemoved();result=await connecting;assert.equal(result.errorCode,'SESSION_CLOSED');assert(!core);assert.equal(proxy,null);assert.equal(storage.mhmConnection.active,false);
 const count=ports;release();await new Promise(setImmediate);await onAlarm({name:'profile-updates'});assert.equal(ports,count,'alarms must not reopen a closed session');
 result=await send({action:'connect',profileId:'p'});assert.equal(result.errorCode,'SESSION_CLOSED');assert.equal(storage.mhmConnection.active,false);
 windowCount=1;onCreated();hold=false;assert((await send({action:'connect',profileId:'p'})).ok);assert(core);assert.equal(ports,count+1);

 holdProxy=true;const pendingProxy=send({action:'connect',profileId:'p'});await new Promise(setImmediate);assert(releaseProxy);
 windowCount=0;const closing=onRemoved();await new Promise(setImmediate);assert(!core,'native port must close before a delayed proxy update');releaseProxy();await closing;
 result=await pendingProxy;assert.equal(result.errorCode,'SESSION_CLOSED');assert.equal(proxy,null);assert.equal(storage.mhmConnection.active,false);assert.equal(vm.runInContext('state.status',ctx),'disconnected');
 windowCount=1;onCreated();holdProxy=false;assert((await send({action:'connect',profileId:'p'})).ok);
 await onStartup();assert(!core);assert.equal(proxy,null);assert.equal(storage.mhmConnection.active,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        for mode in ("open", "no-windows"):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    [
                        "node",
                        "-e",
                        script,
                        str(ROOT / "extension/service-worker.js"),
                        mode,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
