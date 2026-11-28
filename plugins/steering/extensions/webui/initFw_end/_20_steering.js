import { store } from "/plugins/steering/webui/steering-store.js";
import { store as chatInputStore } from "/components/chat/input/input-store.js";

// Agent Zero's page turns `title` into its own tooltip attribute and takes it away, so a title is only set where it does not do that.
function name(button, text) {
  if (button.getAttribute("aria-label") !== text) button.setAttribute("aria-label", text);
  const attribute = globalThis.bootstrap?.Tooltip ? "data-bs-original-title" : "title";
  if (button.getAttribute(attribute) !== text) button.setAttribute(attribute, text);
}

// The keys (before the composer sees them), the send button's words, and one read per selected chat.
export default function steeringInit() {
  document.addEventListener("keydown", (event) => store.onKey(event), true);

  globalThis.Alpine?.effect(() => {
    const own = chatInputStore.sendButtonTitle;
    const text = store.buttonTitle(own);
    if (text === own) return;                       // Agent Zero's own binding says it
    setTimeout(() => {                              // after that binding and the page's tooltip observer
      const button = document.getElementById("send-button");
      if (button && store.buttonTitle(chatInputStore.sendButtonTitle) === text) name(button, text);
    }, 0);
  });

  let last = "";
  globalThis.Alpine?.effect(() => {
    const chat = store.chatId;
    if (!chat || chat === last) return;
    last = chat;
    void store.refresh(chat);
  });
}
