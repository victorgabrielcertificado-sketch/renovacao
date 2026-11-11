// Teste de integração da versão Cloudflare Pages.
// Requer o servidor local: npm run dev (porta 8788) e o banco semeado (npm run db:seed).
import "reflect-metadata";
import assert from "node:assert/strict";
import * as x509 from "@peculiar/x509";
import { certInfo } from "../functions/_lib/certs.js";

const BASE = process.env.BASE_URL || "http://127.0.0.1:8788";
const BASIC = "Basic " + Buffer.from("admin:teste").toString("base64");
const ACCESS_OP = { "Cf-Access-Authenticated-User-Email": "op@teste.com" };
const H = { "X-Requested-With": "fetch" };

async function req(path, { method = "GET", headers = {}, body } = {}) {
  const r = await fetch(BASE + path, { method, headers, body, redirect: "manual" });
  const text = await r.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* html */
  }
  return { status: r.status, text, json, headers: r.headers };
}

async function esperar() {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(BASE + "/", { headers: { Authorization: BASIC } });
      if (r.status) return;
    } catch {
      /* ainda subindo */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("servidor não respondeu em " + BASE);
}

const passos = [];
async function passo(nome, fn) {
  await fn();
  passos.push(nome);
  console.log("  ok -", nome);
}

async function gerarCert(cn) {
  const alg = { name: "ECDSA", namedCurve: "P-256", hash: "SHA-256" };
  const keys = await crypto.subtle.generateKey(alg, true, ["sign", "verify"]);
  const cert = await x509.X509CertificateGenerator.createSelfSigned({
    serialNumber: "01",
    name: `CN=${cn}`,
    notBefore: new Date(Date.now() - 86400000),
    notAfter: new Date(Date.now() + 86400000 * 30),
    signingAlgorithm: alg,
    keys,
  });
  const pem = cert.toString("pem");
  return { pem, info: await certInfo("x.pem", pem) };
}

async function main() {
  await esperar();
  console.log("Servidor:", BASE);

  await passo("painel exige autenticação", async () => {
    assert.equal((await req("/")).status, 401);
  });

  await passo("Basic Auth abre o painel", async () => {
    const r = await req("/", { headers: { Authorization: BASIC } });
    assert.equal(r.status, 200);
    assert.match(r.text, /Renovação de certificados/);
  });

  await passo("/api/eu com Basic Auth", async () => {
    const r = await req("/api/eu", { headers: { Authorization: BASIC } });
    assert.equal(r.status, 200);
    assert.equal(r.json.perfil, "admin");
  });

  await passo("navegação sem login redireciona para /login", async () => {
    const r = await req("/", { headers: { Accept: "text/html" } });
    assert.equal(r.status, 302);
    assert.match(r.headers.get("location") || "", /\/login\?next=%2F/);
    const login = await req("/login");
    assert.equal(login.status, 200);
    assert.match(login.text, /Entrar/);
  });

  await passo("login por formulário cria sessão", async () => {
    const errado = await req("/api/login", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ usuario: "admin", senha: "errada" }),
    });
    assert.equal(errado.status, 401);

    const ok = await req("/api/login", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ usuario: "admin", senha: "teste" }),
    });
    assert.equal(ok.status, 200);
    const setCookie = ok.headers.get("set-cookie") || "";
    const cookie = setCookie.split(";")[0];
    assert.match(cookie, /^renovacao_sessao=/);
    assert.match(setCookie, /HttpOnly/i);
    assert.match(setCookie, /SameSite=Lax/i);
    assert.match(setCookie, /Path=\//i);
    assert.equal(cookie.split("=")[1].split(".").length, 3); // JWT compacto

    const eu = await req("/api/eu", { headers: { Cookie: cookie } });
    assert.equal(eu.status, 200);
    assert.equal(eu.json.fonte, "sessao");
    assert.match(eu.json.csrf, /^[0-9a-f]{32}$/);

    const painel = await req("/", { headers: { Cookie: cookie } });
    assert.equal(painel.status, 200);

    const semCsrf = await req("/api/config", {
      method: "POST",
      headers: { ...H, Cookie: cookie, "Content-Type": "application/json" },
      body: "{}",
    });
    assert.equal(semCsrf.status, 403);

    const comCsrf = await req("/api/config", {
      method: "POST",
      headers: { ...H, Cookie: cookie, "Content-Type": "application/json", "X-CSRF-Token": eu.json.csrf },
      body: "{}",
    });
    assert.equal(comCsrf.status, 200);

    const outSem = await req("/api/logout", { method: "POST", headers: { ...H, Cookie: cookie } });
    assert.equal(outSem.status, 403);

    const out = await req("/api/logout", {
      method: "POST",
      headers: { ...H, Cookie: cookie, "X-CSRF-Token": eu.json.csrf },
    });
    assert.equal(out.status, 200);
    assert.match(out.headers.get("set-cookie") || "", /Max-Age=0/);
  });

  await passo("cabeçalhos de segurança no painel e na API", async () => {
    const login = await req("/login");
    assert.equal(login.status, 200);
    for (const h of [
      "strict-transport-security",
      "content-security-policy",
      "x-frame-options",
      "x-content-type-options",
      "referrer-policy",
      "permissions-policy",
      "cross-origin-opener-policy",
      "cross-origin-resource-policy",
    ]) {
      assert.ok(login.headers.get(h), `login sem ${h}`);
    }
    assert.match(login.headers.get("content-security-policy"), /frame-ancestors 'none'/);

    const api = await req("/api/eu", { headers: { Authorization: BASIC } });
    assert.equal(api.status, 200);
    assert.ok(api.headers.get("strict-transport-security"));
    assert.ok(api.headers.get("content-security-policy"));
    assert.ok(api.headers.get("x-frame-options"));
    assert.equal(api.headers.get("access-control-allow-origin"), null);
  });

  await passo("preflight CORS fechado", async () => {
    const r = await req("/api/eu", { method: "OPTIONS" });
    assert.equal(r.status, 204);
    assert.match(r.headers.get("allow") || "", /OPTIONS/);
    assert.equal(r.headers.get("access-control-allow-origin"), null);
  });

  await passo("fonte pública para a tela de login", async () => {
    const r = await req("/fonts.css");
    assert.equal(r.status, 200);
    assert.match(r.headers.get("content-type") || "", /text\/css/);
  });

  await passo("/api/eu com Cloudflare Access", async () => {
    const r = await req("/api/eu", { headers: ACCESS_OP });
    assert.equal(r.status, 200);
    assert.equal(r.json.nome, "Operador");
    assert.equal(r.json.perfil, "operador");
    assert.equal(r.json.fonte, "access");
  });

  await passo("operador não acessa administração", async () => {
    assert.equal((await req("/api/usuarios", { headers: ACCESS_OP })).status, 403);
    assert.equal((await req("/api/config", { headers: ACCESS_OP })).status, 403);
    assert.equal((await req("/api/renovacoes", { headers: ACCESS_OP })).status, 200);
  });

  await passo("métricas e lista do mês", async () => {
    const m = await req("/api/metricas?mes=2026-11", { headers: { Authorization: BASIC } });
    assert.equal(m.status, 200);
    assert.equal(m.json.total, 2); // Alfa e Beta vencem em novembro
    assert.equal(m.json.funil.pago, 1);
    const l = await req("/api/renovacoes?mes=2026-11", { headers: { Authorization: BASIC } });
    assert.equal(l.status, 200);
    assert.equal(l.json.length, 2);
  });

  await passo("mês inválido retorna 400", async () => {
    assert.equal((await req("/api/metricas?mes=2026-13", { headers: { Authorization: BASIC } })).status, 400);
  });

  await passo("avançar status exige CSRF e grava histórico", async () => {
    const lista = await req("/api/renovacoes?mes=2026-12", { headers: { Authorization: BASIC } });
    const gama = lista.json.find((x) => x.cliente === "Gama Ltda");
    assert.ok(gama, "Gama não encontrada");
    assert.equal(
      (await req("/api/avancar", { method: "POST", headers: { Authorization: BASIC, "Content-Type": "application/json" }, body: JSON.stringify({ id: gama.id, status: "agendado" }) })).status,
      403,
    );
    const ok = await req("/api/avancar", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ id: gama.id, status: "agendado" }),
    });
    assert.equal(ok.status, 200);
    const hist = await req(`/api/historico?id=${gama.id}`, { headers: { Authorization: BASIC } });
    assert.equal(hist.status, 200);
    assert.ok(hist.json.some((e) => e.tipo === "mudanca_etapa" && e.resultado === "agendado"));
  });

  await passo("evento manual é registrado", async () => {
    const lista = await req("/api/renovacoes?mes=2026-12", { headers: { Authorization: BASIC } });
    const id = lista.json[0].id;
    const r = await req("/api/evento", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ id, tipo: "ligacao", canal: "telefone", resultado: "atendeu", detalhe: "teste" }),
    });
    assert.equal(r.status, 200);
    const hist = await req(`/api/historico?id=${id}`, { headers: { Authorization: BASIC } });
    assert.ok(hist.json.some((e) => e.tipo === "ligacao" && e.detalhe === "teste"));
  });

  await passo("régua por cron token", async () => {
    const r = await req("/api/regua?t=cron", { method: "POST", headers: H, body: "" });
    assert.equal(r.status, 200);
    assert.equal(typeof r.json.notificadas, "number");
  });

  await passo("webhook da Kommo aplica etapa", async () => {
    assert.equal(
      (await req("/webhook/kommo", { method: "POST", body: "" })).status,
      403,
    );
    const body = "leads[status][0][id]=999&leads[status][0][status_id]=777";
    const r = await req("/webhook/kommo?t=segredo", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    assert.equal(r.status, 200);
    const lista = await req("/api/renovacoes?mes=2026-11", { headers: { Authorization: BASIC } });
    assert.equal(lista.json.find((x) => x.cliente === "Alfa Ltda").status, "agendado");
  });

  await passo("importação CSV (novas e atualizadas)", async () => {
    const csv = "Cliente;Vencimento;Valor\nImportada API;15/01/2027;R$ 99,90\n";
    const url = "/api/importar?nome=teste.csv";
    const a = await req(url, { method: "POST", headers: { Authorization: BASIC, ...H }, body: csv });
    assert.equal(a.status, 200);
    assert.equal(a.json.novas, 1);
    const b = await req(url, { method: "POST", headers: { Authorization: BASIC, ...H }, body: csv });
    assert.equal(b.status, 200);
    assert.equal(b.json.atualizadas, 1);
  });

  await passo("configuração valida URL e funil", async () => {
    const invalida = await req("/api/config", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ url: "ftp://x" }),
    });
    assert.equal(invalida.status, 400);
    const ok = await req("/api/config", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ url: "https://teste.kommo.com", pipeline: "123", etapas: { agendado: 777 } }),
    });
    assert.equal(ok.status, 200);
    const c = await req("/api/config", { headers: { Authorization: BASIC } });
    assert.equal(c.json.url, "https://teste.kommo.com");
    assert.equal(c.json.etapas.agendado, 777);
  });

  await passo("cadastro de certificado e login por certificado", async () => {
    const { pem, info } = await gerarCert("Cert API");
    const q = `?nome=Cert API&email=cert@teste.com&perfil=admin&arquivo=cert.pem`;
    const r = await req("/api/usuarios" + q, {
      method: "POST",
      headers: { Authorization: BASIC, ...H },
      body: pem,
    });
    assert.equal(r.status, 200);
    assert.equal(r.json.validade, info.fim);

    const headers = {
      "X-Client-Cert": encodeURIComponent(pem),
      "X-Proxy-Secret": "segredo",
    };
    const eu = await req("/api/eu", { headers });
    assert.equal(eu.status, 200);
    assert.equal(eu.json.fonte, "certificado");
    assert.equal(eu.json.perfil, "admin");

    const cfg = await req("/api/auth-config");
    assert.equal(cfg.status, 200);
    assert.equal(cfg.json.cert, true);

    const login = await req("/cert-login?next=/", { headers });
    assert.equal(login.status, 302);
    const cookie = login.headers.get("set-cookie") || "";
    assert.match(cookie, /renovacao_sessao=/);
    const sess = await req("/api/eu", { headers: { Cookie: cookie.split(";")[0] } });
    assert.equal(sess.status, 200);
    assert.equal(sess.json.fonte, "sessao");

    const semCert = await req("/cert-login");
    assert.equal(semCert.status, 302);
    assert.match(semCert.headers.get("location") || "", /erro=cert/);

    const aberto = await req("/cert-login?next=//evil.example", { headers });
    assert.equal(aberto.status, 302);
    assert.ok(!(aberto.headers.get("location") || "").includes("evil.example"));

    const errado = await req("/api/eu", { headers: { ...headers, "X-Proxy-Secret": "x" } });
    assert.equal(errado.status, 401);

    const dup = await req("/api/usuarios" + q, {
      method: "POST",
      headers: { Authorization: BASIC, ...H },
      body: pem,
    });
    assert.equal(dup.status, 409);
  });

  await passo("cadastro só de e-mail (Access)", async () => {
    const q = "?nome=Email API&email=email@teste.com&perfil=operador";
    const r = await req("/api/usuarios" + q, {
      method: "POST",
      headers: { Authorization: BASIC, ...H },
      body: "",
    });
    assert.equal(r.status, 200);
    const eu = await req("/api/eu", {
      headers: { "Cf-Access-Authenticated-User-Email": "email@teste.com" },
    });
    assert.equal(eu.status, 200);
    assert.equal(eu.json.perfil, "operador");
  });

  await passo("cadastro de usuário com senha e login individual", async () => {
    const nome = "Teste Senha " + Date.now();
    const criar = await req("/api/usuarios/senha-criar", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({
        nome,
        email: "teste.senha@teste.com",
        perfil: "operador",
        senha: "segredo123",
      }),
    });
    assert.equal(criar.status, 200);
    const id = criar.json.id;
    assert.ok(id);

    const login = await req("/api/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
        "CF-Connecting-IP": "198.51.100.90",
      },
      body: JSON.stringify({ usuario: nome, senha: "segredo123" }),
    });
    assert.equal(login.status, 200);
    const cookie = login.headers.get("set-cookie") || "";
    const eu = await req("/api/eu", { headers: { Cookie: cookie.split(";")[0] } });
    assert.equal(eu.status, 200);
    assert.equal(eu.json.nome, nome);
    assert.equal(eu.json.perfil, "operador");

    const troca = await req("/api/usuarios/senha", {
      method: "POST",
      headers: { Authorization: BASIC, ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ id, senha: "nova123" }),
    });
    assert.equal(troca.status, 200);

    const velho = await req("/api/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
        "CF-Connecting-IP": "198.51.100.91",
      },
      body: JSON.stringify({ usuario: nome, senha: "segredo123" }),
    });
    assert.equal(velho.status, 401);

    const novo = await req("/api/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch",
        "CF-Connecting-IP": "198.51.100.92",
      },
      body: JSON.stringify({ usuario: nome, senha: "nova123" }),
    });
    assert.equal(novo.status, 200);
  });

  await passo("rota inexistente retorna 404", async () => {
    assert.equal((await req("/api/naoexiste", { headers: { Authorization: BASIC } })).status, 404);
  });

  await passo("limite de tentativas de login", async () => {
    const headers = {
      ...H,
      "Content-Type": "application/json",
      "CF-Connecting-IP": "198.51.100.77",
    };
    for (let i = 0; i < 10; i++) {
      const r = await req("/api/login", {
        method: "POST",
        headers,
        body: JSON.stringify({ usuario: "admin", senha: "errada" }),
      });
      assert.equal(r.status, 401);
    }
    const bloqueado = await req("/api/login", {
      method: "POST",
      headers,
      body: JSON.stringify({ usuario: "admin", senha: "teste" }),
    });
    assert.equal(bloqueado.status, 429);
    assert.ok(bloqueado.headers.get("retry-after"));
  });

  console.log(`\n${passos.length} verificações OK`);
}

main().catch((e) => {
  console.error("\nFALHOU:", e.message);
  console.error(e);
  process.exit(1);
});
