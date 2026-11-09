import { handleWebhook } from "../_lib/core.js";

export const onRequestPost = (context) => handleWebhook(context);

export const onRequestGet = () =>
  new Response(JSON.stringify({ erro: "use POST" }), {
    status: 405,
    headers: { "Content-Type": "application/json; charset=utf-8", Allow: "POST" },
  });
