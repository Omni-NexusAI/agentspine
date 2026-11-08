import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { AudioSession, DUCK, DuckController, MIN_VOICED_SECONDS, PRE_ROLL_SECONDS, SpeechBoundary, VOICED_RMS, feedBoundary, wavBase64 } from '../webui/audio.js';

test('WAV conversion preserves original sample duration', () => {
    const bytes = Buffer.from(wavBase64([new Float32Array(48000).fill(.25)], 48000), 'base64');
    assert.equal(bytes.subarray(0,4).toString(), 'RIFF');
    assert.equal(bytes.readUInt32LE(24), 16000);
    assert.equal(bytes.length, 32044);
    assert.equal(bytes.readInt16LE(44), 8192);
});

test('Admission requires 450ms and retains pre-admission original samples', () => {
    let starts = 0; const turns = [];
    const boundary = new SpeechBoundary({rate: 16000, onStart: () => starts++, onTurn: v => turns.push(v)});
    const speech = new Float32Array(1600).fill(.1);
    for (let n=0;n<4;n++) boundary.push(speech);
    assert.equal(starts, 0);
    boundary.push(speech); assert.equal(starts, 1);
    for (let n=0;n<10;n++) boundary.push(new Float32Array(1600));
    assert.equal(turns.length, 1); assert.equal(turns[0][0], speech);
});

test('Short noises and idle silence produce no uploads', () => {
    let turns = 0, starts = 0;
    const boundary = new SpeechBoundary({rate:16000, onStart:()=>starts++, onTurn:()=>turns++});
    boundary.push(new Float32Array(1600).fill(.1));
    for (let n=0;n<100;n++) boundary.push(new Float32Array(1600));
    assert.equal(starts, 0); assert.equal(turns, 0); assert.equal(boundary.chunks.length, 0);
});

test('Visual callback failure cannot prevent microphone release',()=>{
    let released=0;
    const audio=new AudioSession({onLevel:()=>{throw new Error('visual failed');}});
    audio.stream={getTracks:()=>[{stop:()=>released++}]};
    audio.emitLevel('input',.5);audio.close();assert.equal(released,1);assert.equal(audio.closed,true);
});

test('Interruption clears playback meter and notifies zero output',()=>{
    const levels=[];let disconnected=0;
    const audio=new AudioSession({onLevel:(kind,value)=>levels.push([kind,value])});
    audio.outputMeter={disconnect:()=>disconnected++};audio.levelTimer=setInterval(()=>{},1000);
    audio.interrupt();assert.equal(disconnected,1);assert.equal(audio.levelTimer,null);
    assert.deepEqual(levels,[['output',0]]);
});

test('Candidate probes use cumulative existing samples without becoming turns', () => {
    const probes = [], turns = [];
    const boundary = new SpeechBoundary({rate:16000,onStart:()=>{},onProbe:v=>probes.push(v),onTurn:v=>turns.push(v)});
    const chunk = new Float32Array(1600).fill(.1);
    for (let n=0;n<20;n++) boundary.push(chunk);
    assert.ok(probes.length >= 2); assert.equal(turns.length,0);
    assert.equal(probes[0][0],chunk); assert.ok(probes[1].length > probes[0].length);
    for (let n=0;n<10;n++) boundary.push(new Float32Array(1600));
    assert.equal(turns.length,1);
});

test('The maximum-length cut is exact to the sample and the remainder of that block starts the next utterance', () => {
    const turns = [], starts = [];
    const boundary = new SpeechBoundary({rate: 16000, maximum: 1, onStart: () => starts.push(1), onTurn: v => turns.push(v)});
    const block = new Float32Array(3000).fill(.1);                       // 16000 is not a multiple of 3000: the cut is inside the sixth block
    for (let n = 0; n < 6; n++) boundary.push(block);
    assert.equal(turns.length, 1);
    assert.equal(turns[0].reduce((n, chunk) => n + chunk.length, 0), 16000);   // exactly the maximum, not 18000
    assert.equal(boundary.lastTurn.cut, true);
    const carried = boundary.chunks.reduce((n, chunk) => n + chunk.length, 0);
    assert.equal(carried, 2000);                                          // 6 x 3000 - 16000: nothing was dropped
    assert.equal(starts.length, 1);                                       // the carried audio is a new, not yet admitted, utterance
});

test('A block that ends exactly on the maximum cuts without carrying anything, and an idle silent block is dropped whole', () => {
    const turns = [];
    const boundary = new SpeechBoundary({rate: 16000, maximum: 1, onStart: () => {}, onTurn: v => turns.push(v)});
    const block = new Float32Array(1600).fill(.1);
    for (let n = 0; n < 10; n++) boundary.push(block);
    assert.equal(turns.length, 1);
    assert.equal(boundary.chunks.length, 0);
    assert.equal(turns[0].length, 10); assert.equal(turns[0][0], block);  // whole blocks are kept by identity when no split is needed
    // A silent block that straddles the maximum while idle is dropped whole, as before.
    const idle = new SpeechBoundary({rate: 16000, maximum: 1, onStart: () => {}, onTurn: () => assert.fail('no turn')});
    idle.push(new Float32Array(40000)); assert.equal(idle.chunks.length, 0);
});

test('A maximum that is not a number never cuts, and a tiny one still terminates', () => {
    const turns = [];
    const unbounded = new SpeechBoundary({rate: 16000, maximum: NaN, onStart: () => {}, onTurn: v => turns.push(v)});
    for (let n = 0; n < 400; n++) unbounded.push(new Float32Array(1600).fill(.1));
    assert.equal(turns.length, 0);
    const tiny = new SpeechBoundary({rate: 16000, maximum: 0, onStart: () => {}, onTurn: v => turns.push(v)});
    tiny.push(new Float32Array(2048).fill(.1));                           // limit clamps to one sample; the loop ends
    assert.ok(tiny.chunks.length <= 1);
});

test('Policy uncertainty ducks existing output without stopping or acknowledging it', () => {
    const audio = new AudioSession({}); let stopped=0;
    audio.outputGain={gain:{value:1}};audio.playing={stop:()=>stopped++};
    audio.queue=[{base64:'pending'}];const generation=audio.playbackGeneration;
    audio.setPolicyHold(true);assert.equal(audio.outputGain.gain.value,.15);
    assert.equal(stopped,0);assert.equal(audio.queue.length,1);assert.equal(audio.playbackGeneration,generation);
    audio.setPolicyHold(false);assert.equal(audio.outputGain.gain.value,.15);   // not at once: the duck has a minimum hold and a release delay
    audio.setPolicyHold(false,true);assert.equal(audio.outputGain.gain.value,1);   // a decision that already ended releases immediately
    audio.duck.dispose();
});

test('Voice start observes speech and only semantic admission cancels playback', () => {
    const store=readFileSync(new URL('../webui/convo-store.js',import.meta.url),'utf8');
    const audio=readFileSync(new URL('../webui/audio.js',import.meta.url),'utf8');
    assert.equal((audio.match(/getUserMedia\(/g)||[]).length,1);
    assert.equal((store.match(/onStart: \(\) => beginCandidate\(attempt\)/g)||[]).length,2);
    assert.doesNotMatch(store,/interrupting = this\.interrupt/);
    assert.match(store,/candidate\.admitted/);
});

function pcmFixture() {
    const events=[], sources=[], errors=[];
    const audio=new AudioSession({onError:error=>errors.push(error.message)});
    audio.context={currentTime:0,state:'running',destination:{},
        decodeAudioData:()=>assert.fail('Raw PCM must not use buffered audio decoder'),
        createBuffer:(_,length,rate)=>({duration:length/rate,values:new Float32Array(length),getChannelData(){return this.values;}}),
        createBufferSource:()=>{const source={connect(){return this;},disconnect(){},start(when){this.when=when;},stop(){this.stopped=true;}};sources.push(source);return source;}};
    const begin={session_id:'s',epoch:0,turn_id:'t',phrase_id:'p',format:'pcm_s16le',channels:1,sample_rate:24000};
    const start=()=>audio.pcmBegin(begin,()=>events.push('completed'),()=>events.push('started'));
    const chunk=(sequence=0,bytes=Buffer.from([0,128,255,127]))=>audio.pcmChunk({...begin,sequence,audio:bytes.toString('base64')});
    const finish=source=>{audio.context.currentTime=source.when+source.buffer.duration;source.onended?.();};
    return {audio,begin,start,chunk,events,sources,errors,finish};
}

test('PCM schedules aligned samples continuously and completes only after end and every source',()=>{
    const f=pcmFixture();try {
        assert.equal(f.start(),true);f.chunk(0);f.chunk(1);
        assert.deepEqual(f.events,[]);assert.equal(f.sources[1].when,f.sources[0].when+f.sources[0].buffer.duration);
        assert.equal(f.sources[0].buffer.values[0],-1);
        f.finish(f.sources[0]);assert.deepEqual(f.events,['started']);
        f.audio.pcmEnd({...f.begin,chunks:2});assert.deepEqual(f.events,['started']);
        f.finish(f.sources[1]);assert.deepEqual(f.events,['started','completed']);assert.equal(f.audio.pcm.size,0);
    } finally {f.audio.interrupt();}
});

test('PCM source completion before end is not delivery completion; interruption suppresses all late acknowledgements',()=>{
    const f=pcmFixture();f.start();f.chunk();const late=f.sources[0].onended;
    f.finish(f.sources[0]);assert.deepEqual(f.events,['started']);
    f.audio.interrupt();late();assert.deepEqual(f.events,['started']);assert.equal(f.audio.pcm.size,0);
});

test('PCM rejects malformed metadata, reordered or mismatched chunks and incomplete end',()=>{
    for(const mutate of [f=>f.audio.pcmBegin({...f.begin,format:'wav'},()=>{}),
        f=>{f.start();f.chunk(1);}, f=>{f.start();f.audio.pcmChunk({...f.begin,turn_id:'wrong',sequence:0,audio:'AAA='});},
        f=>{f.start();f.chunk(0,Buffer.from([0]));},f=>{f.start();f.chunk();f.audio.pcmEnd({...f.begin,chunks:2});}]) {
        const f=pcmFixture();mutate(f);assert.equal(f.errors.length,1);assert.equal(f.audio.pcm.size,0);assert.ok(!f.events.includes('completed'));
        f.audio.interrupt();
    }
});

test('PCM bounds queued duration and stops scheduled sources on overflow',()=>{
    const f=pcmFixture();f.start();for(let n=0;n<30&&!f.errors.length;n++)f.chunk(n,Buffer.alloc(32768));
    assert.equal(f.errors.length,1);assert.ok(f.sources.length<30);assert.ok(f.sources.every(s=>s.stopped));
    assert.deepEqual(f.events,[]);assert.equal(f.audio.pcmSources.size,0);
});

test('PCM semantic hold uses the same output gain without completing a phrase',()=>{
    const f=pcmFixture();f.audio.outputGain={gain:{value:1}};f.start();f.chunk();
    f.audio.setPolicyHold(true);assert.equal(f.audio.outputGain.gain.value,.15);assert.equal(f.audio.pcmSources.size,1);
    assert.deepEqual(f.events,[]);f.audio.interrupt();
});

test('PCM abort discards scheduled output and cannot report completion',()=>{
    const f=pcmFixture();f.start();f.chunk();const late=f.sources[0].onended;
    f.audio.pcmAbort({...f.begin,epoch:1});assert.equal(f.audio.pcm.size,1);
    f.audio.pcmAbort(f.begin);late();assert.equal(f.audio.pcm.size,0);assert.deepEqual(f.events,[]);
    assert.equal(f.sources[0].stopped,true);
});
