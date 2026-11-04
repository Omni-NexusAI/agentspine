import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

async function fixture({probeResult = Promise.resolve({ok:true}), profileError = null} = {}) {
    const calls = [], ticks = [], probes = [], apiCalls = [];
    const convo = {enabled:true, state:'off', session:null};
    const setup = {config:{roles:{tts:{voice:'clone:one'}}}, dirty:false,
        async load(){}, async useVoice(voice){calls.push(voice); this.config.roles.tts.voice=voice; return true;}};
    const context = vm.createContext({console, AbortController, clearInterval(){}, setInterval(fn){ticks.push(fn);return 1;},
        fetch:(url,options)=>{probes.push({url,options});return probeResult;},
        Alpine:{store:()=>convo}});
    const exports = {
        '/js/AlpineStore.js':{createStore:(_,value)=>value},
        '/js/modals.js':{closeModal:()=>calls.push('close')},
        '/plugins/_convo/webui/setup-store.js':{store:setup},
        '/js/api.js':{callJsonApi:async(url,payload)=>{apiCalls.push({url,payload});if(payload.action==='studio' && profileError)throw profileError;return {ok:true,data:{voices:[{id:'one',voice:'clone:one',name:'One',task_type:'Base'}]}};}},
    };
    const module = new vm.SourceTextModule(await readFile(new URL('../webui/studio-store.js',import.meta.url),'utf8'),{context});
    await module.link(async name=>{
        const values=exports[name];
        const child=new vm.SyntheticModule(Object.keys(values),function(){for(const [key,value] of Object.entries(values))this.setExport(key,value);},{context});
        await child.link(()=>{});await child.evaluate();return child;
    });
    await module.evaluate();
    return {studio:module.namespace.studio,convo,setup,calls,ticks,probes,apiCalls};
}

test('Studio shares the revision-aware response selection and refuses dirty settings', async()=>{
    const f=await fixture();await f.studio.open();
    assert.equal(f.studio.selectedVoice,'clone:one');assert.equal(f.studio.available,true);
    f.setup.dirty=true;await f.studio.useVoice();assert.equal(f.calls.length,0);
    f.setup.dirty=false;await f.studio.useVoice();assert.deepEqual(f.calls,['clone:one']);
    f.studio.back();assert.equal(f.studio.available,false);assert.equal(f.calls.at(-1),'close');
});

test('Studio probe uses exact root route with native same-origin fetch after studio_open',async()=>{
    const f=await fixture();await f.studio.open();
    assert.equal(f.apiCalls[0].payload.action,'studio_open');
    assert.equal(f.probes.length,1);
    assert.equal(f.probes[0].url,'/convo-studio/');
    assert.equal(f.studio.frameUrl,f.probes[0].url);
    assert.equal(f.probes[0].options.credentials,'same-origin');
    assert.equal(f.probes[0].options.redirect,'error');
    assert.equal(f.probes[0].options.cache,'no-store');
    assert.equal(f.probes[0].options.signal.aborted,false);
    assert.ok(f.apiCalls.every(call=>call.url!== '/convo-studio/' && call.url!== '/api/convo-studio/'));
    f.studio.close();assert.equal(f.probes[0].options.signal.aborted,true);
});

test('Missing managed profile service is concise, preserves saved voice and disables selection',async()=>{
    const f=await fixture({profileError:new Error('HTTP400 Traceback Authorization: Bearer secret-test-value')});
    await f.studio.open();
    assert.equal(f.studio.available,true);
    assert.equal(f.studio.error,'Studio interface is ready, but its Convo-owned audio/profile backend is not configured or reachable. External speech connections do not supply the managed Studio library.');
    assert.ok(!f.studio.error.includes('Traceback') && !f.studio.error.includes('secret-test-value'));
    assert.equal(f.studio.selectedVoice,'clone:one');
    assert.equal(f.studio.savedVoiceUnavailable,true);assert.equal(f.studio.selectedVoiceAvailable,false);
    await f.studio.useVoice();assert.equal(f.calls.length,0);
    const html=await readFile(new URL('../webui/studio.html',import.meta.url),'utf8');
    assert.ok(html.includes('(saved; unavailable in managed Studio)'));
    assert.ok(html.includes(':disabled="$store.convoStudio.busy || !$store.convoStudio.selectedVoiceAvailable"'));
});

test('Close before an outstanding probe finishes never resurrects iframe', async()=>{
    let release;const probeResult=new Promise(resolve=>{release=resolve;});
    const f=await fixture({probeResult});const pending=f.studio.open();
    await Promise.resolve();f.studio.close();release({ok:true});await pending;
    assert.equal(f.studio.available,false);
});

test('Active voice or native OFF immediately removes Studio playback surface',async()=>{
    const f=await fixture();await f.studio.open();assert.equal(f.studio.available,true);
    f.convo.state='connecting';f.ticks.at(-1)();assert.equal(f.studio.available,false);assert.equal(f.studio.blocked,true);
    f.convo.state='off';await f.studio.retry();assert.equal(f.studio.available,true);
    f.convo.enabled=false;f.ticks.at(-1)();assert.equal(f.studio.available,false);
});
