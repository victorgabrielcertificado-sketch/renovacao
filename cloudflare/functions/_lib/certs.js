// Leitura de certificados digitais no runtime do Cloudflare Workers.
// Só a parte pública é usada; chave privada de .pfx/.p12 não é suportada aqui.
import "reflect-metadata";
import * as x509 from "@peculiar/x509";

x509.cryptoProvider.set(globalThis.crypto);

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const iso = (d) => (d instanceof Date && !Number.isNaN(d.getTime()) ? d.toISOString().slice(0, 10) : null);

/**
 * Extrai impressão digital SHA-256, sujeito e validade.
 * @param {string} nome nome do arquivo (para decidir o formato)
 * @param {string|Uint8Array} dados PEM (texto) ou DER (bytes)
 */
export async function certInfo(nome, dados) {
  const lower = String(nome || "").toLowerCase();
  if (lower.endsWith(".pfx") || lower.endsWith(".p12")) {
    throw new Error("PFX/P12 não é suportado na versão Cloudflare; envie .cer, .crt, .pem ou .der");
  }

  let cert;
  if (typeof dados === "string") {
    cert = new x509.X509Certificate(dados);
  } else {
    const texto = new TextDecoder("utf-8", { fatal: false }).decode(dados);
    cert = texto.includes("BEGIN CERTIFICATE")
      ? new x509.X509Certificate(texto)
      : new x509.X509Certificate(dados);
  }

  return {
    fp: hex(await cert.getThumbprint("SHA-256")),
    sujeito: String(cert.subjectName ?? cert.subject ?? ""),
    ini: iso(cert.notBefore),
    fim: iso(cert.notAfter),
  };
}
