// Steering on the page: what Enter does while the agent works, the strip of waiting messages and the mode chip.
// The server decides everything (helpers/steering.py); this only sends, shows and asks.
import { createStore } from "/js/AlpineStore.js";
import { callJsonApi, fetchApi } from "/js/api.js";
import { store as chatsStore } from "/components/sidebar/chats/chats-store.js";
import { store as chatInputStore } from "/components/chat/input/input-store.js";
import { store as attachmentsStore } from "/components/chat/attachments/attachmentsStore.js";
import { toastFrontendError, toastFrontendInfo } from "/components/notifications/notification-store.js";

export const API = "/plugins/steering/steering";
export const MODES = [
  { id: "automatic", label: "Automatic", hint: "The agent takes your message in at its next step and decides by what it says: now, afterwards, or stop." },
  { id: "steer", label: "Steer", hint: "Your message goes in at the next step and is worked in right away." },
  { id: "queue", label: "Queue", hint: "Your message waits until the run is done." },
];
const KNOWN = new Set(MODES.map((mode) => mode.id));
const EMPTY = Object.freeze({ mode: "automatic", own_mode: "", revision: -1, items: Object.freeze([]) });

const newId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;

const model = {
  fetched: {}, // the last view read for a chat, until its snapshot brings a newer one
  sending: [], // messages on their way to the server: {id, context, text}
  nextHow: "", // one message's own way (a key), used once

  get chatId() { return String(chatsStore.selected || ""); },
  get chat() { return chatsStore.selectedContext || null; },

  // A sub-agent's own chat does not count as running while its superior waits for it.
  atWork(chat = this.chat) {
    if (!chat) return false;
    if (chat.running) return true;
    const parent = chat.parent_context_id && (chatsStore.contexts || []).find((item) => item?.id === chat.parent_context_id);
    return parent ? this.atWork(parent) : false;
  },

  viewOf(chat = this.chat) {
    if (!chat?.id) return EMPTY;
    const snapshot = chat._steering, read = this.fetched[chat.id];
    if (snapshot && (!read || (snapshot.revision ?? 0) >= (read.revision ?? 0))) return snapshot;
    return read || EMPTY;
  },

  get items() {
    const items = this.viewOf().items || [];
    const known = new Set(items.map((item) => item.id));
    const onTheWay = this.sending.filter((item) => item.context === this.chatId && !known.has(item.id)).map((item) => ({ ...item, status: "sending", attachments: [] }));
    return [...items, ...onTheWay];
  },
  get mode() { const mode = this.viewOf().mode; return KNOWN.has(mode) ? mode : "automatic"; },
  get modeHint() { return MODES.find((mode) => mode.id === this.mode)?.hint || ""; },
  get showChip() { return this.atWork() || this.items.length > 0; },
  get heldCount() { return this.items.filter((item) => item.status === "held").length; },
  get title() {
    const waiting = this.items.length - this.heldCount;
    if (!waiting) return this.heldCount === 1 ? "Not sent: the run ended first" : `Not sent: the run ended first (${this.heldCount})`;
    return waiting === 1 ? "Goes in at the next step" : `Go in at the next step (${waiting})`;
  },
  itemText(item) { return item.text || (item.attachments?.length ? item.attachments.join(", ") : "(attachment only)"); },
  itemState(item) { return item.status === "held" ? "not sent" : item.status === "sending" ? "sending…" : item.mode === "steer" ? "steers" : "waiting"; },

  keep(view) { if (view && this.chatId) this.fetched = { ...this.fetched, [this.chatId]: view }; },
  keepFor(context, view) { if (view && context) this.fetched = { ...this.fetched, [context]: view }; },

  // Read once per chat: after a restart the waiting messages come back as held, and nothing else would show them.
  async refresh(context = this.chatId) {
    if (!context) return;
    try {
      const answer = await callJsonApi(API, { action: "view", context });
      this.keepFor(context, answer?.steering);
    } catch { /* the chat is new or gone: nothing waits in it */ }
  },

  // send_message_before: while the agent works, a message goes to the server instead of Agent Zero's queue.
  async beforeSend(sendCtx) {
    const how = this.nextHow;
    this.nextHow = "";
    if (!sendCtx || sendCtx.cancel) return;
    const context = String(sendCtx.context || "");
    const message = String(sendCtx.message || "").trim();
    const attachments = Array.isArray(sendCtx.attachments) ? sendCtx.attachments : [];
    if (!context || (!message && !attachments.length)) return;
    const chat = (chatsStore.contexts || []).find((item) => item?.id === context) || (context === this.chatId ? this.chat : null);
    if (!this.atWork(chat)) return;
    const wanted = how || this.viewOf(chat).mode || "automatic";
    if (wanted === "queue" && chat?.running) return; // Agent Zero's own queue
    sendCtx.cancel = true;
    await this.deliver({ context, message, attachments, how: how === "queue" ? "" : how });
  },

  async deliver({ context, message, attachments = [], how = "" }) {
    const id = newId();
    const fromComposer = context === this.chatId && String(chatInputStore.message || "").trim() === message;
    const kept = fromComposer ? { text: chatInputStore.message, files: [...(attachmentsStore.attachments || [])] } : null;
    this.sending = [...this.sending, { id, context, text: message.slice(0, 200) }];
    if (fromComposer) {
      chatInputStore.reset();
      chatInputStore.adjustTextareaHeight?.();
    }
    try {
      const filenames = await this.upload(attachments);
      const answer = await callJsonApi(API, { action: "send", context, text: message, attachments: filenames, how, id });
      this.keepFor(context, answer?.steering);
    } catch (error) {
      console.error("Steering could not send the message:", error);
      if (kept && context === this.chatId && !String(chatInputStore.message || "").trim()) {
        chatInputStore.message = kept.text;
        for (const file of kept.files) attachmentsStore.addAttachment?.(file);
        chatInputStore.adjustTextareaHeight?.();
      }
      toastFrontendError(kept ? "The message was not sent. It is back in the composer." : "The message was not sent.", "Steering");
    } finally {
      this.sending = this.sending.filter((item) => item.id !== id);
    }
  },

  async upload(attachments) {
    if (!attachments.length) return [];
    const form = new FormData();
    for (const attachment of attachments) form.append("file", attachment.file || attachment);
    const response = await fetchApi("/upload", { method: "POST", body: form });
    if (!response?.ok) throw new Error("The attachments could not be uploaded");
    const names = (await response.json())?.filenames || [];
    if (names.length !== attachments.length) throw new Error("Not all attachments could be uploaded");
    return names;
  },

  async act(action, item) {
    const context = this.chatId;
    try {
      const answer = await callJsonApi(API, { action, context, id: item.id });
      this.keepFor(context, answer?.steering);
      if (!answer?.ok) toastFrontendInfo("That message is already in the run.", "Steering");
      return answer;
    } catch (error) {
      console.error("Steering:", error);
      toastFrontendError("That did not work. Please try again.", "Steering");
      return null;
    }
  },
  interruptNow(item) { return this.act("interrupt", item); },
  afterTheTask(item) { return this.act("queue", item); },
  sendNow(item) { return this.act("release", item); },
  remove(item) { return this.act("remove", item); },
  async backToComposer(item) {
    const taken = (await this.act("take", item))?.item;
    if (!taken) return;
    const now = String(chatInputStore.message || "");
    chatInputStore.message = now.trim() ? `${now.replace(/\s+$/, "")}\n${taken.text}` : taken.text;
    if (taken.attachments?.length) toastFrontendInfo("Please add the attachments again: " + taken.attachments.join(", "), "Steering");
    chatInputStore.adjustTextareaHeight?.();
    chatInputStore.focus?.();
  },

  async setMode(mode) {
    const context = this.chatId;
    if (!context || !KNOWN.has(mode)) return;
    const before = this.viewOf();
    this.keepFor(context, { ...before, mode, own_mode: mode, revision: (before.revision ?? 0) + 1 });
    try {
      const answer = await callJsonApi(API, { action: "mode", context, mode });
      this.keepFor(context, answer?.steering);
    } catch (error) {
      console.error("Steering:", error);
      this.keepFor(context, before);
      toastFrontendError("The choice was not saved.", "Steering");
    }
  },

  // Ctrl+Enter steers, Alt+Enter queues, whatever the chip says. Returns true when the key was used.
  onKey(event) {
    if (event.key !== "Enter" || event.isComposing || event.keyCode === 229 || event.shiftKey) return false;
    const steer = event.ctrlKey || event.metaKey, later = event.altKey;
    if (steer === later || !event.target?.closest?.("#chat-input")) return false;
    if (!this.atWork() || (!String(chatInputStore.message || "").trim() && !attachmentsStore.attachments?.length)) return false;
    event.preventDefault();
    event.stopImmediatePropagation();
    this.nextHow = steer ? "steer" : "queue";
    void chatInputStore.sendMessage();
    return true;
  },

  // Agent Zero's send button says "Add to queue" while the agent works; say what it does now.
  buttonTitle(title) {
    if (title !== "Add to queue" || this.mode === "queue" || !this.atWork()) return title;
    return this.mode === "steer" ? "Steer: goes in at the next step" : "Send: goes in at the next step";
  },
};

export const store = createStore("steering", model);
