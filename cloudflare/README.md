# Versão Cloudflare Pages (`renovacao.pages.dev`)

Esta pasta contém a versão do painel que roda em **Cloudflare Pages + Pages Functions + D1**,
publicada em um domínio `*.pages.dev`. O `renovacao.py` (Python/SQLite) continua sendo a versão
para rodar em servidor/VPS; esta é a versão serverless.

## O que muda em relação à versão Python

| Python (VPS) | Cloudflare Pages |
|---|---|
| SQLite em arquivo | **D1** (SQLite gerenciado) |
| `pandas` + `openpyxl` | **SheetJS** (`xlsx`) |
| SMTP | **Resend** (HTTP; Workers não abrem SMTP) |
| Thread da régua a cada 6h | **cron externo** chamando `/api/regua` |
| Login básico + certificado via nginx | Basic + **Cloudflare Access** + certificado via cabeçalho |
| Upload de `.pfx`/`.p12` | somente `.cer`/`.crt`/`.pem`/`.der` |

Tudo o mais é igual: régua D60/D30/D15/D7/D1/D-3, métricas, histórico, importação de planilha,
Kommo (leads, etapas e notas) e perfis `admin`/`operador`.

## Estrutura

```
cloudflare/
  functions/
    _middleware.js          protege o painel estático (Basic/Access/certificado)
    _lib/core.js            regras: régua, métricas, Kommo, auth, rotas
    _lib/import.js          leitura de .xlsx/.xls/.csv
    _lib/certs.js           leitura de certificado (PEM/DER)
    api/[[path]].js         /api/*
    webhook/kommo.js        /webhook/kommo
  public/
    index.html              painel (HTML/CSS/JS)
    _headers                CSP e cabeçalhos de segurança
  schema.sql                tabelas do D1
  test/                     testes unitários e de integração
  wrangler.toml             binding D1 e compatibilidade
```

## Rodar localmente

```bash
cd cloudflare
npm install
npm run db:local      # cria as tabelas no D1 local
npm run db:seed       # dados de exemplo para os testes
cp .dev.vars.example .dev.vars   # ajuste as senhas
npm run dev           # http://127.0.0.1:8788
```

Em outro terminal:

```bash
npm test              # 8 testes unitários (planilha, datas, certificado, auth)
npm run test:api      # 16 verificações de integração (servidor precisa estar rodando)
```

> No Windows, use `npm.cmd`/`npx.cmd` se o PowerShell bloquear os scripts `.ps1`.

## Publicar em `renovacao.pages.dev`

Pré-requisitos: conta Cloudflare e `npx wrangler login`.

### 1. Criar o banco D1

```bash
npx wrangler d1 create renovacao
```

Copie o `database_id` retornado para `wrangler.toml` (substitua o `00000000-...`).

```bash
npx wrangler d1 execute renovacao --remote --file=schema.sql
```

### 2. Criar o projeto Pages

```bash
npx wrangler pages project create renovacao --production-branch main
```

### 3. Configurar o binding D1 no projeto

O `wrangler.toml` já declara o binding `DB`. Se o deploy não o reconhecer, configure no painel:
**Pages → renovacao → Settings → Functions → D1 database bindings → Add** com o nome `DB` e o
banco `renovacao`.

### 4. Definir as variáveis/segredos

Pelo painel (**Settings → Environment variables**) ou pela CLI:

```bash
npx wrangler pages secret put PAINEL_SENHA --project-name renovacao
npx wrangler pages secret put WEBHOOK_TOKEN --project-name renovacao
npx wrangler pages secret put CRON_TOKEN --project-name renovacao
# opcional, para enviar e-mail de verdade:
npx wrangler pages secret put RESEND_API_KEY --project-name renovacao
npx wrangler pages secret put EMAIL_FROM --project-name renovacao
```

| Variável | Para que serve |
|---|---|
| `PAINEL_SENHA` | senha básica de emergência (admin). **Defina antes do primeiro deploy.** |
| `PAINEL_USER` | usuário do Basic Auth (padrão `admin`) |
| `WEBHOOK_TOKEN` | token exigido em `/webhook/kommo?t=...` |
| `CRON_TOKEN` | token para `POST /api/regua?t=...` (régua automática) |
| `RESEND_API_KEY` | envio real de e-mail via Resend |
| `EMAIL_FROM` | remetente verificado no Resend |
| `LINK_AGENDA` | link no corpo do e-mail |
| `TRUST_ACCESS` | `1` para confiar no e-mail do Cloudflare Access |
| `AUTH_CERT` | `1` para aceitar certificado no cabeçalho |
| `CERT_HEADER` | nome do cabeçalho do certificado (padrão `X-Client-Cert`) |
| `PROXY_SECRET` | segredo que o proxy/mTLS envia em `X-Proxy-Secret` |
| `TZ` | fuso da régua (padrão `America/Sao_Paulo`) |
| `ALLOW_PUBLIC` | `1` para permitir painel sem login (só demonstração) |
| `KOMMO_URL`, `KOMMO_TOKEN`, `KOMMO_PIPELINE`, `KOMMO_ETAPAS` | integração Kommo (também configurável pelo painel) |

### 5. Deploy

```bash
npx wrangler pages deploy public --project-name renovacao
```

A URL será `https://renovacao.pages.dev`.

> **Primeiro acesso:** sem `PAINEL_SENHA`/Access, o app responde **503** em `*.pages.dev` de
> propósito. Defina `PAINEL_SENHA` (ou configure o Access) e faça o deploy novamente.

## Login com Cloudflare Access (recomendado)

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. Domínio: `renovacao.pages.dev` (e `*.renovacao.pages.dev` para previews).
3. Política: **Allow** para os e-mails/domínios da equipe (OTP por e-mail, Google, GitHub etc.).
4. Defina `TRUST_ACCESS=1` nas variáveis e faça o deploy.
5. No painel, aba **Usuários**, cadastre **nome + e-mail** (sem certificado) para cada pessoa.
   O Access envia `Cf-Access-Authenticated-User-Email`; o app procura esse e-mail na tabela
   `usuarios` e usa o perfil (`admin`/`operador`).

`PAINEL_SENHA` pode continuar definida como acesso de emergência do administrador.

## Login por certificado digital

Workers não recebem o certificado TLS diretamente. O certificado precisa ser terminado por um
proxy/mTLS que repasse o PEM:

```nginx
# exemplo: nginx na frente do app (ou um Worker/proxy equivalente)
proxy_set_header X-Client-Cert $ssl_client_escaped_cert;
proxy_set_header X-Proxy-Secret "troque-este-segredo";
```

No app:

```
AUTH_CERT=1
PROXY_SECRET=troque-este-segredo
# CERT_HEADER=X-Client-Cert   (padrão)
```

O app calcula a impressão digital SHA-256, confere se o usuário está ativo e se o certificado
está dentro da validade. O cabeçalho só é aceito se `X-Proxy-Secret` conferir com `PROXY_SECRET`.
**Nunca** exponha o app de forma que o cliente consiga mandar esse cabeçalho direto.

`.pfx`/`.p12` não são suportados nesta versão (o parser PKCS#12 não roda no Workers). Converta:

```bash
openssl pkcs12 -in certificado.pfx -clcerts -nokeys -out certificado.pem
```

## Régua automática (cron)

Pages Functions não têm Cron Trigger. Crie um Worker com cron ou use GitHub Actions/cron-job.org
chamando:

```
POST https://renovacao.pages.dev/api/regua?t=SEU_CRON_TOKEN
Header: X-Requested-With: fetch
```

Exemplo de Worker com cron:

```js
export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(fetch(`https://renovacao.pages.dev/api/regua?t=${env.CRON_TOKEN}`, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
    }));
  },
};
```

A régua é idempotente: cada limiar (D60, D30, D15, D7, D1, D-3) dispara uma vez por renovação.

## E-mail

Workers não abrem conexão SMTP. Esta versão usa a API HTTP do **Resend**:

```
RESEND_API_KEY=re_...
EMAIL_FROM=renovacao@seudominio.com.br
```

Sem `RESEND_API_KEY`, o envio é **simulado** (o evento é gravado como enviado, mas nada sai).
Valide o domínio no Resend para o remetente funcionar.

## Segurança

- Em `*.pages.dev`, sem autenticação configurada o app responde **503** (a menos que
  `ALLOW_PUBLIC=1`).
- O painel estático é protegido pelo `functions/_middleware.js`; as rotas `/api/*` e
  `/webhook/*` autenticam por conta própria.
- POSTs do painel exigem `X-Requested-With: fetch` (mitiga CSRF).
- O token da Kommo salvo pelo painel fica em texto puro no D1; prefira variável de ambiente.
- `TRUST_ACCESS=1` só deve ser usado **com o Access na frente** do domínio.
- `AUTH_CERT=1` só deve ser usado com um proxy/mTLS confiável e `PROXY_SECRET`.

## Limites do plano gratuito

- D1: 5 GB e 5 milhões de leituras/dia (suficiente para o protótipo).
- Pages: 500 builds/mês e 100 GB de banda.
- Resend: 3.000 e-mails/mês (plano gratuito).
- Workers: CPU por requisição; a importação de planilhas grandes pode encostar no limite.

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `503 Painel sem autenticação` | defina `PAINEL_SENHA` ou configure o Access |
| `401` no navegador | Basic Auth não enviado; faça login ou use Access |
| `403 e-mail do Access não cadastrado` | cadastre o e-mail na aba Usuários ou ajuste a política |
| `403 certificado inválido` | cabeçalho errado, `PROXY_SECRET` diferente ou certificado não cadastrado |
| `binding D1 (DB) não configurado` | binding `DB` ausente no projeto Pages |
| e-mail não sai | `RESEND_API_KEY`/`EMAIL_FROM` ausentes ou domínio não verificado |
| webhook `403` | `WEBHOOK_TOKEN` diferente do `?t=` |
