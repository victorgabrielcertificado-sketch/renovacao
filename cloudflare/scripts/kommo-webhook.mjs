// Registra (ou atualiza) o webhook da Kommo para o painel.
//
// Uso no PowerShell:
//   $env:KOMMO_URL="https://suaconta.kommo.com"
//   $env:KOMMO_TOKEN="token-de-longa-duracao"
//   $env:WEBHOOK_TOKEN="token-do-webhook-do-painel"
//   node scripts/kommo-webhook.mjs
//
// Ou passe a URL completa:
//   $env:WEBHOOK_URL="https://renovacao-61n.pages.dev/webhook/kommo?t=..."
//
// O token da Kommo precisa ser de um usuario administrador.

const kommoUrl = (process.env.KOMMO_URL || "").trim().replace(/\/+$/, "");
const kommoToken = (process.env.KOMMO_TOKEN || "").trim();
const base = (process.env.WEBHOOK_BASE || "https://renovacao-61n.pages.dev").replace(/\/+$/, "");
const webhookToken = (process.env.WEBHOOK_TOKEN || "").trim();
const destination =
  (process.env.WEBHOOK_URL || "").trim() ||
  `${base}/webhook/kommo${webhookToken ? `?t=${encodeURIComponent(webhookToken)}` : ""}`;

if (!kommoUrl || !kommoToken) {
  console.error("Defina KOMMO_URL e KOMMO_TOKEN.");
  process.exit(1);
}
if (!process.env.WEBHOOK_URL && !webhookToken) {
  console.error("Defina WEBHOOK_TOKEN ou WEBHOOK_URL.");
  process.exit(1);
}

const masked = destination.replace(/([?&]t=)[^&]+/, "$1***");
console.log("Registrando webhook:", masked);

const resp = await fetch(`${kommoUrl}/api/v4/webhooks`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${kommoToken}`,
    "Content-Type": "application/json",
    Accept: "application/json",
  },
  body: JSON.stringify({
    destination,
    settings: ["status_lead"],
  }),
});

const texto = await resp.text();
if (!resp.ok) {
  console.error(`Kommo HTTP ${resp.status}: ${texto.slice(0, 500)}`);
  process.exit(1);
}

console.log("Webhook registrado/atualizado com o evento status_lead.");
