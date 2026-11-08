// These module locations changed between A0 1.2 and 2.7.  Do not use static
// imports of host modules: a missing optional recorder store must leave the
// host recorder intact instead of failing the complete extension bundle. The
// one static import is Convo's own pure dictation module.
import { continuousDraft } from "../../../webui/dictation.js";

let callJsonApi = null;
// The host's composer append (index.js `updateChatInput`: adds the text with a separating space). Continuous drafting needs it;
// without it (older hosts) every transcript goes to the host as before.
let updateChatInput = null;
// Convo's drafting flag (`voice_message_draft`, refreshed by Convo's status poll and by its settings card). Read per phrase.
let convoDraftFlag = () => globalThis.Alpine?.store?.("convo")?.settings?.voice_message_draft;
let speechStore = null;
let microphoneSettingStore = null;
let microphonePatchTarget = null;
let microphoneStatusChanged = null;
let recorderBindingRetryScheduled = false;
let recorderBindingRetryAttempts = 0;
// A0 1.x supplied the legacy transcription route, while current A0/Spine
// exposes it through the built-in Whisper plugin.  Keep the endpoint tied to
// the public module surface selected below instead of inferring it from DOM.
let transcriptionEndpoint = "/transcribe";

function scheduleRecorderBindingRetry() {
  if (recorderBindingRetryScheduled || recorderBindingRetryAttempts >= 30) return;
  recorderBindingRetryScheduled = true;
  recorderBindingRetryAttempts += 1;
  setTimeout(() => {
    recorderBindingRetryScheduled = false;
    void patchEnhancedSttRecorder();
  }, 500);
}

async function patchKokoroSettingsBridge(attempt = 0) {
  // Current A0/Spine exposes this provider module as its public store surface.
  // Importing the proxy works both before and after Alpine attaches its
  // reactive backing store, unlike probing an undocumented page object.
  let kokoroStore = null;
  try {
    ({ store: kokoroStore } = await import("/plugins/_kokoro_tts/webui/kokoro-tts-store.js"));
  } catch {
    kokoroStore = globalThis.Alpine?.store?.("kokoroTts") || null;
  }
  if (!kokoroStore) {
    if (attempt < 30) setTimeout(() => void patchKokoroSettingsBridge(attempt + 1), 100);
    return false;
  }
  if (kokoroStore.__agentspineKokoroSettingsBridgePatched) return true;

  let jsonApi = callJsonApi;
  if (!jsonApi) {
    try {
      ({ callJsonApi: jsonApi } = await import("/js/api.js"));
    } catch {
      return false;
    }
  }
  if (typeof jsonApi !== "function") return false;

  const hydrate = async (store) => {
    try {
      const result = await jsonApi("/plugins/_convo/speech_config", {});
      if (!result?.config || typeof result.enabled !== "boolean") return false;
      // This is the real provider toggle, not a UI inference. An intentional
      // disable remains disabled, while a stale host status result cannot hide
      // the Kokoro Voice Settings card.
      store.enabled = result.enabled;
      store.statusLoaded = true;
      store.config = { ...(store.config || {}), ...result.config };
      if (store.enabled) store.registerProvider?.();
      else store.unregisterProvider?.();
      return true;
    } catch {
      return false;
    }
  };

  kokoroStore.__agentspineKokoroSettingsBridgePatched = true;
  const originalRefreshStatus = kokoroStore.refreshStatus?.bind(kokoroStore);
  if (originalRefreshStatus) {
    kokoroStore.refreshStatus = async function enhancedRefreshStatus(...args) {
      // Do not allow an unavailable host status endpoint to undo a known-good
      // plugin-owned provider registration.  If the compatibility bridge can
      // hydrate the public store, it is the authoritative result.  Native
      // status stays available only as the fallback for hosts where this
      // plugin endpoint itself is unavailable.
      if (await hydrate(this)) return;
      return await originalRefreshStatus(...args);
    };
  }
  const hydrated = await hydrate(kokoroStore);
  if (hydrated) console.info("[Enhanced Speech] Kokoro settings bridge active");
  return hydrated;
}

async function hostModuleAvailable(path) {
  try {
    // Fetching a missing compatibility asset is quiet in the browser, unlike a
    // failed dynamic import which surfaces a console/network error. Probe
    // before importing either version-specific recorder surface.
    const response = await fetch(path, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

async function loadHostBindings() {
  let api;
  try {
    api = await import("/js/api.js");
  } catch {
    console.info("[Enhanced Speech] Recorder API unavailable on this host; native recorder retained.");
    return false;
  }

  // The current release targets expose this public v2.7 surface. Prefer it so
  // they never even probe the removed legacy module paths.
  if (
    (await hostModuleAvailable("/plugins/_whisper_stt/webui/whisper-stt-store.js")) &&
    (await hostModuleAvailable("/js/tts-service.js"))
  ) {
    try {
      const whisper = await import("/plugins/_whisper_stt/webui/whisper-stt-store.js");
      const tts = await import("/js/tts-service.js");
      const store = whisper.store;
      if (!api.callJsonApi || !store?.getSelectedDevice || !store?.initMicrophone) {
        throw new Error("Whisper STT recorder surface is incomplete");
      }

      // The native Whisper dashboard normally owns this initialization through
      // its page-level x-init.  The chat toolbar is available before that
      // dashboard is ever opened, leaving its provider unregistered and the
      // microphone button misleadingly stuck at "disabled".  Initialize the
      // documented public store once here; it only enumerates devices and
      // reads status, and never requests microphone permission.
      await store.initRuntime?.();

      callJsonApi = api.callJsonApi;
      transcriptionEndpoint = "/plugins/_whisper_stt/transcribe";
      // The same module the host's Whisper store imports its composer helpers from (never a second copy of it).
      const composer = await import("/index.js").catch(() => null);
      updateChatInput = typeof composer?.updateChatInput === "function" ? composer.updateChatInput : null;
      microphonePatchTarget = store;
      microphoneSettingStore = {
        getSelectedDevice: () => store.getSelectedDevice(),
      };
      // The recorder implementation below stays host-neutral through this
      // minimal settings/send adapter.
      speechStore = {
        get stt_waiting_timeout() { return Number(store.config?.waiting_timeout ?? 2000); },
        get stt_silence_threshold() { return Number(store.config?.silence_threshold ?? 0.3); },
        get stt_silence_duration() { return Number(store.config?.silence_duration ?? 1000); },
        get isSpeaking() { return Boolean(tts.ttsService?.isSpeaking?.()); },
        async sendMessage(text) { await store.sendVoiceMessage(text); },
      };
      microphoneStatusChanged = () => store.notifyStatusChange?.();
      return true;
    } catch {}
  }

  // Older compatible hosts do not serve the v2.7 Whisper module, so use their
  // public legacy stores only after that quiet availability probe.
  if (
    (await hostModuleAvailable("/components/chat/speech/speech-store.js")) &&
    (await hostModuleAvailable("/components/settings/speech/microphone-setting-store.js"))
  ) {
    try {
      const speech = await import("/components/chat/speech/speech-store.js");
      const microphone = await import("/components/settings/speech/microphone-setting-store.js");
      callJsonApi = api.callJsonApi;
      transcriptionEndpoint = "/transcribe";
      speechStore = speech.store;
      microphoneSettingStore = microphone.store;
      microphonePatchTarget = speech.store;
      microphoneStatusChanged = null;
      return Boolean(callJsonApi && microphonePatchTarget && microphoneSettingStore);
    } catch {}
  }

  console.info("[Enhanced Speech] Recorder adapter unavailable on this host; native recorder retained.");
  return false;
}

const Status = {
  INACTIVE: "inactive",
  ACTIVATING: "activating",
  LISTENING: "listening",
  RECORDING: "recording",
  WAITING: "waiting",
  PROCESSING: "processing",
};

const MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
  "audio/mp4",
];

const MIN_AUDIO_BYTES = 256;
const RECORDER_TIMESLICE_MS = 250;

function pickMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  for (const type of MIME_TYPES) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function notifyTranscriptionError(error) {
  if (typeof window.toastFetchError === "function") {
    window.toastFetchError("Transcription error", error);
  } else {
    console.error("Transcription error", error);
  }
}

function notifyMicrophoneError(message, error) {
  if (typeof window.toastFrontendError === "function") {
    window.toastFrontendError(message, error);
  } else if (typeof window.toast === "function") {
    window.toast(message, "error");
  } else {
    console.error("[Enhanced Speech]", message, error);
  }
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = String(reader.result || "");
      resolve(result.includes(",") ? result.split(",", 2)[1] : result);
    };
    reader.onerror = (error) => reject(error);
    reader.readAsDataURL(blob);
  });
}

class EnhancedMicrophoneInput {
  constructor(updateCallback) {
    this.mediaRecorder = null;
    this.stream = null;
    this.mediaStream = null;
    this.audioChunks = [];
    this.lastChunk = null;
    this.updateCallback = updateCallback;
    this.messageSent = false;
    this.audioContext = null;
    this.mediaStreamSource = null;
    this.analyserNode = null;
    this._status = Status.INACTIVE;
    this.lastAudioTime = null;
    this.waitingTimer = null;
    this.silenceStartTime = null;
    this.hasStartedRecording = false;
    this.analysisFrame = null;
    this.stopPromise = null;
    this.mimeType = "";
    // Convo's hold-to-dictate (orb gesture). `manual`: the recorder records unconditionally while a press is held instead of
    // waiting for voice and silence. `cancelled`: nothing already recorded may reach the host. `lastOutcome`: what the last
    // transcription did ("delivered" | "empty" | "silent" | "cancelled" | "error"), so Convo can say so when nothing arrives.
    this.manual = false;
    this.cancelled = false;
    this.lastOutcome = null;
    this.heardSpeech = false;
    this.analysisFrames = 0;
    // Continuous drafting. `owner`: the host speech store this recorder serves (its message_mode and finalTranscriptHandler).
    // `phrasesAdded`: phrases this dictation added to the composer. `appendGate`: after a manual send, a phrase that finishes
    // transcribing waits on it, so it lands in the composer the send has emptied instead of being wiped with the sent text.
    this.owner = null;
    this.phrasesAdded = 0;
    this.appendGate = null;
  }

  // Whether the next final transcript is added to the composer while listening goes on (see continuousDraft in dictation.js).
  continuousDraft() {
    if (typeof updateChatInput !== "function") return false;
    let flag = "";
    try { flag = convoDraftFlag(); } catch {}
    return continuousDraft({ flag, messageMode: this.owner?.config?.message_mode, handler: this.owner?.finalTranscriptHandler });
  }

  get status() {
    return this._status;
  }

  set status(newStatus) {
    if (this._status === newStatus) return;
    const oldStatus = this._status;
    this._status = newStatus;
    this.handleStatusChange(oldStatus, newStatus);
    microphoneStatusChanged?.();
    // Convo follows the recorder: when the host itself ends dictation (its draft voice-message mode stops after one utterance),
    // the orb must say so. Observation only; a failing observer never affects the recorder.
    try { this.onConvoStatus?.(newStatus); } catch {}
  }

  async initialize() {
    this.status = Status.ACTIVATING;
    try {
      const selectedDevice = microphoneSettingStore.getSelectedDevice();
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId:
            selectedDevice && selectedDevice.deviceId
              ? { exact: selectedDevice.deviceId }
              : undefined,
          echoCancellation: true,
          noiseSuppression: true,
          channelCount: 1,
        },
      });
      this.mediaStream = this.stream;
      this.selectedDeviceId = selectedDevice?.deviceId || "";

      this.mimeType = pickMimeType();
      const options = this.mimeType ? { mimeType: this.mimeType } : undefined;
      this.mediaRecorder = new MediaRecorder(this.stream, options);
      this.mimeType = this.mediaRecorder.mimeType || this.mimeType || "audio/webm";

      this.mediaRecorder.ondataavailable = (event) => {
        if (!event.data || event.data.size <= 0) return;
        if (this.status === Status.LISTENING) {
          this.lastChunk = event.data;
          return;
        }
        this.audioChunks.push(event.data);
      };

      this.setupAudioAnalysis(this.stream);
      return true;
    } catch (error) {
      console.error("[Enhanced Speech] Microphone initialization error:", error);
      notifyMicrophoneError("Failed to access microphone. Please check permissions.", error);
      this.status = Status.INACTIVE;
      return false;
    }
  }

  handleStatusChange(_oldStatus, newStatus) {
    if (newStatus !== Status.RECORDING) this.lastChunk = null;

    switch (newStatus) {
      case Status.INACTIVE:
        this.handleInactiveState();
        break;
      case Status.LISTENING:
        this.handleListeningState();
        break;
      case Status.RECORDING:
        this.handleRecordingState();
        break;
      case Status.WAITING:
        this.handleWaitingState();
        break;
      case Status.PROCESSING:
        this.handleProcessingState();
        break;
    }
  }

  handleInactiveState() {
    this.stopAudioAnalysis();
    if (this.waitingTimer) {
      clearTimeout(this.waitingTimer);
      this.waitingTimer = null;
    }
    this.finalizeRecording();
  }

  handleListeningState() {
    this.finalizeRecording();
    this.audioChunks = [];
    this.hasStartedRecording = false;
    this.silenceStartTime = null;
    this.lastAudioTime = null;
    this.messageSent = false;
    this.startAudioAnalysis();
  }

  handleRecordingState() {
    if (!this.hasStartedRecording && this.mediaRecorder?.state !== "recording") {
      this.hasStartedRecording = true;
      this.audioChunks = [];
      if (this.lastChunk) {
        this.audioChunks.push(this.lastChunk);
        this.lastChunk = null;
      }
      this.mediaRecorder.start(RECORDER_TIMESLICE_MS);
      console.log("[Enhanced Speech] Speech recording started", this.mimeType);
    }
    if (this.waitingTimer) {
      clearTimeout(this.waitingTimer);
      this.waitingTimer = null;
    }
  }

  handleWaitingState() {
    this.waitingTimer = setTimeout(() => {
      if (this.status === Status.WAITING) this.status = Status.PROCESSING;
    }, speechStore.stt_waiting_timeout);
  }

  handleProcessingState() {
    if (!this.processingPromise) this.processingPromise = this.process().finally(() => { this.processingPromise = null; });
  }

  setupAudioAnalysis(stream) {
    this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
    this.mediaStreamSource = this.audioContext.createMediaStreamSource(stream);
    this.analyserNode = this.audioContext.createAnalyser();
    this.analyserNode.fftSize = 2048;
    this.analyserNode.minDecibels = -90;
    this.analyserNode.maxDecibels = -10;
    this.analyserNode.smoothingTimeConstant = 0.85;
    this.mediaStreamSource.connect(this.analyserNode);
  }

  startAudioAnalysis() {
    const analyzeFrame = () => {
      if (this.status === Status.INACTIVE) return;

      const dataArray = new Uint8Array(this.analyserNode.fftSize);
      this.analyserNode.getByteTimeDomainData(dataArray);

      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const amplitude = (dataArray[i] - 128) / 128;
        sum += amplitude * amplitude;
      }
      const rms = Math.sqrt(sum / dataArray.length);
      const now = Date.now();

      this.analysisFrames += 1;
      if (rms > this.densify(speechStore.stt_silence_threshold)) {
        this.lastAudioTime = now;
        this.heardSpeech = true;
        this.silenceStartTime = null;

        if (
          (this.status === Status.LISTENING || this.status === Status.WAITING) &&
          !speechStore.isSpeaking
        ) {
          this.status = Status.RECORDING;
        }
      } else if (this.status === Status.RECORDING && !this.manual) {
        if (!this.silenceStartTime) this.silenceStartTime = now;

        const silenceDuration = now - this.silenceStartTime;
        if (silenceDuration >= speechStore.stt_silence_duration) {
          this.status = Status.WAITING;
        }
      }

      this.analysisFrame = requestAnimationFrame(analyzeFrame);
    };

    this.analysisFrame = requestAnimationFrame(analyzeFrame);
  }

  stopAudioAnalysis() {
    if (this.analysisFrame) {
      cancelAnimationFrame(this.analysisFrame);
      this.analysisFrame = null;
    }
  }

  finalizeRecording() {
    if (!this.mediaRecorder || this.mediaRecorder.state !== "recording") {
      this.hasStartedRecording = false;
      return Promise.resolve();
    }
    if (this.stopPromise) return this.stopPromise;

    this.stopPromise = new Promise((resolve) => {
      const recorder = this.mediaRecorder;
      const done = () => {
        recorder.removeEventListener("stop", done);
        this.hasStartedRecording = false;
        this.stopPromise = null;
        resolve();
      };
      recorder.addEventListener("stop", done, { once: true });
      try {
        if (recorder.state === "recording" && typeof recorder.requestData === "function") {
          recorder.requestData();
        }
      } catch (error) {
        console.debug("[Enhanced Speech] requestData failed", error);
      }
      recorder.stop();
    });

    return this.stopPromise;
  }

  densify(x) {
    return Math.exp(-5 * (1 - x));
  }

  async process() {
    await this.finalizeRecording();

    const size = this.audioChunks.reduce((total, chunk) => total + chunk.size, 0);
    if (size < MIN_AUDIO_BYTES) {
      console.debug("[Enhanced Speech] Ignoring empty or tiny recording", size);
      this.lastOutcome = "empty";
      this.audioChunks = [];
      this.status = Status.LISTENING;
      return;
    }

    const audioBlob = new Blob(this.audioChunks, { type: this.mimeType });
    const base64 = await blobToBase64(audioBlob);

    try {
      const result = await callJsonApi(transcriptionEndpoint, {
        audio: base64,
        mime_type: audioBlob.type || this.mimeType,
      });
      const text = this.filterResult(result.text || "");

      if (this.cancelled) {
        // Dictation was cancelled while this was being transcribed: the host never sees the text.
        this.lastOutcome = "cancelled";
      } else if (text) {
        console.log("[Enhanced Speech] Transcription:", result.text);
        const handled = await this.updateCallback(result.text, true);
        this.lastOutcome = handled === "cancelled" ? "cancelled" : "delivered";
      } else {
        this.lastOutcome = "empty";
      }
    } catch (error) {
      this.lastOutcome = "error";
      notifyTranscriptionError(error);
    } finally {
      this.audioChunks = [];
      // Back to listening for the next utterance (continuous drafting always comes back here), unless the host stopped dictation
      // while handling this one (draft mode calls its own stop() from sendVoiceMessage): a stopped recorder must not start
      // listening again by itself.
      if (this.status !== Status.INACTIVE) this.status = Status.LISTENING;
    }
  }

  filterResult(text) {
    text = text.trim();
    let ok = false;
    while (!ok) {
      if (!text) break;
      if (text[0] === "{" && text[text.length - 1] === "}") break;
      if (text[0] === "(" && text[text.length - 1] === ")") break;
      if (text[0] === "[" && text[text.length - 1] === "]") break;
      ok = true;
    }
    if (ok) return text;
    console.log("[Enhanced Speech] Discarding transcription:", text);
    return "";
  }

  async toggle() {
    const hasPermission = await this.requestPermission();
    if (!hasPermission) return;

    if (this.status === Status.INACTIVE || this.status === Status.ACTIVATING) {
      this.phrasesAdded = 0;
      this.appendGate = null;
      this.status = Status.LISTENING;
    } else if (this.owner?.microphoneInput === this && typeof this.owner.stop === "function") {
      // The host's own contract for a second microphone click: its stop() releases the stream and clears its handler. A phrase
      // already being transcribed still arrives; the one being recorded is dropped, as with the host's own recorder.
      this.owner.stop();
    } else {
      this.status = Status.INACTIVE;
    }
  }

  // Convo hold-to-dictate. Starts recording at once and keeps recording until finish/discard, whatever the silence (push-to-talk).
  // The capture is the one this recorder already owns: no second stream is opened, and the host's permission probe is skipped
  // because initialize() has already been granted the device.
  async startManual() {
    this.cancelled = false;
    this.heardSpeech = false;
    this.analysisFrames = 0;
    this.phrasesAdded = 0;
    this.appendGate = null;
    this.manual = true;
    if (this.status === Status.INACTIVE || this.status === Status.ACTIVATING) this.status = Status.LISTENING;
    if (this.status !== Status.RECORDING) this.status = Status.RECORDING;
  }

  // Held -> released: the same recording carries on under the host's own voice and silence rules (latched dictation). If nothing
  // has been said yet, discard the silence recorded so far rather than send it to speech-to-speech.
  setManual(flag) {
    flag = Boolean(flag);
    if (this.manual === flag) return;
    this.manual = flag;
    if (!flag && this.status === Status.RECORDING && !this.heardSpeech) {
      this.silenceStartTime = null;
      this.status = Status.LISTENING;
    }
  }

  // Stop dictating and hand over what was said, exactly like the host's voice button would. Resolves to what happened, so the
  // caller can say so when nothing arrived.
  async finishConvoDictation() {
    this.stopAudioAnalysis();
    if (this.waitingTimer) { clearTimeout(this.waitingTimer); this.waitingTimer = null; }
    this.lastOutcome = null;
    try {
      if (this.processingPromise) await this.processingPromise;
      else if (this.hasStartedRecording || this.audioChunks.length) {
        // A held recording of pure silence is not sent: speech-to-speech models invent words for silence. Only when the
        // meter actually ran (a hidden page pauses it) and heard nothing.
        if (this.manual && this.analysisFrames > 0 && !this.heardSpeech) this.lastOutcome = "silent";
        else {
          this._status = Status.PROCESSING;
          await this.process();
        }
      }
    } finally { this.dispose(); }
    return this.lastOutcome;
  }

  // Cancel: whatever is recorded or being transcribed is dropped and never reaches the host.
  discardConvoDictation() {
    this.cancelled = true;
    this.lastOutcome = "cancelled";
    this.audioChunks = [];
    this.dispose();
    return this.lastOutcome;
  }

  async requestPermission() {
    try {
      // Match the current v2.7 host contract: this is a permission probe, not
      // the stream consumed by the recorder. Leaving it open can make a second
      // device request fail on browsers that allow only one active capture.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      return true;
    } catch (error) {
      console.error("[Enhanced Speech] Error accessing microphone:", error);
      notifyMicrophoneError(
        "Microphone access denied. Please enable microphone access in your browser settings.",
        error,
      );
      return false;
    }
  }

  dispose() {
    this.status = Status.INACTIVE;
    this.stopAudioAnalysis();
    if (this.waitingTimer) {
      clearTimeout(this.waitingTimer);
      this.waitingTimer = null;
    }
    this.stream?.getTracks?.().forEach((track) => track.stop());
    this.mediaStream = null;
    this.stream = null;
    this.mediaStreamSource?.disconnect?.();
    this.mediaStreamSource = null;
    this.analyserNode?.disconnect?.();
    this.analyserNode = null;
    void this.audioContext?.close?.();
    this.audioContext = null;
  }
}

// Where a final transcript goes. Continuous drafting adds it to the composer and keeps listening; nothing is sent. Otherwise it
// goes to the host exactly as the host's own recorder hands it over: deliverVoiceMessage (which honours a host finalTranscriptHandler,
// then leaves the text in the composer or sends it, as its message_mode says), or the older hosts' send path.
async function deliverTranscript(owner, input, text) {
  if (input.continuousDraft()) {
    if (input.appendGate) await input.appendGate;
    if (input.cancelled) return "cancelled";                 // Escape while it waited: the phrase in progress is dropped
    const phrase = String(text || "").trim();
    if (!phrase) return "empty";
    updateChatInput(phrase);
    input.phrasesAdded += 1;
    return "appended";
  }
  if (typeof owner?.deliverVoiceMessage === "function") await owner.deliverVoiceMessage(text);
  else if (typeof owner?.sendMessage === "function") await owner.sendMessage(text);
  else await speechStore.sendMessage(text);
  return "delivered";
}

// The recorder Convo puts in the host's place, serving `owner` (the host speech store).
function createConvoRecorder(owner) {
  const input = new EnhancedMicrophoneInput(async (text, isFinal) => {
    if (!isFinal) return null;
    return deliverTranscript(owner, input, text);
  });
  input.owner = owner;
  return input;
}

export default async function patchEnhancedSttRecorder() {
  const hostBindingsReady = await loadHostBindings();
  // Run this from the known-loaded `initFw_end` module. The bridge remains
  // useful even when a legacy host has no recorder surface, so do it first.
  const kokoroBridgeReady = await patchKokoroSettingsBridge();
  if (!hostBindingsReady) {
    // On a fresh CUDA start, initFw can run while the plugin asset registry is
    // still becoming reachable. Retry the documented public binding briefly
    // instead of leaving the chat toolbar disabled for the whole session.
    scheduleRecorderBindingRetry();
    return kokoroBridgeReady;
  }
  if (microphonePatchTarget._enhancedSpeechRecorderPatched) return;

  const originalInitMicrophone = microphonePatchTarget.initMicrophone?.bind(microphonePatchTarget);
  microphonePatchTarget._enhancedSpeechOriginalInitMicrophone = originalInitMicrophone;
  microphonePatchTarget._enhancedSpeechRecorderPatched = true;
  microphonePatchTarget.initMicrophone = async function initEnhancedMicrophone() {
    if (this.microphoneInput) return this.microphoneInput;

    this.microphoneInput = createConvoRecorder(this);

    const initialized = await this.microphoneInput.initialize();
    if (!initialized) {
      this.microphoneInput = null;
      return null;
    }
    return this.microphoneInput;
  };

  console.log("[Enhanced Speech] STT recorder patch active");
}

// Test hook only: lets the node fixtures drive the recorder without the host modules.
export const __test = {
  EnhancedMicrophoneInput,
  Status,
  createConvoRecorder,
  deliverTranscript,
  configure(values) {
    if ("speechStore" in values) speechStore = values.speechStore;
    if ("microphoneSettingStore" in values) microphoneSettingStore = values.microphoneSettingStore;
    if ("callJsonApi" in values) callJsonApi = values.callJsonApi;
    if ("transcriptionEndpoint" in values) transcriptionEndpoint = values.transcriptionEndpoint;
    if ("updateChatInput" in values) updateChatInput = values.updateChatInput;
    if ("convoDraftFlag" in values) convoDraftFlag = values.convoDraftFlag;
  },
};
