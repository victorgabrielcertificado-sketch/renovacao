import "reflect-metadata";
import assert from "node:assert/strict";
import test from "node:test";
import * as XLSX from "xlsx";
import * as x509 from "@peculiar/x509";

import { lerLinhas, norm, numero, telefone, toISODate } from "../functions/_lib/import.js";
import { certInfo } from "../functions/_lib/certs.js";
import { authenticate } from "../functions/_lib/core.js";

const enc = (s) => new TextEncoder().encode(s);

test("normaliza cabeçalhos e números", () => {
  assert.equal(norm("  Razão   Social "), "razao social");
  assert.equal(numero("R$ 1.234,56"), 1234.56);
  assert.equal(numero("220"), 220);
  assert.equal(numero(""), null);
  assert.equal(telefone("79 99999-1111"), "79 99999-1111");
  assert.equal(telefone("123"), null);
});

test("converte datas ISO, dd/mm/aaaa e serial do Excel", () => {
  assert.equal(toISODate("2026-12-10"), "2026-12-10");
  assert.equal(toISODate("10/12/2026"), "2026-12-10");
  assert.equal(toISODate("10-12-26"), "2026-12-10");
  assert.equal(toISODate("31/02/2026"), null);
  assert.equal(toISODate(46334), "2026-11-08"); // serial do Excel
  assert.equal(toISODate(""), null);
});

test("lê CSV com ponto e vírgula, BOM e vírgula decimal", () => {
  const csv = "\uFEFFCliente;E-mail;WhatsApp;Tipo;Vencimento;Valor\n" +
    "ACME;acme@exemplo.com;79 99999-1111;e-CNPJ A1;10/12/2026;R$ 220,00\n" +
    "Sem data;x@y.com;;e-CPF A1;;100\n";
  const r = lerLinhas(enc(csv), "teste.csv");
  assert.equal(r.total, 2);
  assert.equal(r.linhas.length, 1);
  assert.equal(r.ignoradas, 1);
  assert.deepEqual(r.linhas[0], {
    cliente: "ACME",
    email: "acme@exemplo.com",
    telefone: "79 99999-1111",
    tipo: "e-CNPJ A1",
    vencimento: "2026-12-10",
    valor: 220,
  });
});

test("lê XLSX com data e cabeçalhos com acento", () => {
  const ws = XLSX.utils.aoa_to_sheet([
    ["Cliente", "Vencimento", "Valor"],
    ["Beta Ltda", new Date(2026, 11, 31), 310],
  ]);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Plan1");
  const buf = XLSX.write(wb, { type: "array", bookType: "xlsx" });
  const r = lerLinhas(new Uint8Array(buf), "teste.xlsx");
  assert.equal(r.linhas.length, 1);
  assert.equal(r.linhas[0].cliente, "Beta Ltda");
  assert.equal(r.linhas[0].vencimento, "2026-12-31");
  assert.equal(r.linhas[0].valor, 310);
});

test("rejeita planilha sem colunas obrigatórias", () => {
  assert.throws(() => lerLinhas(enc("Nome;Valor\nA;1\n"), "x.csv"), /colunas obrigatórias/);
});

test("lê certificado PEM e DER e rejeita PFX", async () => {
  const alg = { name: "ECDSA", namedCurve: "P-256", hash: "SHA-256" };
  const keys = await crypto.subtle.generateKey(alg, true, ["sign", "verify"]);
  const cert = await x509.X509CertificateGenerator.createSelfSigned({
    serialNumber: "01",
    name: "CN=Teste API",
    notBefore: new Date(Date.now() - 86400000),
    notAfter: new Date(Date.now() + 86400000 * 30),
    signingAlgorithm: alg,
    keys,
  });
  const pem = cert.toString("pem");
  const info = await certInfo("teste.pem", pem);
  assert.match(info.fp, /^[0-9a-f]{64}$/);
  assert.equal(info.ini, cert.notBefore.toISOString().slice(0, 10));
  assert.equal(info.fim, cert.notAfter.toISOString().slice(0, 10));
  assert.match(info.sujeito, /Teste API/);

  const der = await certInfo("teste.der", new Uint8Array(cert.rawData));
  assert.equal(der.fp, info.fp);

  await assert.rejects(() => certInfo("teste.pfx", pem), /PFX/);
});

test("bloqueia painel .pages.dev sem autenticação configurada", async () => {
  const url = new URL("https://renovacao.pages.dev/");
  const r = await authenticate({}, new Request(url), url);
  assert.equal(r.ok, false);
  assert.equal(r.status, 503);

  const liberado = await authenticate({ ALLOW_PUBLIC: "1" }, new Request(url), url);
  assert.equal(liberado.ok, true);
  assert.equal(liberado.user.perfil, "admin");
});

test("exige Basic Auth quando PAINEL_SENHA está definida", async () => {
  const env = { PAINEL_SENHA: "teste", PAINEL_USER: "admin" };
  const url = new URL("https://renovacao.pages.dev/");
  assert.equal((await authenticate(env, new Request(url), url)).status, 401);

  const ok = new Request(url, { headers: { Authorization: "Basic " + btoa("admin:teste") } });
  const r = await authenticate(env, ok, url);
  assert.equal(r.ok, true);
  assert.equal(r.user.perfil, "admin");
});
