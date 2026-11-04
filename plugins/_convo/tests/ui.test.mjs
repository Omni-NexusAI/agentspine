import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

async function fixture({ startBarrier = null, captureBarrier = null } = {}) {
    const calls = [], sessions = [], microphones = [], listeners = {}, playback = [];
    let selected = 'a', epoch = 0, session = 'session-a', statusError = false;
    const nativeMic = { status:'LISTENING', dispose() { calls.push('native.dispose'); }, async toggle() { calls.push('native.toggle'); }, async finishConvoDictation() { calls.push('native.finish'); } };
    const nativeStore = { microphoneInput: null, getSelectedDevice:()=>({deviceId:'mic'}), async initRuntime(){}, async initMicrophone(){ this.microphoneInput=nativeMic; calls.push('native.init'); } };
    const context = vm.createContext({ setTimeout, clearTimeout, setInterval:()=>0, clearInterval, console, crypto:globalThis.crypto,
        window: { confirm:()=>true, addEventListener(){} }, document: { hidden:false, addEventListener(type,callback){listeners[type]=callback;}, getElementById(){return null;} },
        fetch:async()=>({ok:true}) });
    class Client {
        addHandlers() {}
        async on(event, cb) { (this.handlers ??= {})[event] = cb; this.receive=cb; }
        onDisconnect(cb) { this.disconnected=cb; }
        disconnect() { calls.push('socket.close'); }
        async request(event, data) {
            calls.push(event);
            if (event === 'convo.playback') playback.push(data);
            if (event === 'convo.start' && startBarrier) await startBarrier;
            if (event === 'convo.interrupt' || event === 'convo.retarget') epoch++;
            return { results:[{handlerId:'plugin.Realtime',ok:true,data:{ok:true,session:{id:session,epoch,target:data.target||selected}}}] };
        }
    }
    class Audio {
        constructor(options) { Object.assign(this, options); microphones.push(this); }
        async start() { calls.push('capture.start'); if(captureBarrier) await captureBarrier; }
        interrupt() { calls.push('playback.interrupt'); }
        close() { this.closed=true; calls.push('capture.close'); }
        enqueue(value, done) { this.done=done; }
        pcmBegin(event, done, started) { this.pcmEvent=event; this.done=done; this.started=started; calls.push('pcm.begin'); }
        pcmChunk() { calls.push('pcm.chunk'); }
        pcmEnd() { calls.push('pcm.end'); }
        pcmAbort() { calls.push('pcm.abort'); }
    }
    const exports = {
        '/js/AlpineStore.js': { createStore:(_,value)=>value },
        '/js/api.js': { callJsonApi:async(_,payload)=>{
            if (statusError) throw new Error('Plugin disabled or unavailable');
            return payload.action==='status' ? {settings:{enabled:true,max_audio_seconds:20,transcription_enabled:false}} : {events:[],jobs:[]};
        } },
        '/js/websocket.js': { createNamespacedClient:()=>{const client=new Client(); sessions.push(client);return client;} },
        '/js/shortcuts.js': {getCurrentContextId:()=>selected,getCurrentContext:()=>({id:selected,name:'Chat '+selected}),openModal:async()=>{}},
        './audio.js': {AudioSession:Audio},
        './view-helpers.js': await import('../webui/view-helpers.js'),   // pure and import-free: the real modules
        './orb.js': await import('../webui/orb.js'),
        './dictation.js': await import('../webui/dictation.js'),
        '/plugins/_whisper_stt/webui/whisper-stt-store.js': {store:nativeStore},
    };
    const modules=new Map();
    async function resolve(specifier) {
        if(!modules.has(specifier)) {
            const values=exports[specifier]; if(!values) throw new Error('Unexpected import '+specifier);
            const module=new vm.SyntheticModule(Object.keys(values),function(){for(const [key,value] of Object.entries(values))this.setExport(key,value);},{context});
            modules.set(specifier,module); await module.link(()=>{}); await module.evaluate();
        }
        return modules.get(specifier);
    }
    const source=await readFile(new URL('../webui/convo-store.js',import.meta.url),'utf8');
    const module=new vm.SourceTextModule(source,{context,importModuleDynamically:resolve});
    await module.link(resolve); await module.evaluate();
    return {store:module.namespace.store,install:module.namespace.install,listeners,calls,sessions,microphones,playback,nativeStore,setTarget:v=>selected=v,failStatus:()=>statusError=true};
}

test('Start, interrupt, stop have one owned socket and microphone',async()=>{
    const f=await fixture(); await f.store.start();
    assert.equal(f.microphones.length,1); assert.equal(f.store.session.id,'session-a');
    await f.store.interrupt(); assert.equal(f.store.session.epoch,1);
    await f.store.stop(); assert.equal(f.store.session,null); assert.equal(f.microphones[0].closed,true);
    assert.ok(f.calls.includes('convo.stop'));
});

test('PCM events and acknowledgements are session/epoch gated and abort reaches owned sink',async()=>{
    const f=await fixture();await f.store.start();
    const receive=f.sessions[0].receive;
    const event={session_id:'session-a',epoch:0,turn_id:'t',phrase_id:'p'};
    receive({data:{...event,type:'audio.begin'}});receive({data:{...event,type:'audio.chunk'}});
    receive({data:{...event,type:'audio.end'}});receive({data:{...event,type:'audio.abort'}});
    assert.deepEqual(f.calls.filter(v=>v.startsWith('pcm.')),['pcm.begin','pcm.chunk','pcm.end','pcm.abort']);
    assert.equal(f.playback.length,0);
    f.microphones[0].started();await Promise.resolve();assert.equal(f.playback[0].status,'started');
    await f.store.interrupt();
    f.microphones[0].done();receive({data:{...event,type:'audio.chunk'}});
    assert.equal(f.playback.length,1);assert.equal(f.calls.filter(v=>v==='pcm.chunk').length,1);
    await f.store.stop();
});

test('Opening native sidebar selects the current chat without starting capture', async()=>{
    const f=await fixture(); await f.store.mountPanel();
    assert.equal(f.store.target,'a'); assert.equal(f.store.targetLabel,'Chat a');
    assert.equal(f.microphones.length,0); assert.equal(f.sessions.length,0);
});

test('Text chat selection does not silently retarget an active conversation', async()=>{
    const f=await fixture(); await f.store.start(); f.setTarget('b'); await f.store.refresh();
    assert.equal(f.store.target,'a'); assert.equal(f.store.targetLabel,'Chat a'); await f.store.stop();
});

test('A half-second hold is no longer dictation; a tap toggles once and its release click is swallowed',async()=>{
    // The hold thresholds (about 2 s latched, 4 s push-to-talk) are covered on a virtual clock in dictation.test.mjs.
    const f=await fixture(); await f.store.refresh(); f.install();
    const event={button:0,pointerId:1,isPrimary:true,target:{closest:()=>({})},preventDefault(){},stopImmediatePropagation(){}};
    f.listeners.pointerdown(event); await new Promise(resolve=>setTimeout(resolve,480));
    assert.equal(f.store.dictating,false); assert.equal(f.store.dictationMode,'');
    f.listeners.pointerup(event); await new Promise(resolve=>setTimeout(resolve,5));      // 480 ms is a tap: Convo starts once
    f.listeners.click(event); await new Promise(resolve=>setTimeout(resolve,5));          // the click after the release must not toggle it off again
    assert.equal(f.store.dictating,false); assert.equal(f.sessions.length,1); assert.equal(f.store.active,true);
    await f.store.stop();
});

test('Delayed transcript updates its original turn without duplicating a bubble',async()=>{
    const f=await fixture();f.store.events=[
        {seq:1,session:'s',turn:'t',kind:'user.accepted',payload:{}},
        {seq:2,session:'s',turn:'t',kind:'assistant.playback_started',payload:{}},
        {seq:3,session:'s',turn:'t',kind:'user.transcript',payload:{text:'Hello'}}
    ];
    assert.equal(f.store.visibleEvents.length,1);assert.equal(f.store.eventText(f.store.visibleEvents[0]),'Hello');
    assert.equal(f.store.events[0].payload.transcript,undefined);
});

test('A late start cannot revive a stopped voice session',async()=>{
    let resolve; const barrier=new Promise(r=>resolve=r);
    const f=await fixture({startBarrier:barrier}); const pending=f.store.start();
    while(!f.calls.includes('convo.start')) await new Promise(r=>setTimeout(r,1));
    await f.store.stop(); resolve(); await pending;
    assert.equal(f.store.session,null); assert.equal(f.microphones.length,0);
});

test('Closing during microphone permission/setup releases late capture',async()=>{
    let resolve; const barrier=new Promise(r=>resolve=r);
    const f=await fixture({captureBarrier:barrier}); const pending=f.store.start();
    while(!f.calls.includes('capture.start')) await new Promise(r=>setTimeout(r,1));
    await f.store.stop(); resolve(); await pending;
    assert.equal(f.store.session,null); assert.equal(f.microphones[0].closed,true);
});

test('Retarget keeps the voice session and discards the old capture owner',async()=>{
    const f=await fixture(); await f.store.start(); f.setTarget('b'); await f.store.retarget();
    assert.equal(f.store.target,'b'); assert.equal(f.store.session.id,'session-a');
    assert.equal(f.store.session.epoch,1); assert.equal(f.microphones[0].closed,true);
    assert.equal(f.calls.filter(x=>x==='convo.start').length,1); await f.store.stop();
});

test('Explicit dictation flushes through native recorder before releasing it',async()=>{
    const f=await fixture(); await f.store.beginDictation(); assert.equal(f.store.dictating,true);
    await f.store.endDictation(); assert.equal(f.store.dictating,false); assert.ok(f.calls.includes('native.finish'));
    assert.equal(f.nativeStore.microphoneInput,null);
});

test('Losing the disabled plugin API releases microphone and playback',async()=>{
    const f=await fixture(); await f.store.start(); f.failStatus(); await f.store.refresh();
    assert.equal(f.microphones[0].closed,true); assert.equal(f.store.active,false);
    assert.equal(f.store.enabled,false); assert.ok(f.calls.includes('socket.close'));
});

test('Server disable releases capture and leaves explicit native dictation usable',async()=>{
    const f=await fixture(); await f.store.start();
    f.sessions[0].handlers['convo.disabled']({});
    while(f.store.busy) await new Promise(r=>setTimeout(r,1));
    assert.equal(f.microphones[0].closed,true); assert.equal(f.store.active,false);
    await f.store.beginDictation(); assert.equal(f.store.dictating,true); await f.store.endDictation();
});
