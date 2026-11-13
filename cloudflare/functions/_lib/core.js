// Núcleo da versão Cloudflare Pages: D1 + Pages Functions.
// Porta fiel das regras do renovacao.py (régua, métricas, Kommo, usuários).

export const STATUS = ["a_vencer", "email_enviado", "whatsapp_enviado", "agendado", "emitido", "pago", "perdido"];
export const REGUA = [60, 30, 15, 7, 1, -3]; // limiares do WhatsApp (dias antes/depois do vencimento)
const LIMITE_ATRASO = 30; // WhatsApp não notifica quem venceu há mais de X dias
const EMAIL_INICIO = 30; // primeiro e-mail de vencimento: 30 dias antes
const EMAIL_INTERVALO = 7; // repete o e-mail a cada 7 dias até concluir a renovação
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MES_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const MAX_UPLOAD = 20 * 1024 * 1024;
const MAX_CERT = 1 * 1024 * 1024;

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export const CSP =
  "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; " +
  "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; " +
  "frame-ancestors 'none'; object-src 'none'; media-src 'none'; worker-src 'none'; manifest-src 'self'";

export const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Content-Security-Policy": CSP,
};

export function withSecurityHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [nome, valor] of Object.entries(SECURITY_HEADERS)) headers.set(nome, valor);
  if ((headers.get("Content-Type") || "").includes("text/html")) headers.set("Cache-Control", "no-store");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export const json = (body, status = 200, extra = {}) => {
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  for (const [nome, valor] of Object.entries(extra)) {
    if (nome === "Set-Cookie" && Array.isArray(valor)) {
      for (const cookie of valor) headers.append(nome, cookie);
    } else {
      headers.set(nome, valor);
    }
  }
  for (const [nome, valor] of Object.entries(SECURITY_HEADERS)) headers.set(nome, valor);
  return new Response(JSON.stringify(body), { status, headers });
};

const db = (env) => env.DB;
const all = async (env, sql, ...args) =>
  (await (args.length ? db(env).prepare(sql).bind(...args) : db(env).prepare(sql)).all()).results;
const first = async (env, sql, ...args) =>
  await (args.length ? db(env).prepare(sql).bind(...args) : db(env).prepare(sql)).first();
const run = async (env, sql, ...args) =>
  await (args.length ? db(env).prepare(sql).bind(...args) : db(env).prepare(sql)).run();

function safeEqual(a, b) {
  const x = new TextEncoder().encode(String(a ?? ""));
  const y = new TextEncoder().encode(String(b ?? ""));
  if (x.length !== y.length) return false;
  let r = 0;
  for (let i = 0; i < x.length; i++) r |= x[i] ^ y[i];
  return r === 0;
}

function intOrNull(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isInteger(n) ? n : null;
}

function parseEtapas(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  } catch {
    return {};
  }
}

export function todayISO(env) {
  const tz = env.TZ || "America/Sao_Paulo";
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function diffDays(iso, hoje) {
  if (!iso || !hoje) return null;
  const a = Date.parse(iso + "T00:00:00Z");
  const b = Date.parse(hoje + "T00:00:00Z");
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((a - b) / 86400000);
}

function quandoTexto(dias) {
  if (dias > 0) return `vence em ${dias} dia${dias === 1 ? "" : "s"}`;
  if (dias === 0) return "vence hoje";
  const d = -dias;
  return `venceu há ${d} dia${d === 1 ? "" : "s"}`;
}

async function loadConfig(env) {
  const K = {
    url: env.KOMMO_URL || "",
    token: env.KOMMO_TOKEN || "",
    pipeline: env.KOMMO_PIPELINE || "",
    etapas: parseEtapas(env.KOMMO_ETAPAS),
  };
  const rows = await all(env, "SELECT chave, valor FROM config");
  for (const { chave, valor } of rows) {
    if (["url", "token", "pipeline"].includes(chave) && valor != null) K[chave] = valor;
    else if (chave === "etapas" && valor) {
      const e = parseEtapas(valor);
      if (Object.keys(e).length) K.etapas = e;
    }
  }
  return K;
}

async function log(env, rid, tipo, canal = "", resultado = "", detalhe = "") {
  await run(
    env,
    "INSERT INTO eventos(renovacao_id,ts,tipo,canal,resultado,detalhe) VALUES(?,?,?,?,?,?)",
    rid,
    new Date().toISOString().slice(0, 19),
    tipo,
    canal,
    resultado,
    detalhe,
  );
}

async function kommo(env, K, method, path, body) {
  if (!K.url || !K.token) {
    console.log(`[kommo simulado] ${method} ${path} ${body ? JSON.stringify(body).slice(0, 110) : ""}`);
    return {};
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const resp = await fetch(K.url.replace(/\/+$/, "") + path, {
      method,
      headers: {
        Authorization: `Bearer ${K.token}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: body !== undefined && body !== null ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    if (!resp.ok) throw new Error(`Kommo HTTP ${resp.status}: ${(await resp.text()).slice(0, 400)}`);
    const raw = await resp.text();
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Kommo indisponível: timeout");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function syncKommo(env, K, rid, nota = null) {
  const r = await first(env, "SELECT * FROM renovacoes WHERE id=?", rid);
  if (!r) return false;

  if (!K.url || !K.token) {
    const etapa = K.etapas?.[r.status];
    if (r.kommo_lead_id) await kommo(env, K, "PATCH", `/api/v4/leads/${r.kommo_lead_id}`, { status_id: etapa });
    else
      await kommo(env, K, "POST", "/api/v4/leads", [
        { name: `Renovação ${r.tipo} - ${r.cliente}`, status_id: etapa },
      ]);
    return true;
  }

  const body = { name: `Renovação ${r.tipo} - ${r.cliente}`, price: Math.trunc(Number(r.valor) || 0) };
  const pipeline = intOrNull(K.pipeline);
  if (pipeline) body.pipeline_id = pipeline;
  const etapa = intOrNull(K.etapas?.[r.status]);
  if (etapa) body.status_id = etapa;
  else if (K.etapas && Object.keys(K.etapas).length) {
    await log(env, rid, "kommo", "kommo", "aviso", `sem etapa mapeada para ${r.status}`);
  }

  let lid = r.kommo_lead_id;
  try {
    if (lid) await kommo(env, K, "PATCH", `/api/v4/leads/${lid}`, body);
    else {
      const resp = await kommo(env, K, "POST", "/api/v4/leads", [body]);
      lid = resp?._embedded?.leads?.[0]?.id;
      if (lid) await run(env, "UPDATE renovacoes SET kommo_lead_id=? WHERE id=?", lid, rid);
    }
    if (nota && lid) {
      await kommo(env, K, "POST", `/api/v4/leads/${lid}/notes`, [
        { note_type: "common", params: { text: nota } },
      ]);
    }
  } catch (e) {
    console.log("[kommo] erro:", e.message);
    await log(env, rid, "kommo", "kommo", "falhou", String(e.message).slice(0, 300));
    return false;
  }
  return true;
}

async function avancar(env, K, rid, novo, origem = "sistema", detalhe = "") {
  if (!STATUS.includes(novo)) throw new HttpError(400, `status inválido: ${novo}`);
  const r = await first(env, "SELECT * FROM renovacoes WHERE id=?", rid);
  if (!r || r.status === novo) return false;
  await run(env, "UPDATE renovacoes SET status=? WHERE id=?", novo, rid);
  await log(env, rid, "mudanca_etapa", origem, novo, `${r.status} -> ${novo} ${detalhe}`.trim());
  if (!["kommo", "demo"].includes(origem)) await syncKommo(env, K, rid);
  return true;
}

async function enviarEmail(env, r, dias) {
  const destino = String(r.email || "").trim();
  if (!destino || !EMAIL_RE.test(destino)) {
    await log(env, r.id, "email", "email", "ignorado", "sem e-mail válido");
    return [false, true];
  }
  const quando = quandoTexto(dias);
  const assunto = `Seu certificado ${r.tipo} ${quando}`;
  const corpo =
    `Olá, ${r.cliente}!\n\nSeu certificado digital ${r.tipo} ${quando} (${r.vencimento}). ` +
    `Para renovar sem interrupção, agende aqui: ${env.LINK_AGENDA || "https://exemplo.com/agendar"}\n`;

  if (env.RESEND_API_KEY) {
    try {
      const resp = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          from: env.EMAIL_FROM || "renovacao@exemplo.com",
          to: [destino],
          subject: assunto,
          text: corpo,
        }),
      });
      if (!resp.ok) {
        const detalhe = (await resp.text()).slice(0, 300);
        console.log("[e-mail] falhou:", resp.status, detalhe);
        return [false, resp.status >= 400 && resp.status < 500 && resp.status !== 429];
      }
      return [true, false];
    } catch (e) {
      console.log("[e-mail] falhou:", e.message);
      return [false, false];
    }
  }
  console.log(`[e-mail simulado] ${destino}: ${assunto}`);
  return [true, false];
}

async function enviarWhatsapp(env, K, r, dias) {
  if (!String(r.telefone || "").replace(/\D/g, "") || String(r.telefone).replace(/\D/g, "").length < 10) return false;
  if (!K.url || !K.token) {
    console.log(`[whatsapp simulado] ${r.telefone}: certificado ${quandoTexto(dias)}`);
    return true;
  }
  return syncKommo(
    env,
    K,
    r.id,
    `WhatsApp de renovação: certificado ${r.tipo} ${quandoTexto(dias)} (${r.vencimento})`,
  );
}

async function regua(env, K, hoje = todayISO(env)) {
  const linhas = await all(
    env,
    "SELECT * FROM renovacoes WHERE status NOT IN ('emitido','pago','perdido') ORDER BY vencimento",
  );
  let n = 0;
  for (const r of linhas) {
    const dias = diffDays(r.vencimento, hoje);
    if (dias === null) continue;

    let novo = r.status;
    let notificou = false;

    // E-mail: aviso a partir de D30 e repetição a cada 7 dias, até concluir.
    const temEmail = Boolean(r.email && EMAIL_RE.test(String(r.email).trim()));
    if (temEmail && dias <= EMAIL_INICIO) {
      const ultimo = r.ultimo_email ? String(r.ultimo_email).slice(0, 10) : null;
      const desde = ultimo ? diffDays(hoje, ultimo) : null;
      if (ultimo === null || desde === null || desde >= EMAIL_INTERVALO) {
        const [enviado, permanente] = await enviarEmail(env, r, dias);
        if (enviado) {
          await log(env, r.id, "email", "email", "enviado", `aviso de vencimento (${quandoTexto(dias)})`);
          await run(env, "UPDATE renovacoes SET ultimo_email=? WHERE id=?", hoje, r.id);
          notificou = true;
          if (novo === "a_vencer") novo = "email_enviado";
        } else if (!permanente) {
          await log(env, r.id, "email", "email", "falhou", "erro temporário no envio");
        }
      }
    }

    // WhatsApp: mantém os limiares antigos (D15/D7/D1/D-3), sem repetir.
    if (["a_vencer", "email_enviado", "whatsapp_enviado"].includes(r.status) && dias >= -LIMITE_ATRASO) {
      // menor limiar ainda não ultrapassado (REGUA é decrescente)
      let alvo = null;
      for (const d of REGUA) if (dias <= d) alvo = d;
      if (alvo !== null) {
        const ultima = r.regua_ultima === null || r.regua_ultima === undefined ? 1e6 : Number(r.regua_ultima);
        if (alvo < ultima) {
          const temFone = String(r.telefone || "").replace(/\D/g, "").length >= 10 && alvo <= 15;
          if (!temEmail && !temFone) {
            await log(env, r.id, "regua", "sistema", "ignorado", "sem contato válido");
            await run(env, "UPDATE renovacoes SET regua_ultima=? WHERE id=?", alvo, r.id);
            continue;
          }
          if (temFone) {
            if (await enviarWhatsapp(env, K, r, dias)) {
              await log(env, r.id, "whatsapp", "whatsapp", "enviado", `régua D${alvo >= 0 ? "+" : ""}${alvo} (faltam ${dias} dias)`);
              await run(env, "UPDATE renovacoes SET regua_ultima=? WHERE id=?", alvo, r.id);
              notificou = true;
              if (STATUS.indexOf("whatsapp_enviado") > STATUS.indexOf(novo)) novo = "whatsapp_enviado";
            } else {
              await log(env, r.id, "whatsapp", "whatsapp", "falhou", "erro no envio");
            }
          }
        }
      }
    }

    if (notificou && novo !== r.status) {
      await avancar(env, K, r.id, novo, "regua");
    }
    if (notificou) n++;
  }
  return n;
}

async function metricas(env, mes, hoje = todayISO(env)) {
  if (mes && !MES_RE.test(mes)) throw new HttpError(400, "mês inválido: use AAAA-MM");
  const filtroMes = mes ? "WHERE substr(vencimento,1,7)=?" : "";
  const funil = Object.fromEntries(STATUS.map((s) => [s, 0]));
  for (const row of await all(
    env,
    `SELECT status, COUNT(*) AS n FROM renovacoes ${filtroMes} GROUP BY 1`,
    ...(mes ? [mes] : []),
  )) {
    funil[row.status] = row.n;
  }

  const base = "(vencimento<=? OR status='pago')";
  const taxa = (g, filtro, p) =>
    all(
      env,
      `SELECT ${g} AS grupo, COUNT(*) AS total,
        COALESCE(SUM(status='pago'),0) AS renovados,
        COALESCE(ROUND(100.0*SUM(status='pago')/COUNT(*),1),0) AS taxa,
        COALESCE(SUM(CASE WHEN status='pago' THEN valor END),0) AS receita
       FROM renovacoes WHERE ${filtro} GROUP BY 1 ORDER BY 1`,
      ...p,
    );

  const g = await taxa("'x'", mes ? "substr(vencimento,1,7)=?" : base, mes ? [mes] : [hoje]);
  return {
    funil,
    total: g[0]?.total ?? 0,
    taxa_geral: g[0]?.taxa ?? 0,
    receita: g[0]?.receita ?? 0,
    por_mes: await taxa("substr(vencimento,1,7)", base, [hoje]),
    por_tipo: await taxa("tipo", base, [hoje]),
  };
}

async function certInfoFromHeader(raw) {
  const { certInfo } = await import("./certs.js");
  let dado = raw;
  try {
    dado = decodeURIComponent(raw);
  } catch {
    /* header não estava urlencoded */
  }
  if (!String(dado).includes("BEGIN CERTIFICATE")) {
    try {
      dado = Uint8Array.from(atob(String(dado).replace(/\s+/g, "")), (c) => c.charCodeAt(0));
    } catch {
      /* tenta como DER textual */
    }
  }
  return certInfo("", dado);
}

const SESSION_COOKIE = "renovacao_sessao";
const SESSION_COOKIE_HOST = "__Host-renovacao_sessao";
const SESSION_TTL = 12 * 3600; // 12 horas

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(texto) {
  let s = String(texto).replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

const SENHA_ITER = 25000;
const SENHA_MIN = 6;
const SENHA_MAX = 200;

function safeEqualBytes(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a[i] ^ b[i];
  return r === 0;
}

async function hashSenha(senha) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const chave = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(senha)),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: SENHA_ITER, hash: "SHA-256" },
    chave,
    256,
  );
  return `pbkdf2$${SENHA_ITER}$${b64url(salt)}$${b64url(new Uint8Array(bits))}`;
}

async function verificarSenha(senha, guardado) {
  const partes = String(guardado || "").split("$");
  if (partes.length !== 4 || partes[0] !== "pbkdf2") return false;
  const iter = Number(partes[1]);
  if (!Number.isInteger(iter) || iter < 1000 || iter > 1000000) return false;
  let salt;
  let esperado;
  try {
    salt = b64urlDecode(partes[2]);
    esperado = b64urlDecode(partes[3]);
  } catch {
    return false;
  }
  if (!esperado.length) return false;
  const chave = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(senha)),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: iter, hash: "SHA-256" },
    chave,
    esperado.length * 8,
  );
  return safeEqualBytes(new Uint8Array(bits), esperado);
}

function getCookie(request, nome) {
  const raw = request.headers.get("Cookie") || "";
  for (const parte of raw.split(";")) {
    const i = parte.indexOf("=");
    if (i < 0) continue;
    if (parte.slice(0, i).trim() !== nome) continue;
    try {
      return decodeURIComponent(parte.slice(i + 1).trim());
    } catch {
      return parte.slice(i + 1).trim();
    }
  }
  return null;
}

async function chaveSessao(env) {
  const segredo = env.SESSION_SECRET
    ? env.SESSION_SECRET + "|" + (env.PAINEL_SENHA || "")
    : env.PAINEL_SENHA || "dev";
  return await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(segredo),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

function tokenAleatorio(bytes = 16) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

function b64urlJson(obj) {
  return b64url(new TextEncoder().encode(JSON.stringify(obj)));
}

export async function criarSessao(env, user) {
  const agora = Math.floor(Date.now() / 1000);
  const header = b64urlJson({ alg: "HS256", typ: "JWT" });
  const payload = b64urlJson({
    sub: user.nome,
    id: user.id ?? null,
    role: user.perfil,
    iat: agora,
    exp: agora + SESSION_TTL,
    csrf: tokenAleatorio(16),
  });
  const dados = header + "." + payload;
  const chave = await chaveSessao(env);
  const assinatura = await crypto.subtle.sign("HMAC", chave, new TextEncoder().encode(dados));
  return dados + "." + b64url(new Uint8Array(assinatura));
}

export async function lerSessao(env, token) {
  if (!token || token.split(".").length !== 3) return null;
  const [header, payload, assinatura] = token.split(".");
  try {
    const cabecalho = JSON.parse(new TextDecoder().decode(b64urlDecode(header)));
    if (!cabecalho || cabecalho.alg !== "HS256") return null;
    const chave = await chaveSessao(env);
    const ok = await crypto.subtle.verify(
      "HMAC",
      chave,
      b64urlDecode(assinatura),
      new TextEncoder().encode(header + "." + payload),
    );
    if (!ok) return null;
    const dados = JSON.parse(new TextDecoder().decode(b64urlDecode(payload)));
    const agora = Math.floor(Date.now() / 1000);
    if (!dados || !dados.exp || dados.exp < agora || !dados.iat || dados.iat > agora + 60) return null;
    return {
      nome: dados.sub || "Administrador",
      id: Number.isInteger(dados.id) ? dados.id : null,
      perfil: dados.role === "admin" ? "admin" : "operador",
      validade: null,
      fonte: "sessao",
      csrf: typeof dados.csrf === "string" ? dados.csrf : "",
    };
  } catch {
    return null;
  }
}

const LOGIN_MAX = 10;
const LOGIN_JANELA = 15 * 60; // 15 minutos

async function chaveRate(env, request) {
  const ip = (request.headers.get("CF-Connecting-IP") || request.headers.get("X-Forwarded-For") || "local")
    .split(",")[0]
    .trim();
  const sal = env.SESSION_SECRET || env.PAINEL_SENHA || "dev";
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ip + "|" + sal));
  return Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function bloqueioLogin(env, chave) {
  const agora = Math.floor(Date.now() / 1000);
  const row = await first(env, "SELECT contagem, expira FROM login_tentativas WHERE chave=?", chave);
  if (!row) return 0;
  if (Number(row.expira) <= agora) {
    await run(env, "DELETE FROM login_tentativas WHERE chave=?", chave);
    return 0;
  }
  return Number(row.contagem) >= LOGIN_MAX ? Math.max(1, Number(row.expira) - agora) : 0;
}

async function registrarFalha(env, chave) {
  const agora = Math.floor(Date.now() / 1000);
  const expira = agora + LOGIN_JANELA;
  await run(
    env,
    `INSERT INTO login_tentativas(chave,contagem,expira) VALUES(?,1,?)
     ON CONFLICT(chave) DO UPDATE SET
       contagem = CASE WHEN login_tentativas.expira <= ? THEN 1 ELSE login_tentativas.contagem + 1 END,
       expira = CASE WHEN login_tentativas.expira <= ? THEN ? ELSE login_tentativas.expira END`,
    chave,
    expira,
    agora,
    agora,
    expira,
  );
  await run(env, "DELETE FROM login_tentativas WHERE expira < ?", agora - 3600);
}

async function limparFalhas(env, chave) {
  await run(env, "DELETE FROM login_tentativas WHERE chave=?", chave);
}

async function autenticarCertificado(env, request) {
  const header = env.CERT_HEADER || "X-Client-Cert";
  const raw = request.headers.get(header) || "";
  const segredo = request.headers.get("X-Proxy-Secret") || "";
  if (!raw) return { ok: false, status: 401, body: { erro: "certificado não apresentado" } };
  if (!env.PROXY_SECRET || !safeEqual(segredo, env.PROXY_SECRET)) {
    return { ok: false, status: 401, body: { erro: "proxy não autorizado" } };
  }
  let info = null;
  try {
    info = await certInfoFromHeader(raw);
  } catch {
    info = null;
  }
  if (!info) return { ok: false, status: 403, body: { erro: "certificado inválido" } };
  const hj = todayISO(env);
  const u = await first(env, "SELECT * FROM usuarios WHERE fp=? AND ativo=1", info.fp);
  if (!u || !(info.ini <= hj && hj <= info.fim)) {
    return { ok: false, status: 403, body: { erro: "certificado não autorizado, inativo ou vencido" } };
  }
  return {
    ok: true,
    user: { id: u.id, nome: u.nome, perfil: u.perfil, validade: u.validade, fonte: "certificado" },
  };
}

export async function authenticate(env, request, url) {
  if (url.pathname.startsWith("/webhook/")) return { ok: true, user: null };
  if (url.pathname === "/api/login") return { ok: true, user: null };
  if (url.pathname === "/api/auth-config") return { ok: true, user: null };

  // chamada automática da régua (cron externo) com token dedicado
  if (
    url.pathname === "/api/regua" &&
    request.method === "POST" &&
    env.CRON_TOKEN &&
    safeEqual(url.searchParams.get("t") || "", env.CRON_TOKEN)
  ) {
    return { ok: true, user: { nome: "cron", perfil: "admin", validade: null, fonte: "cron" } };
  }

  const senha = env.PAINEL_SENHA || "";
  const certOn = env.AUTH_CERT === "1";
  const accessOn = env.TRUST_ACCESS === "1";
  const accessEmail = (request.headers.get("Cf-Access-Authenticated-User-Email") || "").trim();

  if (!senha && !certOn && !accessOn) {
    // proteção contra painel aberto em produção
    if (url.hostname.endsWith(".pages.dev") && env.ALLOW_PUBLIC !== "1") {
      return {
        ok: false,
        status: 503,
        body: {
          erro:
            "Painel sem autenticação. Defina PAINEL_SENHA, AUTH_CERT=1 ou TRUST_ACCESS=1 " +
            "(ou ALLOW_PUBLIC=1 para deixar público).",
        },
      };
    }
    return { ok: true, user: { nome: "local", perfil: "admin", validade: null } };
  }

  // sessão criada pelo formulário de login (cookie HMAC)
  if (senha || env.SESSION_SECRET) {
    const sess = await lerSessao(
      env,
      getCookie(request, SESSION_COOKIE_HOST) || getCookie(request, SESSION_COOKIE),
    );
    if (sess) return { ok: true, user: sess };
  }

  const auth = request.headers.get("Authorization") || "";
  if (senha && auth.startsWith("Basic ")) {
    try {
      const dec = atob(auth.slice(6));
      const i = dec.indexOf(":");
      const usuario = i >= 0 ? dec.slice(0, i) : "";
      const informada = i >= 0 ? dec.slice(i + 1) : "";
      if (safeEqual(usuario, env.PAINEL_USER || "admin") && safeEqual(informada, senha)) {
        return { ok: true, user: { nome: "Administrador", perfil: "admin", validade: null } };
      }
    } catch {
      /* header inválido */
    }
  }

  // Cloudflare Access (e-mail autenticado pelo Access)
  if (accessOn && accessEmail) {
    const u = await first(
      env,
      "SELECT * FROM usuarios WHERE ativo=1 AND lower(email)=lower(?) ORDER BY (perfil='admin') DESC LIMIT 1",
      accessEmail,
    );
    if (u) {
      return {
        ok: true,
        user: { id: u.id, nome: u.nome, perfil: u.perfil, validade: u.validade, fonte: "access" },
      };
    }
    return { ok: false, status: 403, body: { erro: "e-mail do Access não cadastrado ou inativo" } };
  }

  // certificado digital (header definido pelo proxy/mTLS)
  if (certOn) {
    const cert = await autenticarCertificado(env, request);
    if (cert.ok) return cert;
    if (cert.status !== 401) return cert;
  }

  const extra = senha ? { "WWW-Authenticate": 'Basic realm="Renovacoes", charset="UTF-8"' } : {};
  return { ok: false, status: 401, body: { erro: "autenticação necessária" }, extra };
}

function destinoSeguro(next) {
  if (!next) return "/";
  try {
    const u = new URL(next, "https://local.invalid");
    if (u.origin !== "https://local.invalid") return "/";
    return u.pathname + u.search + u.hash;
  } catch {
    return "/";
  }
}

function redirecionar(url, destino) {
  return withSecurityHeaders(
    new Response(null, { status: 302, headers: { Location: new URL(destino, url).toString() } }),
  );
}

// Recebe o certificado encaminhado pelo proxy mTLS, cria a sessão e volta ao painel.
export async function certLogin(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const destino = destinoSeguro(url.searchParams.get("next"));
  if (!env.DB) return redirecionar(url, "/login?erro=cert");
  if (env.AUTH_CERT !== "1") return redirecionar(url, "/login?erro=cert_off");
  if (!env.SESSION_SECRET && !env.PAINEL_SENHA) return redirecionar(url, "/login?erro=cert_off");

  const cert = await autenticarCertificado(env, request);
  if (!cert.ok) {
    console.warn("cert-login:", cert.status, cert.body?.erro || "");
    return redirecionar(url, "/login?erro=cert");
  }

  const token = await criarSessao(env, cert.user);
  const seguro = url.protocol === "https:";
  const nome = seguro ? SESSION_COOKIE_HOST : SESSION_COOKIE;
  const secure = seguro ? "; Secure" : "";
  const headers = new Headers({
    Location: new URL(destino, url).toString(),
    "Set-Cookie":
      `${nome}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; ` +
      `Max-Age=${SESSION_TTL}${secure}`,
  });
  return withSecurityHeaders(new Response(null, { status: 302, headers }));
}

async function rotaGet(env, K, url, user, admin) {
  const p = url.pathname;
  if (p === "/api/auth-config") {
    return json({
      cert:
        env.AUTH_CERT === "1" &&
        Boolean(env.PROXY_SECRET) &&
        Boolean(env.SESSION_SECRET || env.PAINEL_SENHA),
      certLoginUrl: env.CERT_LOGIN_URL || "",
    });
  }
  const mes = url.searchParams.get("mes");
  if (mes && !MES_RE.test(mes)) throw new HttpError(400, "mês inválido: use AAAA-MM");

  if (p === "/api/metricas") return json(await metricas(env, mes));
  if (p === "/api/renovacoes") {
    const sql =
      "SELECT * FROM renovacoes" +
      (mes ? " WHERE substr(vencimento,1,7)=?" : "") +
      " ORDER BY vencimento";
    return json(mes ? await all(env, sql, mes) : await all(env, sql));
  }
  if (p === "/api/historico") {
    const rid = intOrNull(url.searchParams.get("id"));
    if (rid === null) throw new HttpError(400, "id inválido");
    return json(await all(env, "SELECT * FROM eventos WHERE renovacao_id=? ORDER BY id", rid));
  }
  if (p === "/api/eu") return json(user);
  if (p === "/api/usuarios") {
    if (!admin) throw new HttpError(403, "apenas administradores");
    return json(
      await all(
        env,
        "SELECT id,nome,email,perfil,sujeito,validade,ativo,(senha_hash IS NOT NULL) AS senha_definida FROM usuarios ORDER BY nome",
      ),
    );
  }
  if (p === "/api/config") {
    return json({
      url: K.url,
      pipeline: K.pipeline,
      etapas: K.etapas,
      token_definido: Boolean(K.token),
    });
  }
  throw new HttpError(404, "não encontrado");
}

async function cadastrarUsuario(env, url, body) {
  const nome = (url.searchParams.get("nome") || "").trim().slice(0, 120);
  if (!nome) throw new HttpError(400, "informe o nome");
  const email = (url.searchParams.get("email") || "").trim().slice(0, 200) || null;
  const perfil = (url.searchParams.get("perfil") || "").trim() === "admin" ? "admin" : "operador";
  const arquivo = (url.searchParams.get("arquivo") || "").trim();

  // usuário só de e-mail (login via Cloudflare Access), sem certificado
  if (body.byteLength === 0) {
    if (!email || !EMAIL_RE.test(email)) throw new HttpError(400, "informe um e-mail válido");
    const ins = await first(
      env,
      "INSERT INTO usuarios(nome,email,perfil,fp,sujeito,validade) VALUES(?,?,?,NULL,NULL,NULL) RETURNING id",
      nome,
      email,
      perfil,
    );
    return json({ ok: true, id: ins?.id ?? null, validade: null });
  }

  let info;
  try {
    const { certInfo } = await import("./certs.js");
    info = await certInfo(arquivo, body);
  } catch (e) {
    throw new HttpError(400, e.message || "certificado inválido");
  }

  try {
    const ins = await first(
      env,
      "INSERT INTO usuarios(nome,email,perfil,fp,sujeito,validade) VALUES(?,?,?,?,?,?) RETURNING id",
      nome,
      email,
      perfil,
      info.fp,
      info.sujeito,
      info.fim,
    );
    return json({ ok: true, id: ins?.id ?? null, validade: info.fim });
  } catch (e) {
    if (String(e).toUpperCase().includes("UNIQUE")) {
      throw new HttpError(409, "este certificado já está cadastrado");
    }
    throw e;
  }
}

async function importar(env, url, body) {
  const nome = (url.searchParams.get("nome") || "up.xlsx").split(/[\\/]/).pop() || "up.xlsx";
  const ext = nome.toLowerCase();
  if (![".xlsx", ".xls", ".xlsm", ".csv"].some((e) => ext.endsWith(e))) {
    throw new HttpError(400, "use .xlsx, .xls ou .csv");
  }
  const { lerLinhas } = await import("./import.js");
  const { linhas, ignoradas, avisos, detalhes } = lerLinhas(body, nome);
  let novas = 0;
  let atualizadas = 0;
  let ignoradasTotal = ignoradas;

  for (const l of linhas) {
    const email = l.email && EMAIL_RE.test(l.email) ? l.email : null;
    const atual = await first(
      env,
      "SELECT id FROM renovacoes WHERE cliente=? AND tipo=? AND vencimento=?",
      l.cliente,
      l.tipo,
      l.vencimento,
    );
    if (atual) {
      await run(
        env,
        "UPDATE renovacoes SET email=COALESCE(?,email), telefone=COALESCE(?,telefone), valor=COALESCE(?,valor) WHERE id=?",
        email,
        l.telefone,
        l.valor,
        atual.id,
      );
      await log(env, atual.id, "atualizado", "planilha");
      atualizadas++;
    } else {
      const ins = await first(
        env,
        "INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor) VALUES(?,?,?,?,?,?) RETURNING id",
        l.cliente,
        email,
        l.telefone,
        l.tipo,
        l.vencimento,
        l.valor,
      );
      if (ins) {
        await log(env, ins.id, "importado", "planilha");
        novas++;
      } else {
        ignoradasTotal++;
      }
    }
  }
  return json({ novas, atualizadas, ignoradas: ignoradasTotal, avisos, detalhes });
}

async function criarRenovacao(env, body) {
  let d;
  try {
    d = JSON.parse(new TextDecoder().decode(body) || "{}");
  } catch {
    throw new HttpError(400, "JSON inválido");
  }
  if (!d || typeof d !== "object" || Array.isArray(d)) throw new HttpError(400, "JSON inválido");
  const cliente = String(d.cliente || "").trim().slice(0, 200);
  const tipo = String(d.tipo || "").trim().slice(0, 80);
  const venc = String(d.vencimento || "").trim();
  if (!cliente || !tipo || !venc) throw new HttpError(400, "cliente, tipo e vencimento são obrigatórios");
  let dt = null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(venc)) dt = venc;
  else if (/^\d{2}\/\d{2}\/\d{4}$/.test(venc)) {
    const [dd, mm, yyyy] = venc.split("/");
    dt = `${yyyy}-${mm}-${dd}`;
  }
  if (!dt) throw new HttpError(400, "vencimento inválido");
  let valor = null;
  if (d.valor !== undefined && d.valor !== null && d.valor !== "") {
    valor = Number(d.valor);
    if (!Number.isFinite(valor)) throw new HttpError(400, "valor inválido");
  }
  const email = String(d.email || "").trim().slice(0, 200) || null;
  const telefone = String(d.telefone || "").trim().slice(0, 40) || null;
  try {
    const ins = await first(
      env,
      "INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor) VALUES(?,?,?,?,?,?) RETURNING id",
      cliente,
      email,
      telefone,
      tipo,
      dt,
      valor,
    );
    if (!ins) throw new HttpError(500, "não foi possível criar");
    const row = await first(env, "SELECT * FROM renovacoes WHERE id=?", ins.id);
    await log(env, ins.id, "criado", "painel");
    return json(row, 201);
  } catch (e) {
    if (String(e).toUpperCase().includes("UNIQUE")) {
      throw new HttpError(409, "já existe uma renovação com esse cliente, tipo e vencimento");
    }
    throw e;
  }
}

async function salvarConfig(env, d) {
  if ("url" in d) {
    const u = String(d.url || "").trim();
    if (u && !/^https?:\/\//i.test(u)) throw new HttpError(400, "a URL deve começar com http:// ou https://");
    await run(env, "INSERT OR REPLACE INTO config(chave,valor) VALUES('url',?)", u);
  }
  if ("pipeline" in d) {
    const p = String(d.pipeline || "").trim();
    if (p && !/^\d+$/.test(p)) throw new HttpError(400, "o ID do funil deve ser numérico");
    await run(env, "INSERT OR REPLACE INTO config(chave,valor) VALUES('pipeline',?)", p);
  }
  if (d.token) {
    await run(env, "INSERT OR REPLACE INTO config(chave,valor) VALUES('token',?)", String(d.token).trim());
  }
  if (d.limpar_token) await run(env, "DELETE FROM config WHERE chave='token'");
  if ("etapas" in d) {
    const etapas = {};
    if (d.etapas && typeof d.etapas === "object") {
      for (const [k, v] of Object.entries(d.etapas)) {
        if (STATUS.includes(k) && /^\d+$/.test(String(v).trim())) etapas[k] = Number(v);
      }
    }
    await run(env, "INSERT OR REPLACE INTO config(chave,valor) VALUES('etapas',?)", JSON.stringify(etapas));
  }
  return json({ ok: true });
}

async function rotaPost(env, K, url, request, user, admin) {
  const p = url.pathname;
  const limite = p === "/api/usuarios" ? MAX_CERT : MAX_UPLOAD;
  const cl = Number(request.headers.get("Content-Length") || 0);
  if (cl > limite) throw new HttpError(413, "corpo grande demais");
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > limite) throw new HttpError(413, "corpo grande demais");

  // CSRF: navegador não consegue mandar este header cross-site sem preflight
  if (request.headers.get("X-Requested-With") !== "fetch") throw new HttpError(403, "requisição inválida");

  // dupla proteção para sessões por cookie: token sincronizado com o JWT
  if (user?.fonte === "sessao") {
    const csrf = request.headers.get("X-CSRF-Token") || "";
    if (!safeEqual(csrf, user.csrf || "")) throw new HttpError(403, "CSRF token inválido");
  }

  if (p === "/api/login") {
    const origem = request.headers.get("Origin");
    if (origem && origem !== url.origin) throw new HttpError(403, "origem inválida");
    const chave = await chaveRate(env, request);
    const espera = await bloqueioLogin(env, chave);
    if (espera) {
      return json({ erro: "muitas tentativas; tente novamente mais tarde" }, 429, {
        "Retry-After": String(espera),
      });
    }
    let d;
    try {
      d = JSON.parse(new TextDecoder().decode(body) || "{}");
    } catch {
      throw new HttpError(400, "JSON inválido");
    }
    const usuario = String(d?.usuario || "admin").trim();
    const informada = String(d?.senha || "");

    let conta = null;
    // senha de emergência do administrador (PAINEL_SENHA)
    if (
      env.PAINEL_SENHA &&
      safeEqual(usuario, env.PAINEL_USER || "admin") &&
      safeEqual(informada, env.PAINEL_SENHA)
    ) {
      conta = { nome: env.PAINEL_USER || "admin", perfil: "admin", validade: null };
    } else if (informada) {
      // senha individual gravada na tabela usuarios
      const u = await first(
        env,
        "SELECT * FROM usuarios WHERE ativo=1 AND (lower(nome)=lower(?) OR (email IS NOT NULL AND lower(email)=lower(?))) ORDER BY (perfil='admin') DESC, id LIMIT 1",
        usuario,
        usuario,
      );
      if (u?.senha_hash && (await verificarSenha(informada, u.senha_hash))) {
        conta = { id: u.id, nome: u.nome, perfil: u.perfil, validade: u.validade };
      }
    }

    if (!conta) {
      await registrarFalha(env, chave);
      throw new HttpError(401, "usuário ou senha inválidos");
    }
    await limparFalhas(env, chave);
    const token = await criarSessao(env, conta);
    const seguro = url.protocol === "https:";
    const nome = seguro ? SESSION_COOKIE_HOST : SESSION_COOKIE;
    const secure = seguro ? "; Secure" : "";
    return json({ ok: true }, 200, {
      "Set-Cookie": `${nome}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_TTL}${secure}`,
    });
  }

  if (p === "/api/logout") {
    const seguro = url.protocol === "https:";
    const secure = seguro ? "; Secure" : "";
    const atributos = `Path=/; HttpOnly; SameSite=Lax; Max-Age=0${secure}`;
    return json({ ok: true }, 200, {
      "Set-Cookie": [
        `${SESSION_COOKIE}=; ${atributos}`,
        `${SESSION_COOKIE_HOST}=; ${atributos}`,
      ],
    });
  }

  if (p === "/api/importar") return await importar(env, url, body);
  if (p === "/api/renovacoes") return await criarRenovacao(env, body);

  if (p === "/api/usuarios") {
    if (!admin) throw new HttpError(403, "apenas administradores");
    return await cadastrarUsuario(env, url, body);
  }
  if (p.startsWith("/api/usuarios")) {
    if (!admin) throw new HttpError(403, "apenas administradores");
  }

  let d;
  try {
    d = JSON.parse(new TextDecoder().decode(body) || "{}");
  } catch {
    throw new HttpError(400, "JSON inválido");
  }
  if (!d || typeof d !== "object" || Array.isArray(d)) throw new HttpError(400, "JSON inválido");

  if (p === "/api/usuarios/senha") {
    const uid = intOrNull(d.id);
    if (uid === null) throw new HttpError(400, "id inválido");
    const senha = String(d.senha || "");
    if (senha.length < SENHA_MIN || senha.length > SENHA_MAX) {
      throw new HttpError(400, `a senha deve ter entre ${SENHA_MIN} e ${SENHA_MAX} caracteres`);
    }
    const atualizado = await first(
      env,
      "UPDATE usuarios SET senha_hash=? WHERE id=? RETURNING id",
      await hashSenha(senha),
      uid,
    );
    if (!atualizado) throw new HttpError(404, "usuário não encontrado");
    return json({ ok: true });
  }

  if (p === "/api/usuarios/senha-criar") {
    const nome = String(d.nome || "").trim().slice(0, 120);
    if (!nome) throw new HttpError(400, "informe o nome");
    const email = String(d.email || "").trim().slice(0, 200) || null;
    if (email && !EMAIL_RE.test(email)) throw new HttpError(400, "informe um e-mail válido");
    const perfil = String(d.perfil || "") === "admin" ? "admin" : "operador";
    const senha = String(d.senha || "");
    if (senha.length < SENHA_MIN || senha.length > SENHA_MAX) {
      throw new HttpError(400, `a senha deve ter entre ${SENHA_MIN} e ${SENHA_MAX} caracteres`);
    }
    const existe = await first(env, "SELECT id FROM usuarios WHERE lower(nome)=lower(?) LIMIT 1", nome);
    if (existe) throw new HttpError(409, "já existe um usuário com esse nome");
    const ins = await first(
      env,
      "INSERT INTO usuarios(nome,email,perfil,senha_hash) VALUES(?,?,?,?) RETURNING id",
      nome,
      email,
      perfil,
      await hashSenha(senha),
    );
    return json({ ok: true, id: ins?.id ?? null });
  }

  if (p === "/api/avancar") {
    const rid = intOrNull(d.id);
    if (rid === null) throw new HttpError(400, "id inválido");
    if (!STATUS.includes(d.status)) throw new HttpError(400, "status inválido");
    await avancar(env, K, rid, d.status, "painel");
    return json({ ok: true });
  }
  if (p === "/api/evento") {
    const rid = intOrNull(d.id);
    if (rid === null) throw new HttpError(400, "id inválido");
    await log(
      env,
      rid,
      String(d.tipo || "contato").slice(0, 40),
      String(d.canal || "").slice(0, 40),
      String(d.resultado || "").slice(0, 40),
      String(d.detalhe || "").slice(0, 500),
    );
    return json({ ok: true });
  }
  if (p === "/api/regua") return json({ notificadas: await regua(env, K) });
  if (p === "/api/usuarios/ativo") {
    const uid = intOrNull(d.id);
    if (uid === null) throw new HttpError(400, "id inválido");
    if (uid === user?.id) throw new HttpError(400, "não é possível desativar o próprio usuário");
    await run(env, "UPDATE usuarios SET ativo=? WHERE id=?", d.ativo ? 1 : 0, uid);
    return json({ ok: true });
  }
  if (p === "/api/usuarios/remover") {
    const uid = intOrNull(d.id);
    if (uid === null) throw new HttpError(400, "id inválido");
    if (uid === user?.id) throw new HttpError(400, "não é possível remover o próprio usuário");
    await run(env, "DELETE FROM usuarios WHERE id=?", uid);
    return json({ ok: true });
  }
  if (p === "/api/config") return await salvarConfig(env, d);
  if (p === "/api/kommo/testar") {
    if (!K.url || !K.token) return json({ msg: "Sem URL/token: modo simulação" });
    try {
      await kommo(env, K, "GET", "/api/v4/account");
      return json({ msg: "Conexão OK" });
    } catch (e) {
      return json({ msg: `Falhou: ${e.message}` });
    }
  }
  throw new HttpError(404, "não encontrado");
}

export async function handleApi(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  try {
    const method = request.method.toUpperCase();
    // API é same-origin: não emitimos Access-Control-Allow-Origin de propósito.
    if (method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          Allow: "GET, POST, OPTIONS",
          "Cache-Control": "no-store",
          ...SECURITY_HEADERS,
        },
      });
    }
    if (!env.DB) return json({ erro: "binding D1 (DB) não configurado" }, 500);
    const auth = await authenticate(env, request, url);
    if (!auth.ok) return json(auth.body, auth.status, auth.extra);

    const user = auth.user;
    const admin = user?.perfil === "admin";
    const K = await loadConfig(env);
    if (method === "GET") return await rotaGet(env, K, url, user, admin);
    if (method === "POST") return await rotaPost(env, K, url, request, user, admin);
    return json({ erro: "método não permitido" }, 405, { Allow: "GET, POST, OPTIONS" });
  } catch (e) {
    if (e instanceof HttpError) return json({ erro: e.message }, e.status);
    console.error("erro API:", e);
    return json({ erro: "erro interno" }, 500);
  }
}

export async function handleWebhook(context) {
  const { request, env } = context;
  try {
    if (!env.DB) return json({ erro: "binding D1 (DB) não configurado" }, 500);
    const url = new URL(request.url);
    const esperado = env.WEBHOOK_TOKEN || "";
    if (esperado && !safeEqual(url.searchParams.get("t") || "", esperado)) {
      return json({ erro: "token inválido" }, 403);
    }

    const K = await loadConfig(env);
    const corpo = await request.text();
    const ctype = request.headers.get("Content-Type") || "";
    const pares = [];

    if (ctype.includes("application/json")) {
      let dados;
      try {
        dados = JSON.parse(corpo || "{}");
      } catch {
        return json({ erro: "JSON inválido" }, 400);
      }
      const leads = dados?.leads?.status;
      if (Array.isArray(leads)) {
        for (const x of leads) {
          if (x && typeof x === "object") pares.push([String(x.id), String(x.status_id)]);
        }
      }
    } else {
      const f = new URLSearchParams(corpo);
      for (let i = 0; f.has(`leads[status][${i}][id]`); i++) {
        pares.push([
          f.get(`leads[status][${i}][id]`),
          f.get(`leads[status][${i}][status_id]`) || "",
        ]);
      }
    }

    const inv = {};
    for (const [k, v] of Object.entries(K.etapas || {})) {
      const n = intOrNull(v);
      if (n !== null) inv[String(n)] = k;
    }

    for (const [lid, st] of pares) {
      const r = await first(env, "SELECT id FROM renovacoes WHERE kommo_lead_id=?", lid);
      const status = String(st).trim();
      if (r && inv[status]) {
        try {
          await avancar(env, K, r.id, inv[status], "kommo");
        } catch (e) {
          if (!(e instanceof HttpError)) throw e;
        }
      }
    }
    return json({ ok: true });
  } catch (e) {
    console.error("erro webhook:", e);
    return json({ erro: "erro interno" }, 500);
  }
}
