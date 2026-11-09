// Leitura de planilhas (.xlsx/.xls/.csv) no runtime do Cloudflare Workers.
import * as XLSX from "xlsx";

export const ALIAS = {
  cliente: ["cliente", "nome", "razao social", "nome do cliente", "empresa"],
  email: ["email", "e-mail", "e mail", "correio eletronico"],
  telefone: ["telefone", "whatsapp", "fone", "tel", "celular"],
  tipo: ["tipo", "certificado", "produto", "tipo de certificado"],
  vencimento: ["vencimento", "validade", "data de vencimento", "data vencimento", "expira em"],
  valor: ["valor", "preco", "valor da renovacao"],
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
    colunas[chave] = disponiveis.find((c) => ALIAS[chave].includes(norm(c))) || null;
  }
  return colunas;
}

/**
 * Lê a planilha e devolve linhas já normalizadas. Linhas sem cliente ou sem
 * vencimento válido são contadas como ignoradas.
 */
export function lerLinhas(bytes, nome) {
  const livro = lerLivro(bytes, nome);
  const aba = livro.Sheets[livro.SheetNames[0]];
  if (!aba) throw new Error("planilha vazia");
  const bruto = XLSX.utils.sheet_to_json(aba, { defval: null, raw: true });
  if (!bruto.length) throw new Error("planilha vazia");

  const cols = mapearColunas(bruto[0]);
  const faltando = ["cliente", "vencimento"].filter((k) => !cols[k]);
  if (faltando.length) {
    throw new Error(
      `colunas obrigatórias ausentes: ${faltando.join(", ")} (encontradas: ${Object.keys(bruto[0]).join(", ")})`,
    );
  }

  const linhas = [];
  let ignoradas = 0;
  for (const row of bruto) {
    const cliente = texto(row[cols.cliente]);
    const vencimento = toISODate(row[cols.vencimento]);
    if (!cliente || !vencimento) {
      ignoradas++;
      continue;
    }
    linhas.push({
      cliente,
      email: cols.email ? texto(row[cols.email]) : null,
      telefone: cols.telefone ? telefone(row[cols.telefone]) : null,
      tipo: (cols.tipo ? texto(row[cols.tipo]) : null) || "e-CNPJ",
      vencimento,
      valor: cols.valor ? numero(row[cols.valor]) : null,
    });
  }
  return { linhas, ignoradas, total: bruto.length };
}
