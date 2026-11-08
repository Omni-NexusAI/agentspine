import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const ok = (data = {}) => ({results: [{handlerId: 'Realtime', ok: true, data: {ok: true, ...data}}]});
const refused = flags => ({results: [{handlerId: 'Realtime', ok: true, data: {ok: false, ...flags}}]});
const settle = (ms = 0) => new Promise(r => setTimeout(r, ms));

async function fixture(t, {respond} = {}) {
    const calls=[], microphones=[], handlers={}; let chat='a';
    const context=vm.createContext({console,crypto:globalThis.crypto,setTimeout,clearTimeout,
        fetch:async()=>({ok:true}),document:{getElementById:()=>null}});
    const socket={addHandlers(){},async on(name,fn){handlers[name]=fn;},onDisconnect(){},disconnect(){},
        async request(event,data){calls.push({event,data});return respond?.(event,data,calls) || ok({session:{id:'s',epoch:0,target:'a'}});}};
    class Audio {
        constructor(options){Object.assign(this,options);microphones.push(this);}
        async start(){}
        interrupt(){calls.push({event:'playback.interrupt'});}
        setPolicyHold(value){calls.push({event:'duck',value});}
        pcmBegin(){} pcmChunk(){} pcmEnd(){} pcmAbort(){} enqueue(){}
        close(){}
    }
    const values={
        '/js/AlpineStore.js':{createStore:(_,s)=>s},
        '/js/api.js':{callJsonApi:async(_,data)=>data.action==='status'?{settings:{enabled:true,max_audio_seconds:20}}:{events:[],jobs:[]}},
        '/js/websocket.js':{createNamespacedClient:()=>socket},
        '/js/shortcuts.js':{getCurrentContextId:()=>chat,getCurrentContext:()=>({id:chat}),openModal:async()=>{}},
        './audio.js':{AudioSession:Audio},
        './view-helpers.js':await import('../webui/view-helpers.js'),   // pure and import-free: the real modules
        './orb.js':await import('../webui/orb.js'),
        './dictation.js':await import('../webui/dictation.js'),
        '/plugins/_whisper_stt/webui/whisper-stt-store.js':{store:{initRuntime:async()=>{},getSelectedDevice:()=>({})}},
    };
    const modules=new Map();
    async function resolve(name){
        if(!modules.has(name)){
            const exports=values[name];assert.ok(exports,name);
            const module=new vm.SyntheticModule(Object.keys(exports),function(){for(const[k,v]of Object.entries(exports))this.setExport(k,v);},{context});
            modules.set(name,module);await module.link(()=>{});await module.evaluate();
        }
        return modules.get(name);
    }
    const source=await readFile(new URL('../webui/convo-store.js',import.meta.url),'utf8');
    const module=new vm.SourceTextModule(source,{context,importModuleDynamically:resolve});
    await module.link(resolve);await module.evaluate();
    const store=module.namespace.store;await store.start();
    t.after(() => store.stop());                                      // a failed assertion must not leave a live session (and its timers) behind
    store.holdSafetyMs=40; store.busyRetryMs=[5,5,5];                 // the real schedule is seconds long; the logic is what is tested
    return {store,calls,mic:microphones[0],emit:data=>handlers['convo.event']({data}),setChat:id=>{chat=id;},
        ducks:()=>calls.filter(c=>c.event==='duck').map(c=>c.value),
        sent:()=>calls.filter(c=>c.event==='convo.candidate').map(c=>c.data)};
}

test('Live candidates never interrupt until bound semantic admission; old event cannot revoke newer playback',async t=>{
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('bounded-wave');
    await new Promise(r=>setTimeout(r,0));
    const request=f.calls.find(c=>c.event==='convo.candidate').data;
    assert.equal(request.final,false);assert.equal(request.sequence,1);
    assert.equal(f.calls.filter(c=>c.event==='playback.interrupt').length,0);
    assert.equal(f.calls.filter(c=>c.event==='convo.interrupt').length,0);
    await f.emit({session_id:'s',epoch:0,turn_id:request.turn_id,type:'candidate.state',sequence:1,state:'ignore',final:false});
    assert.equal(f.calls.at(-1).value,false);
    await f.emit({session_id:'s',epoch:1,previous_epoch:0,new_session:{id:'s',epoch:1,target:'a'},turn_id:request.turn_id,type:'candidate.admitted',sequence:1});
    assert.equal(f.store.session.epoch,1);
    assert.equal(f.calls.filter(c=>c.event==='playback.interrupt').length,1);
    f.mic.onTurn('final-wave');await new Promise(r=>setTimeout(r,0));
    const final=f.calls.filter(c=>c.event==='convo.candidate').at(-1).data;
    assert.equal(final.turn_id,request.turn_id);assert.equal(final.epoch,1);assert.equal(final.final,true);
    await f.emit({session_id:'s',epoch:1,previous_epoch:0,new_session:{id:'s',epoch:1},turn_id:request.turn_id,type:'candidate.admitted',sequence:1});
    assert.equal(f.calls.filter(c=>c.event==='playback.interrupt').length,1);
    await f.store.stop();assert.equal(f.store.session,null);
});

// ---- BP3b: the duck is a promise that a decision is coming, and it never outlives that decision

// A completed turn whose decision is "withhold" (uncertain speech, e.g. a low-confidence classifier).
async function decidedFinal(t, state, options) {
    const f=await fixture(t, options);f.mic.onStart();f.mic.onProbe('probe-wave');
    await settle();
    const probe=f.sent()[0];
    f.mic.onTurn('final-wave');await settle();
    const final=f.sent().at(-1);
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:final.sequence,state:'considering',final:true});
    assert.equal(f.ducks().at(-1),true);                                 // while the decision is pending the assistant is ducked
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:final.sequence,state,final:true});
    await settle(20);
    return {f,probe,final};
}

test('BP3b: a withheld final releases the duck once the decision is terminal',async t=>{
    const {f}=await decidedFinal(t,'withhold');
    assert.equal(f.ducks().at(-1),false);
    assert.equal(f.store.policyHold,false);                              // the orb sees it too
    await f.store.stop();
});

test('BP3b: a degraded final is not answered, says so, and does not leave the assistant ducked',async t=>{
    const {f}=await decidedFinal(t,'degraded');
    assert.equal(f.ducks().at(-1),false);
    assert.match(f.store.error,/this turn was not answered/);
    assert.doesNotMatch(f.store.error,/ducked/);
    await f.store.stop();
});

test('BP3b: a withheld PROBE (speech still going on) stays ducked until a decision or the user stops',async t=>{
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('probe-wave');await settle();
    const probe=f.sent()[0];
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:1,state:'withhold',final:false});
    assert.equal(f.ducks().at(-1),true);
    await settle(120);                                                   // far longer than the safety interval, but the user is still speaking
    assert.equal(f.store.policyHold,true);
    assert.equal(f.ducks().at(-1),true);
    await f.store.stop();
});

test('BP3b: an older final ending does not release the duck while a newer utterance is open or waiting',async t=>{
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('probe-wave');await settle();
    const first=f.sent()[0];
    f.mic.onTurn('final-wave');await settle();
    const final=f.sent().at(-1);
    f.mic.onStart();                                                     // the user speaks again before the decision arrives
    await f.emit({session_id:'s',epoch:0,turn_id:first.turn_id,type:'candidate.state',sequence:final.sequence,state:'withhold',final:true});
    assert.equal(f.ducks().at(-1),true);                                 // the newer utterance decides
    f.mic.onProbe('second-probe');await settle();
    const second=f.sent().at(-1);
    assert.notEqual(second.turn_id,first.turn_id);
    f.mic.onTurn('second-final');await settle();
    const secondFinal=f.sent().at(-1);
    await f.emit({session_id:'s',epoch:0,turn_id:second.turn_id,type:'candidate.state',sequence:secondFinal.sequence,state:'ignore',final:true});
    assert.equal(f.ducks().at(-1),false);
    await f.store.stop();
});

test('BP3b: if no decision ever arrives the duck still ends, once the user has stopped and nothing is waiting',async t=>{
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('probe-wave');await settle();
    const probe=f.sent()[0];
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:1,state:'considering',final:false});
    assert.equal(f.store.policyHold,true);
    await settle(100);
    assert.equal(f.store.policyHold,true);                               // still speaking: the timer waits
    f.mic.onTurn('final-wave');await settle();                           // the final is sent, and the server never answers it
    await settle(150);
    assert.equal(f.store.policyHold,false);
    assert.equal(f.ducks().at(-1),false);
    await f.store.stop();
});

test('BP3b: a new reply is never born ducked by a decision that already ended, but a live utterance keeps the duck',async t=>{
    const reply=turn=>({session_id:'s',epoch:0,turn_id:turn,type:'audio.begin',phrase_id:'p',format:'pcm_s16le',channels:1,sample_rate:24000});
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('probe-wave');await settle();
    const probe=f.sent()[0];
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:1,state:'withhold',final:false});
    await f.emit(reply('r1'));
    assert.equal(f.store.policyHold,true);                               // the user is mid-utterance: ducked
    f.mic.onTurn('final-wave');await settle();
    f.store.holdSafetyMs=60000;                                           // only the reply, not the timer, may release it here
    await f.emit(reply('r2'));
    assert.equal(f.store.policyHold,false);
    await f.store.stop();
});

test('A stopped session cancels the safety timer instead of ducking a closed one',async t=>{
    const f=await fixture(t);f.mic.onStart();f.mic.onProbe('probe-wave');await settle();
    const probe=f.sent()[0];
    await f.emit({session_id:'s',epoch:0,turn_id:probe.turn_id,type:'candidate.state',sequence:1,state:'considering',final:false});
    await f.store.stop();
    const before=f.calls.length;
    await settle(120);
    assert.equal(f.calls.length,before);                                 // no late hold(false) on a torn-down session
    assert.equal(f.store.policyHold,false);
});

// ---- BP3c: busy and stale are ordinary answers, not an outage

test('BP3c: a probe refused as busy is replaced by the next one, ducks, and is not reported as an outage',async t=>{
    const f=await fixture(t,{respond:event=>event==='convo.candidate'?refused({busy:true}):null});
    f.mic.onStart();f.mic.onProbe('p1');await settle(10);
    assert.equal(f.store.error,'');
    assert.equal(f.ducks().at(-1),true);                                 // undecided speech: conservative, not alarming
    f.mic.onProbe('p2');await settle(10);
    assert.equal(f.sent().length,2);                                     // the next probe was still tried (nothing latched)
    assert.equal(f.store.error,'');
    await f.store.stop();
});

test('BP3c: a stale answer (the epoch moved on) is dropped silently',async t=>{
    const f=await fixture(t,{respond:event=>event==='convo.candidate'?refused({stale:true}):null});
    f.mic.onStart();f.mic.onProbe('p1');await settle(10);
    assert.equal(f.store.error,'');
    assert.equal(f.ducks().includes(true),false);
    await f.store.stop();
});

test('BP3c: any other refusal is still reported as the outage it is, and ducks',async t=>{
    const f=await fixture(t,{respond:event=>event==='convo.candidate'?refused({}):null});
    f.mic.onStart();f.mic.onProbe('p1');await settle(10);
    assert.match(f.store.error,/Turn policy unavailable/);
    assert.equal(f.ducks().at(-1),true);
    await f.store.stop();
});

test('BP3c: a completed turn refused as busy is sent again until the server is free, and is never lost',async t=>{
    let finals=0;
    const f=await fixture(t,{respond:(event,data)=>event==='convo.candidate'&&data.final&&++finals<=2?refused({busy:true}):null});
    f.mic.onStart();f.mic.onProbe('probe');await settle();
    f.mic.onTurn('final-wave');await settle(60);
    const attempts=f.sent().filter(s=>s.final);
    assert.equal(attempts.length,3);                                     // busy, busy, accepted
    assert.deepEqual([...new Set(attempts.map(s=>s.turn_id))].length,1);
    assert.deepEqual([...new Set(attempts.map(s=>s.sequence))].length,1);// the same observation, not a new one
    assert.equal(f.store.error,'');
    await f.store.stop();
});

test('BP3c: a completed turn that stays busy is reported as not answered, and releases the duck',async t=>{
    const f=await fixture(t,{respond:(event,data)=>event==='convo.candidate'&&data.final?refused({busy:true}):null});
    f.mic.onStart();f.mic.onProbe('probe');await settle();
    f.mic.onTurn('final-wave');await settle(80);
    assert.equal(f.sent().filter(s=>s.final).length,4);                  // the first try and the three retries of this test's schedule
    assert.match(f.store.error,/still busy with an earlier turn, so this one was not answered/);
    assert.doesNotMatch(f.store.error,/policy unavailable/);
    assert.equal(f.ducks().at(-1),false);
    await f.store.stop();
});

test('BP3c: the retry schedule is bounded and short enough to matter',async t=>{
    const f=await fixture(t);
    const fresh=Object.getPrototypeOf(f.store)===Object.prototype?f.store:f.store;
    assert.ok(Array.isArray(fresh.busyRetryMs));
    const source=await readFile(new URL('../webui/convo-store.js',import.meta.url),'utf8');
    const schedule=source.match(/busyRetryMs: \[([\d, ]+)\]/)[1].split(',').map(Number);
    assert.ok(schedule.length<=6&&schedule.reduce((a,b)=>a+b,0)<=6000,'a turn waits for a free server for at most a few seconds');
    await f.store.stop();
});

test('A complaint about having no chat clears itself once a chat is chosen, and no other error is touched',async t=>{
    const f=await fixture(t);
    await f.store.stop(); f.setChat('');
    await f.store.start();
    assert.equal(f.store.error,'Select or create a chat first.');
    f.setChat('b'); await f.store.refresh();
    assert.equal(f.store.error,'');                                       // a chat is selected now: the complaint was true, is no longer
    f.store.error='Voice disconnected. Jobs continue in their target chats.';
    await f.store.refresh();
    assert.equal(f.store.error,'Voice disconnected. Jobs continue in their target chats.');   // anything else stays until acted on
    await f.store.stop();
});

test('A refused start shows the reason the server gave, not a generic fault',async t=>{
    const reason='The conversation model takes audio, so the turn-policy classifier must too: enable its audio input.';
    const f=await fixture(t,{respond:event=>event==='convo.start'?refused({refused:true,message:reason}):null});
    assert.equal(f.store.error,reason);
    assert.equal(f.store.active,false);
    assert.equal(f.store.busy,false);
    assert.equal(f.store.state,'off');
});

test('A host-masked fault still reads as one, and a stale or busy answer is not mistaken for a refusal',async t=>{
    const f=await fixture(t,{respond:event=>event==='convo.start'?{results:[{handlerId:'Realtime',ok:false,error:{code:'HANDLER_ERROR',error:'Internal server error'}}]}:null});
    assert.equal(f.store.error,'Internal server error');
    const g=await fixture(t,{respond:event=>event==='convo.start'?refused({stale:true}):null});
    assert.equal(g.store.error,'Stale voice operation withheld.');
});
