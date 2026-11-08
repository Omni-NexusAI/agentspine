import assert from 'node:assert/strict';
import {test} from 'node:test';
import {loadModule, hostStubs} from './vm-loader.mjs';
import {settingsMarkup} from './settings-markup.mjs';

async function fixture(handler) {
    const calls=[];
    const config={roles:{conversation:{url:'http://local/v1',model:'saved'},classifier:{},tts:{voice:'clone:old'}}};
    const result=()=>({ok:true,config:structuredClone(config),revision:'r1',readiness:[]});
    const bindings=hostStubs({'/js/api.js':{callJsonApi:async(_,payload)=>{calls.push(structuredClone(payload)); return handler ? handler(payload,result) : result();}}});
    const {namespace}=await loadModule('webui/setup-store.js',{bindings,globals:{window:{confirm:()=>true}}});
    return {store:namespace.store,calls};
}

test('Setup read does not discover endpoints or start resources',async()=>{
    const f=await fixture();await f.store.load();await f.store.load();
    assert.deepEqual(f.calls.map(c=>c.action),['setup_read']);assert.equal(f.store.dirty,false);
    assert.equal(f.store.options('conversation')[0].id,'saved');
});
test('Save includes revision and keys separately; successful save clears keys',async()=>{
    const f=await fixture();await f.store.load();f.store.keys.conversation='test-key';f.store.config.style='brief';await f.store.save();
    const call=f.calls.at(-1);assert.equal(call.revision,'r1');assert.equal(call.keys.conversation,'test-key');
    assert.equal(JSON.stringify(call.config).includes('test-key'),false);assert.equal(Object.keys(f.store.keys).length,0);
});
test('Conflict preserves user draft and error',async()=>{
    const f=await fixture((payload,read)=>payload.action==='setup_save'?{ok:false,error:'Settings changed in another view',code:'conflict'}:read());
    await f.store.load();f.store.config.style='unsaved';assert.equal(await f.store.save(),false);
    assert.equal(f.store.config.style,'unsaved');assert.equal(f.store.dirty,true);assert.match(f.store.error,/another view/);
});
test('Discovery is explicit and ignores a response for an edited provider',async()=>{
    let resolve;
    const f=await fixture((payload,read)=>payload.action==='discover'?new Promise(r=>resolve=r):read());
    await f.store.load();const pending=f.store.discover('conversation');
    f.store.config.roles.conversation.url='http://different/v1';resolve({ok:true,models:[{id:'stale'}]});await pending;
    assert.equal(f.store.options('conversation').some(x=>x.id==='stale'),false);
});
test('Import requires explicitly selected paths and uses preview revision',async()=>{
    const f=await fixture((payload,read)=>payload.action==='import_preview'?{ok:true,revision:'preview',changes:[{path:'roles.tts.voice'}],warnings:[]}:read());
    await f.store.load();await f.store.previewImport({size:10,text:async()=>'{}'});
    assert.equal(f.store.importPaths.length,0);f.store.importPaths=['roles.tts.voice'];await f.store.applyImport();
    assert.equal(f.calls.at(-1).revision,'preview');assert.deepEqual(f.calls.at(-1).paths,['roles.tts.voice']);
});
test('Studio selection refuses to silently save unrelated pending edits',async()=>{
    const f=await fixture();await f.store.load();f.store.config.style='pending';assert.equal(await f.store.useVoice('clone:new'),false);
    assert.equal(f.calls.filter(c=>c.action==='setup_save').length,0);assert.equal(f.store.config.roles.tts.voice,'clone:old');
});

test('Empty key input preserves saved credentials; explicit clear is dirty and sent',async()=>{
    const f=await fixture();await f.store.load();f.store.setKey('tts','');assert.equal(f.store.dirty,false);
    f.store.clearKey('tts');assert.equal(f.store.dirty,true);await f.store.save();assert.equal(f.calls.at(-1).keys.tts,'');
});

test('Edits made during save remain dirty instead of disappearing',async()=>{
    let resolve;
    const f=await fixture((payload,read)=>payload.action==='setup_save'?new Promise(r=>resolve=()=>r({...read(),config:payload.config,revision:'r2'})):read());
    await f.store.load();f.store.config.style='first';const pending=f.store.save();f.store.config.style='second';resolve();await pending;
    assert.equal(f.store.config.style,'second');assert.equal(f.store.revision,'r2');assert.equal(f.store.dirty,true);
});

test('Tuning choices preserve unavailable saved profile and save its own field',async()=>{
    const f=await fixture();await f.store.load();f.store.config.roles.tts.tuning_profile='saved-tuning';
    f.store.choices.tts={tuning:[{id:'fast',label:'Fast'}]};
    assert.equal(f.store.options('tts','tuning')[0].id,'saved-tuning');
    assert.equal(f.store.options('tts','tuning').some(value=>value.id==='clone:old'),false);
    f.store.config.roles.tts.tuning_profile='fast';await f.store.save();
    assert.equal(f.calls.at(-1).config.roles.tts.tuning_profile,'fast');
    assert.equal(f.calls.at(-1).config.roles.tts.voice,'clone:old');
});

test('Initial discovery button bindings return false, never undefined',async()=>{
    const f=await fixture();await f.store.load();
    const html=await settingsMarkup();   // the panel no longer hosts settings; the modal and Config share the settings components
    const bindings=[...html.matchAll(/:disabled="([^"]*isDiscovering[^\"]*)"/g)].map(match=>match[1]);
    assert.ok(bindings.length,'the settings have explicit discovery state bindings');
    for (const expression of bindings) {
        const evaluate=new Function('$store','role','return '+expression);
        assert.equal(evaluate({convoSetup:f.store},'conversation'),false);
    }
    f.store.choices.conversation={busy:true};assert.equal(f.store.isDiscovering('conversation'),true);
    f.store.choices.conversation={};assert.equal(f.store.isDiscovering('conversation'),false);
});
