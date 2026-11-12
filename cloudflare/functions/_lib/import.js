// Leitura de planilhas (.xlsx/.xls/.csv) no runtime do Cloudflare Workers.
import * as XLSX from "xlsx";

export const ALIAS = {
  cliente: ["cliente", "nome", "razao social", "nome do cliente", "empresa"],
  email: ["email", "e-mail", "e mail", "correio eletronico"],
  telefone: ["telefone", "whatsapp", "fone", "tel", "celular"],
  tipo: ["tipo", "certificado", "produto", "tipo de certificado"],
  vencimento: ["vencimento", "data de vencimento", "data vencimento", "expira em", "validade"],
  valor: ["valor", "preco", "valor da renovacao"],
  emissao: [
    "emissao",
    "data de emissao",
    "data emissao",
    "emitido em",
    "inicio",
    "data de inicio",
    "inicio da validade",
  ],
  duracao: [
    "duracao",
    "periodo",
    "tempo",
    "duracao em meses",
    "duracao em anos",
    "validade em meses",
    "validade em anos",
    "validade",
  ],
};

// Durações permitidas, em meses, por tipo de certificado.
export const DURACOES = {
  a1: [12],
  a3: [12, 24, 36],
  safeid: [3, 12],
};

export const norm = (s) =>
  String(s ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

export function texto(v) {
  if (v == null) return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : toISODate(v);
  const s = String(v).trim();
  return s && !["nan", "none", "nat", "<na>"].includes(s.toLowerCase()) ? s : null;
}

export function telefone(v) {
  const s = texto(v);
  return s && s.replace(/\D/g, "").length >= 10 ? s : null;
}

export function numero(v) {
  if (v == null || v === "") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  let s = String(v).trim().replace(/[R$\s]/g, "");
  if (s.includes(",")) s = s.replace(/\./g, "").replace(",", ".");
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

const pad = (n) => String(n).padStart(2, "0");
const valida = (y, m, d) => {
  if (!y || m < 1 || m > 12 || d < 1 || d > 31) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d
    ? `${y}-${pad(m)}-${pad(d)}`
    : null;
};

export function toISODate(v) {
  if (v == null || v === "") return null;
  if (v instanceof Date) {
    // o SheetJS grava datas de célula em torno da meia-noite UTC
    return Number.isNaN(v.getTime())
      ? null
      : `${v.getUTCFullYear()}-${pad(v.getUTCMonth() + 1)}-${pad(v.getUTCDate())}`;
  }
  if (typeof v === "number" && Number.isFinite(v)) {
    if (v < 1) return null;
    // serial do Excel: 1899-12-30 + N dias (o dia fictício 29/02/1900 só afeta seriais < 60)
    const d = new Date(Date.UTC(1899, 11, 30) + Math.floor(v) * 86400000);
    return valida(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
  }
  const s = String(v).trim();
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return valida(+m[1], +m[2], +m[3]);
  m = s.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})/);
  if (m) {
    let y = +m[3];
    if (y < 100) y += y < 50 ? 2000 : 1900;
    return valida(y, +m[2], +m[1]);
  }
  return null;
}

export function tipoRegra(tipo) {
  const t = norm(tipo);
  if (/\bsafe\s*id\b|\bsafeid\b/.test(t)) return "safeid";
  if (/\ba1\b/.test(t)) return "a1";
  if (/\ba3\b/.test(t)) return "a3";
  return null;
}

function mesesDeTexto(valor) {
  const t = norm(valor);
  let m = t.match(/(\d+)\s*(mes|meses)\b/);
  if (m) return Number(m[1]);
  m = t.match(/(\d+)\s*(ano|anos)\b/);
  if (m) return Number(m[1]) * 12;
  return null;
}

export function duracaoMeses(valor, tipo) {
  const regra = tipoRegra(tipo);
  if (valor != null && valor !== "") {
    if (typeof valor === "number" && Number.isFinite(valor)) {
      const n = Math.trunc(valor);
      if (regra === "a3" && [1, 2, 3].includes(n)) return n * 12;
      if (regra === "a1" && n === 1) return 12;
      if (regra === "safeid" && n === 1) return 12;
      return n > 0 ? n : null;
    }
    const porTexto = mesesDeTexto(valor);
    if (porTexto != null) return porTexto;
    const s = norm(valor);
    const m = s.match(/^(\d+)$/);
    if (m) {
      const n = Number(m[1]);
      if (regra === "a3" && [1, 2, 3].includes(n)) return n * 12;
      if (regra === "a1" && n === 1) return 12;
      if (regra === "safeid" && n === 1) return 12;
      return n > 0 ? n : null;
    }
  }
  return mesesDeTexto(tipo);
}

export function duracaoValida(tipo, meses) {
  const regra = tipoRegra(tipo);
  if (!regra || !meses) return true;
  return DURACOES[regra].includes(meses);
}

export function addMeses(iso, meses) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(iso)) || !Number.isInteger(meses) || meses <= 0) {
    return null;
  }
  const [y, m, d] = String(iso).split("-").map(Number);
  const alvo = new Date(Date.UTC(y, m - 1 + meses, 1));
  const ultimo = new Date(Date.UTC(alvo.getUTCFullYear(), alvo.getUTCMonth() + 1, 0)).getUTCDate();
  alvo.setUTCDate(Math.min(d, ultimo));
  return `${alvo.getUTCFullYear()}-${pad(alvo.getUTCMonth() + 1)}-${pad(alvo.getUTCDate())}`;
}

function lerLivro(bytes, nome) {
  const lower = String(nome || "").toLowerCase();
  if (lower.endsWith(".csv")) {
    let textoCsv;
    try {
      textoCsv = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      textoCsv = new TextDecoder("windows-1252").decode(bytes);
    }
    textoCsv = textoCsv.replace(/^\uFEFF/, "");
    const primeira = textoCsv.split(/\r?\n/)[0] || "";
    const contagem = {
      ",": (primeira.match(/,/g) || []).length,
      ";": (primeira.match(/;/g) || []).length,
      "\t": (primeira.match(/\t/g) || []).length,
    };
    const fs = Object.entries(contagem).sort((a, b) => b[1] - a[1])[0][0];
    return XLSX.read(textoCsv, { type: "string", FS: fs, raw: true });
  }
  if ([".xlsx", ".xls", ".xlsm"].some((ext) => lower.endsWith(ext))) {
    return XLSX.read(bytes, { type: "array", cellDates: true, raw: true });
  }
  throw new Error("formato não suportado: use .csv, .xls ou .xlsx");
}

function mapearColunas(linha) {
  const colunas = {};
  const disponiveis = Object.keys(linha || {});
  for (const chave of Object.keys(ALIAS)) {
    colunas[chave] = null;
    for (const alias of ALIAS[chave]) {
      const encontrada = disponiveis.find((c) => norm(c) === alias);
      if (encontrada) {
        colunas[chave] = encontrada;
        break;
      }
    }
  }
  return colunas;
}

/**
 * Lê a planilha e devolve linhas já normalizadas. Linhas sem cliente ou sem
 * vencimento válido são contadas como ignoradas. Quando a planilha traz a data
 * de emissão e a duração (ou a duração no próprio tipo), o vencimento é
 * calculado e conferido com as regras de validade de cada tipo.
 */
export function lerLinhas(bytes, nome) {
  const livro = lerLivro(bytes, nome);
  const aba = livro.Sheets[livro.SheetNames[0]];
  if (!aba) throw new Error("planilha vazia");
  const bruto = XLSX.utils.sheet_to_json(aba, { defval: null, raw: true });
  if (!bruto.length) throw new Error("planilha vazia");

  const cols = mapearColunas(bruto[0]);
  const faltando = [];
  if (!cols.cliente) faltando.push("cliente");
  if (!cols.vencimento && !cols.emissao) faltando.push("vencimento ou emissao");
  if (faltando.length) {
    throw new Error(
      `colunas obrigatórias ausentes: ${faltando.join(", ")} (encontradas: ${Object.keys(bruto[0]).join(", ")})`,
    );
  }

  const linhas = [];
  const detalhes = [];
  let ignoradas = 0;
  let avisos = 0;
  const avisar = (msg) => {
    avisos++;
    if (detalhes.length < 10) detalhes.push(msg);
  };

  for (const row of bruto) {
    const cliente = texto(row[cols.cliente]);
    const tipo = (cols.tipo ? texto(row[cols.tipo]) : null) || "e-CNPJ";
    const regra = tipoRegra(tipo);
    let vencimento = cols.vencimento ? toISODate(row[cols.vencimento]) : null;
    const emissao = cols.emissao ? toISODate(row[cols.emissao]) : null;
    let duracao = cols.duracao ? duracaoMeses(row[cols.duracao], tipo) : null;

    // coluna "Validade" pode vir como duração ("1 ano", "3 meses")
    if (vencimento == null && cols.vencimento) {
      const alt = duracaoMeses(row[cols.vencimento], tipo);
      if (alt != null) duracao = duracao ?? alt;
    }

    if (vencimento == null) {
      if (emissao && duracao != null) {
        vencimento = addMeses(emissao, duracao);
      } else if (emissao && regra && DURACOES[regra].length === 1) {
        duracao = DURACOES[regra][0];
        vencimento = addMeses(emissao, duracao);
      }
    }

    if (!cliente || !vencimento) {
      ignoradas++;
      continue;
    }

    if (duracao != null && !duracaoValida(tipo, duracao)) {
      avisar(
        `${cliente}: ${tipo} com ${duracao} meses; permitido: ${DURACOES[regra]?.join(", ") || "—"} meses`,
      );
    }

    if (emissao) {
      if (duracao != null) {
        const esperado = addMeses(emissao, duracao);
        if (esperado && esperado !== vencimento) {
          avisar(`${cliente}: vencimento ${vencimento} não confere com ${emissao} + ${duracao} meses (${esperado})`);
        }
      } else if (regra) {
        const permitidas = DURACOES[regra];
        if (!permitidas.some((m) => addMeses(emissao, m) === vencimento)) {
          avisar(
            `${cliente}: vencimento ${vencimento} não confere com ${emissao} + ${permitidas.join("/")} meses`,
          );
        }
      }
    }

    linhas.push({
      cliente,
      email: cols.email ? texto(row[cols.email]) : null,
      telefone: cols.telefone ? telefone(row[cols.telefone]) : null,
      tipo,
      vencimento,
      valor: cols.valor ? numero(row[cols.valor]) : null,
    });
  }
  return { linhas, ignoradas, total: bruto.length, avisos, detalhes };
}
