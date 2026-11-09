import { handleWebhook, json } from "../_lib/core.js";

export const onRequestPost = (context) => handleWebhook(context);

export const onRequestGet = () => json({ erro: "use POST" }, 405, { Allow: "POST" });
