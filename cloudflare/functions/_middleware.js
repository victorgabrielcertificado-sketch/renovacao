// Protege o painel estático (o Python autenticava o "/" no próprio servidor).
// Rotas /api/* e /webhook/* fazem a própria autenticação.
import { authenticate, json } from "./_lib/core.js";

export async function onRequest(context) {
  const { request, env, next } = context;
  const url = new URL(request.url);

  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/webhook/")) return next();
  if (!env.DB) return json({ erro: "binding D1 (DB) não configurado" }, 500);

  const auth = await authenticate(env, request, url);
  if (!auth.ok) return json(auth.body, auth.status, auth.extra);
  return next();
}
