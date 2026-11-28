import { store } from "/plugins/steering/webui/steering-store.js";

export default async function steerBeforeSend(sendCtx) {
  await store.beforeSend(sendCtx);
}
