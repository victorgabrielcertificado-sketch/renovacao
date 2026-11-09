// Núcleo da versão Cloudflare Pages: D1 + Pages Functions.
// Porta fiel das regras do renovacao.py (régua, métricas, Kommo, usuários).

export const STATUS = ["a_vencer", "email_enviado", "whatsapp_enviado", "agendado", "emitido", "pago", "perdido"];
export const REGUA = [60, 30, 15, 7, 1, -3]; // dias antes/depois do vencimento
const LIMITE_ATRASO = 30;
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

export const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      ...extra,
    },
  });

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
    "SELECT * FROM renovacoes WHERE status IN ('a_vencer','email_enviado','whatsapp_enviado') ORDER BY vencimento",
  );
  let n = 0;
  for (const r of linhas) {
    const dias = diffDays(r.vencimento, hoje);
    if (dias === null || dias < -LIMITE_ATRASO) continue;

    // menor limiar ainda não ultrapassado (REGUA é decrescente)
    let alvo = null;
    for (const d of REGUA) if (dias <= d) alvo = d;
    if (alvo === null) continue;

    const ultima = r.regua_ultima === null || r.regua_ultima === undefined ? 1e6 : Number(r.regua_ultima);
    if (alvo >= ultima) continue;

    const rotulo = `D${alvo >= 0 ? "+" : ""}${alvo}`;
    const temEmail = Boolean(r.email && EMAIL_RE.test(String(r.email).trim()));
    const temFone = String(r.telefone || "").replace(/\D/g, "").length >= 10 && alvo <= 15;

    if (!temEmail && !temFone) {
      await log(env, r.id, "regua", "sistema", "ignorado", "sem contato válido");
      await run(env, "UPDATE renovacoes SET regua_ultima=? WHERE id=?", alvo, r.id);
      continue;
    }

    let transitorio = false;
    let enviados = 0;
    if (temEmail) {
      const [enviado, permanente] = await enviarEmail(env, r, dias);
      if (enviado) {
        await log(env, r.id, "email", "email", "enviado", `régua ${rotulo} (faltam ${dias} dias)`);
        enviados++;
      } else if (!permanente) {
        transitorio = true;
        await log(env, r.id, "email", "email", "falhou", "erro temporário no envio");
      }
    }

    let novo = r.status;
    if (temFone) {
      if (await enviarWhatsapp(env, K, r, dias)) {
        await log(env, r.id, "whatsapp", "whatsapp", "enviado", `régua ${rotulo} (faltam ${dias} dias)`);
        enviados++;
        novo = "whatsapp_enviado";
      } else {
        transitorio = true;
        await log(env, r.id, "whatsapp", "whatsapp", "falhou", "erro no envio");
      }
    }

    if (transitorio) continue; // tenta de novo no próximo ciclo
    await run(env, "UPDATE renovacoes SET regua_ultima=? WHERE id=?", alvo, r.id);
    if (enviados && STATUS.indexOf(novo) > STATUS.indexOf(r.status)) {
      await avancar(env, K, r.id, novo, "regua");
    }
    if (enviados) n++;
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

export async function authenticate(env, request, url) {
  if (url.pathname.startsWith("/webhook/")) return { ok: true, user: null };

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
    const header = env.CERT_HEADER || "X-Client-Cert";
    const raw = request.headers.get(header) || "";
    const segredo = request.headers.get("X-Proxy-Secret") || "";
    if (raw && env.PROXY_SECRET && safeEqual(segredo, env.PROXY_SECRET)) {
      let info = null;
      try {
        info = await certInfoFromHeader(raw);
      } catch {
        info = null;
      }
      if (info) {
        const hj = todayISO(env);
        const u = await first(env, "SELECT * FROM usuarios WHERE fp=? AND ativo=1", info.fp);
        if (u && info.ini <= hj && hj <= info.fim) {
          return {
            ok: true,
            user: { id: u.id, nome: u.nome, perfil: u.perfil, validade: u.validade, fonte: "certificado" },
          };
        }
        return { ok: false, status: 403, body: { erro: "certificado não autorizado, inativo ou vencido" } };
      }
      return { ok: false, status: 403, body: { erro: "certificado inválido" } };
    }
  }

  const extra = senha ? { "WWW-Authenticate": 'Basic realm="Renovacoes", charset="UTF-8"' } : {};
  return { ok: false, status: 401, body: { erro: "autenticação necessária" }, extra };
}

async function rotaGet(env, K, url, user, admin) {
  const p = url.pathname;
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
      await all(env, "SELECT id,nome,email,perfil,sujeito,validade,ativo FROM usuarios ORDER BY nome"),
    );
  }
  if (p === "/api/config") {
    if (!admin) throw new HttpError(403, "apenas administradores");
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
  const { linhas, ignoradas } = lerLinhas(body, nome);
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
  return json({ novas, atualizadas, ignoradas: ignoradasTotal });
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

  if (p === "/api/importar") return await importar(env, url, body);

  if (p === "/api/usuarios") {
    if (!admin) throw new HttpError(403, "apenas administradores");
    return await cadastrarUsuario(env, url, body);
  }
  if (p.startsWith("/api/usuarios") || p.startsWith("/api/config")) {
    if (!admin) throw new HttpError(403, "apenas administradores");
  }

  let d;
  try {
    d = JSON.parse(new TextDecoder().decode(body) || "{}");
  } catch {
    throw new HttpError(400, "JSON inválido");
  }
  if (!d || typeof d !== "object" || Array.isArray(d)) throw new HttpError(400, "JSON inválido");

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
    if (!env.DB) return json({ erro: "binding D1 (DB) não configurado" }, 500);
    const auth = await authenticate(env, request, url);
    if (!auth.ok) return json(auth.body, auth.status, auth.extra);

    const user = auth.user;
    const admin = user?.perfil === "admin";
    const K = await loadConfig(env);
    const method = request.method.toUpperCase();
    if (method === "GET") return await rotaGet(env, K, url, user, admin);
    if (method === "POST") return await rotaPost(env, K, url, request, user, admin);
    return json({ erro: "método não permitido" }, 405, { Allow: "GET, POST" });
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
