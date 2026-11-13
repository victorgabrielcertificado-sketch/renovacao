# Versão Cloudflare Pages (`renovacao-61n.pages.dev`)

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
| Login básico + certificado via nginx | formulário com cookie + Basic + **Cloudflare Access** + certificado via cabeçalho |
| Upload de `.pfx`/`.p12` | somente `.cer`/`.crt`/`.pem`/`.der` |

Tudo o mais é igual: e-mail de vencimento a partir de D30 e repetido a cada 7 dias até a renovação ser
concluída, WhatsApp nos limiares D60/D30/D15/D7/D1/D-3, métricas, histórico, importação de planilha,
Kommo (leads, etapas e notas) e perfis `admin`/`operador`.

## Estrutura

```
cloudflare/
  functions/
    _middleware.js          protege o painel (formulário/Basic/Access/certificado); /login é público
    _lib/core.js            regras: régua, métricas, Kommo, auth, rotas
    _lib/import.js          leitura de .xlsx/.xls/.csv
    _lib/certs.js           leitura de certificado (PEM/DER)
    api/[[path]].js         /api/*
    webhook/kommo.js        /webhook/kommo
  public/
    index.html              painel (HTML/CSS/JS)
    login.html              formulário de login (cookie de sessão)
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
npm test              # 12 testes unitários (planilha, datas, validade, certificado, auth)
npm run test:api      # 23 verificações de integração (servidor precisa estar rodando)
```

> No Windows, use `npm.cmd`/`npx.cmd` se o PowerShell bloquear os scripts `.ps1`.

## Regras de validade dos certificados

| Tipo | Duração permitida |
|---|---|
| A1 | 1 ano (12 meses) |
| A3 | 1, 2 ou 3 anos (12, 24 ou 36 meses) |
| SafeID | 3 meses ou 1 ano (3 ou 12 meses) |

A planilha pode trazer a data de **Vencimento** ou, no lugar dela, **Emissão** + **Duração**.
Quando só há emissão e duração, o sistema calcula o vencimento. Quando as duas datas vêm
preenchidas, ele confere a regra e mostra um aviso (sem bloquear a importação) se não bater.
A duração aceita textos como `3 meses`, `1 ano` e `2 anos`; em colunas numéricas, A3 `1/2/3`
é interpretado como anos e SafeID `1` = 1 ano, `3` = 3 meses.

## Publicar no Cloudflare Pages

Pré-requisitos: conta Cloudflare e `npx wrangler login`.

### 1. Criar o banco D1

```bash
npx wrangler d1 create renovacao
```

Copie o `database_id` retornado para `wrangler.toml` (substitua o `00000000-...`).

```bash
npx wrangler d1 execute renovacao --remote --file=schema.sql
# Se o banco já existia antes da versão com senha individual:
npx wrangler d1 execute renovacao --remote --file=migrations/2026-11-11-senha-usuarios.sql
# Se o banco já existia antes do aviso de e-mail em D30:
npx wrangler d1 execute renovacao --remote --file=migrations/2026-11-12-ultimo-email.sql
```

> Se `--file` falhar com `Authentication error [code: 10000]`, aplique a migração com `--command`
> (ex.: `npx wrangler d1 execute renovacao --remote --command "ALTER TABLE renovacoes ADD COLUMN ultimo_email TEXT;"`).

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
| `PAINEL_SENHA` | senha de emergência do admin e do Basic Auth (não é mais o único login). **Defina antes do primeiro deploy.** |
| `PAINEL_USER` | usuário do formulário/Basic Auth (padrão `admin`) |
| `SESSION_SECRET` | chave HMAC do cookie de sessão (opcional; padrão: `PAINEL_SENHA`) |
| `WEBHOOK_TOKEN` | token exigido em `/webhook/kommo?t=...` |
| `CRON_TOKEN` | token para `POST /api/regua?t=...` (régua automática) |
| `RESEND_API_KEY` | envio real de e-mail via Resend |
| `EMAIL_FROM` | remetente verificado no Resend |
| `LINK_AGENDA` | link no corpo do e-mail |
| `TRUST_ACCESS` | `1` para confiar no e-mail do Cloudflare Access |
| `AUTH_CERT` | `1` para aceitar certificado no cabeçalho |
| `CERT_HEADER` | nome do cabeçalho do certificado (padrão `X-Client-Cert`) |
| `PROXY_SECRET` | segredo que o proxy/mTLS envia em `X-Proxy-Secret` |
| `CERT_LOGIN_URL` | URL do endpoint mTLS que abre o seletor de certificados (se vazio, usa `/cert-login` no mesmo domínio) |
| `TZ` | fuso da régua (padrão `America/Sao_Paulo`) |
| `ALLOW_PUBLIC` | `1` para permitir painel sem login (só demonstração) |
| `KOMMO_URL`, `KOMMO_TOKEN`, `KOMMO_PIPELINE`, `KOMMO_ETAPAS` | integração Kommo (também configurável pelo painel) |

### 5. Deploy

```bash
npx wrangler pages deploy public --project-name renovacao
```

A URL será `https://renovacao-61n.pages.dev` neste projeto (o nome `renovacao.pages.dev` já estava
em uso; a Cloudflare atribui um sufixo quando isso acontece).

> **Primeiro acesso:** sem `PAINEL_SENHA`, o app responde **503** em `*.pages.dev` de propósito.
> Defina `PAINEL_SENHA` e faça o deploy novamente.

## Login pelo formulário e usuários

O painel exige sessão. O formulário troca usuário/senha por um **cookie de sessão HMAC**
(`HttpOnly`, `SameSite=Lax`, 12 horas), e o botão **Sair** remove o cookie.

- **Admin de emergência:** `PAINEL_USER` + `PAINEL_SENHA` (também aceito no Basic Auth).
- **Usuários individuais:** cadastrados na aba **Usuários** com nome, e-mail, perfil e senha.
  O login aceita o **nome** ou o **e-mail** cadastrado.
- **Perfis:** `admin` vê todas as abas; `operador` vê **Renovações** e **Configurações**
  (importação de planilha e integração Kommo), mas a aba **Usuários** é exclusiva de `admin`.
- **Tema:** em **Configurações → Aparência** o usuário escolhe **Automático**, **Claro** ou
  **Escuro**; a preferência fica salva no navegador (`localStorage`).
- As senhas são gravadas como hash **PBKDF2-SHA256 com salt**; nunca em texto puro.
- O admin pode redefinir a senha de qualquer usuário pelo botão **Senha** na lista.
- O cookie é assinado com `SESSION_SECRET` (ou, se ausente, com `PAINEL_SENHA`).
- Trocar a senha (ou o `SESSION_SECRET`) invalida todas as sessões abertas.
- O Basic Auth continua aceito para scripts/CLI.
- O login tem limite de **10 falhas por IP a cada 15 minutos** (tabela `login_tentativas`,
  resposta `429` + `Retry-After`). No `*.pages.dev` não há rate limiting de borda.

## Segurança

| Item | Como está implementado |
|---|---|
| **TLS 1.2/1.3** | Terminação TLS automática da Cloudflare Pages; o app só responde atrás de HTTPS em produção. |
| **HSTS** | `Strict-Transport-Security: max-age=31536000; includeSubDomains` em `_headers` e em todas as respostas das Functions. |
| **CSP** | `default-src 'self'`, fontes auto-hospedadas, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`. Ainda usa `'unsafe-inline'` para os scripts/estilos inline do painel (ver limitações abaixo). |
| **Clickjacking** | `X-Frame-Options: DENY` + `frame-ancestors 'none'`. |
| **MIME sniffing** | `X-Content-Type-Options: nosniff`. |
| **Referrer** | `Referrer-Policy: no-referrer`. |
| **CORS** | API **same-origin**: nenhum `Access-Control-Allow-Origin` é emitido; `OPTIONS` responde 204 sem liberar origem. |
| **CSRF** | Header `X-Requested-With: fetch` em todo POST + token `X-CSRF-Token` vinculado ao JWT da sessão; cookie `SameSite=Lax`. |
| **Cookies** | `HttpOnly`, `Secure` em HTTPS, `SameSite=Lax`, `Path=/`, 12 h; em HTTPS usa o prefixo `__Host-`. |
| **Rate limiting** | Login: 10 falhas por IP a cada 15 min (tabela `login_tentativas`, resposta 429 + `Retry-After`). Para as demais rotas, use as regras de Rate Limiting/WAF da Cloudflare. |
| **WAF / DDoS** | Camada de borda da Cloudflare. Regras de WAF, Rate Limiting e Access exigem um **domínio próprio** apontado para o projeto Pages (o `*.pages.dev` não pertence à sua zona). |
| **OAuth 2.0 / OIDC** | **Cloudflare Access** (Google, GitHub, OTP por e-mail etc.) exige domínio próprio. O app ainda **não valida o JWT do Access**; não ative `TRUST_ACCESS=1` sem essa validação. |
| **Certificado digital** | mTLS terminado no proxy; o app valida fingerprint SHA-256, usuário ativo e validade. `/cert-login` cria a sessão após o handshake. |
| **Sessão/JWT** | JWT compacto HS256 com `sub`, `id`, `role`, `iat`, `exp` (12 h) e `csrf`, assinado com `SESSION_SECRET` + `PAINEL_SENHA`. |
| **Senhas de usuários** | PBKDF2-SHA256 com salt por usuário (25.000 iterações), armazenado como `pbkdf2$iter$salt$hash`; nunca em texto puro. |
| **SQL injection** | Todas as consultas D1 usam prepared statements com bind de parâmetros. |
| **XSS** | CSP + escape (`esc()`) de todo dado vindo do banco/planilha antes de ir para o HTML. |

### Limitações e próximos passos de segurança

- **CSP com `'unsafe-inline'`:** o painel tem um bloco `<script>` e handlers `onclick` inline. Para remover
  essa permissão, é necessário extrair o JS para um arquivo e trocar os `onclick` por `addEventListener`.
- **WAF/Access em `*.pages.dev`:** anexe um domínio próprio (Pages → Custom domains) para habilitar
  WAF gerenciado, Rate Limiting por rota e Cloudflare Access com política por e-mail.
- **HSTS preload:** não se aplica a `*.pages.dev` (sufixo público). Em domínio próprio, envie o domínio
  para a lista de preload depois de confirmar que todos os subdomínios são HTTPS.
- **Rotação de segredos:** trocar `PAINEL_SENHA` ou `SESSION_SECRET` invalida todas as sessões.
  Gere um `SESSION_SECRET` forte com:
  ```bash
  npx wrangler pages secret put SESSION_SECRET --project-name renovacao
  ```
- **Login por Basic Auth:** mantido para scripts/CLI; o uso normal é o formulário com cookie.

## Caminho gratuito com `pages.dev`

Sem domínio próprio, **WAF, Cloudflare Access e rate limiting de borda não ficam disponíveis**: o
`*.pages.dev` não é uma zona da sua conta. A segurança fica toda no app, que já entrega TLS 1.2/1.3,
HSTS, CSP, `X-Frame-Options`, `nosniff`, cookies `__Host-`, CSRF e limite de tentativas de login.

Automatismos gratuitos preparados em `.github/workflows/`:

| Workflow | O que faz | Secrets necessários |
|---|---|---|
| `.github/workflows/regua.yml` | chama `POST /api/regua` a cada 6 h | `CRON_TOKEN` |
| `.github/workflows/backup-d1.yml` | exporta o D1 todo dia às 03:00 UTC | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |

> Se o push for feito por um token OAuth sem o escopo `workflow`, o GitHub recusa o envio
> desses arquivos. Nesse caso, adicione-os pelo site do GitHub ou use um PAT com `repo` + `workflow`.

Cadastre em **GitHub → Settings → Secrets and variables → Actions → New repository secret**.
Para o backup, crie um API Token na Cloudflare em **My Profile → API Tokens → Create Token** com
**Account → D1 → Edit**.

**E-mail real:** sem domínio verificado, o Resend só permite testes com `onboarding@resend.dev`
para o e-mail da própria conta. Para enviar aos clientes, verifique um domínio no Resend (pode ser
um domínio só para e-mail) e copie os registros DNS para o provedor de DNS desse domínio.
Sem `RESEND_API_KEY`, o app continua em modo simulação.

**Mantenha** `TRUST_ACCESS=0` e `AUTH_CERT=0` nesse caminho.

## Login com Cloudflare Access (requer domínio próprio)

> **Não disponível em `*.pages.dev`:** o Access precisa de um hostname dentro de uma zona sua.
> Além disso, o app hoje confia no header `Cf-Access-Authenticated-User-Email` **sem validar o
> JWT do Access**. Mantenha `TRUST_ACCESS=0` até implementar a validação de
> `Cf-Access-Jwt-Assertion` com `ACCESS_TEAM_DOMAIN`/`ACCESS_AUD`.

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. Domínio: `painel.seudominio.com` (e `*.painel.seudominio.com` para previews).
3. Política: **Allow** para os e-mails/domínios da equipe (OTP por e-mail, Google, GitHub etc.).
4. Depois de implementar a validação do JWT, defina `TRUST_ACCESS=1` e faça o deploy.
5. No painel, aba **Usuários**, cadastre **nome + e-mail** (sem certificado) para cada pessoa.
   O Access envia `Cf-Access-Authenticated-User-Email`; o app procura esse e-mail na tabela
   `usuarios` e usa o perfil (`admin`/`operador`).

`PAINEL_SENHA` pode continuar definida como acesso de emergência do administrador.

## Login por certificado digital (opcional, sem botão)

O login por certificado continua disponível no backend, mas **não há botão na tela de login**.
Para usar, o proxy mTLS deve levar o usuário diretamente a `CERT_LOGIN_URL` (ou `/cert-login`).

O navegador não expõe o repositório de certificados para JavaScript — os navegadores bloqueiam
esse acesso por segurança. Quem abre o seletor é o handshake TLS: o proxy solicita o certificado
e o navegador mostra os certificados do sistema (Windows: repositório **Pessoal**; macOS:
**Keychain**). Chrome/Edge usam o repositório do Windows e Safari/Chrome no macOS usam o
Keychain; o Firefox pode usar o próprio repositório.

Fluxo:

1. O proxy mTLS exige o certificado e repassa o PEM em `X-Client-Cert` + `X-Proxy-Secret`.
2. `GET /cert-login` valida a impressão digital SHA-256 na tabela `usuarios`, cria o cookie de
   sessão e redireciona para o painel.

Sem proxy mTLS, `/cert-login` não tem como receber o certificado. **No `*.pages.dev` não há
mTLS de cliente**, então é necessário um domínio próprio com um proxy na frente (nginx,
Cloudflare Tunnel + nginx, Caddy etc.).

Exemplo de nginx:

```nginx
server {
  listen 443 ssl;
  server_name painel.seudominio.com;

  ssl_certificate     /etc/letsencrypt/live/painel.seudominio.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/painel.seudominio.com/privkey.pem;

  # CA(s) que emitiram os certificados dos usuários (cadeia ICP-Brasil ou CA interna).
  ssl_client_certificate /etc/nginx/ca-clientes.pem;
  ssl_verify_client optional;

  # Só esta rota exige o certificado; o resto do site continua acessível para o formulário.
  location = /cert-login {
    if ($ssl_client_verify != SUCCESS) { return 403; }
    proxy_set_header X-Client-Cert $ssl_client_escaped_cert;
    proxy_set_header X-Proxy-Secret "troque-este-segredo";
    proxy_set_header Host renovacao-61n.pages.dev;
    proxy_ssl_server_name on;
    proxy_ssl_name renovacao-61n.pages.dev;
    proxy_pass https://renovacao-61n.pages.dev;
  }

  location / {
    proxy_set_header Host renovacao-61n.pages.dev;
    proxy_ssl_server_name on;
    proxy_ssl_name renovacao-61n.pages.dev;
    proxy_pass https://renovacao-61n.pages.dev;
  }
}
```

No app (produção):

```
AUTH_CERT=1
PROXY_SECRET=troque-este-segredo
CERT_LOGIN_URL=https://painel.seudominio.com/cert-login
# CERT_HEADER=X-Client-Cert   (padrão)
```

O app calcula a impressão digital SHA-256, confere se o usuário está ativo e se o certificado
está dentro da validade. O cabeçalho só é aceito se `X-Proxy-Secret` conferir com `PROXY_SECRET`.
**Nunca** exponha o app de forma que o cliente consiga mandar esse cabeçalho direto.

Cadastre o certificado em **Usuários → Certificado** (PEM/DER). `.pfx`/`.p12` não são suportados
nesta versão (o parser PKCS#12 não roda no Workers). Converta:

```bash
openssl pkcs12 -in certificado.pfx -clcerts -nokeys -out certificado.pem
```

Observações:

- O certificado do usuário precisa estar na cadeia configurada em `ssl_client_certificate`; para
  ICP-Brasil, inclua a cadeia completa da AC.
- Revogação (CRL/OCSP) pode ser configurada no nginx (`ssl_crl`) ou no proxy; o app valida
  fingerprint, atividade e validade.
- `CERT_LOGIN_URL` deve apontar para o mesmo domínio do painel (ou para um domínio que também
  proxy o app), porque o cookie de sessão é emitido para esse domínio.
- O `ssl_verify_client optional` é por `server`: o navegador pode pedir o certificado já na
  primeira visita. O usuário pode cancelar e usar o formulário de senha.
- Não há como habilitar mTLS de cliente apenas no `*.pages.dev`; sem o proxy, o endpoint
  `/cert-login` não é utilizável.

## Régua automática (cron)

Pages Functions não têm Cron Trigger. Crie um Worker com cron ou use GitHub Actions/cron-job.org
chamando:

```
POST https://renovacao-61n.pages.dev/api/regua?t=SEU_CRON_TOKEN
Header: X-Requested-With: fetch
```

Exemplo de Worker com cron:

```js
export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(fetch(`https://renovacao-61n.pages.dev/api/regua?t=${env.CRON_TOKEN}`, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
    }));
  },
};
```

A régua é idempotente: o e-mail começa em D30 e se repete a cada 7 dias até a renovação ser concluída
(`emitido`/`pago`/`perdido`); o WhatsApp dispara uma vez em cada limiar antigo (D60, D30, D15, D7, D1, D-3).

## E-mail

Workers não abrem conexão SMTP. Esta versão usa a API HTTP do **Resend**:

```
RESEND_API_KEY=re_...
EMAIL_FROM=renovacao@seudominio.com.br
```

Sem `RESEND_API_KEY`, o envio é **simulado** (o evento é gravado como enviado, mas nada sai).
Valide o domínio no Resend para o remetente funcionar.

## Webhook da Kommo

O endpoint do painel é:

```
https://renovacao-61n.pages.dev/webhook/kommo?t=SEU_WEBHOOK_TOKEN
```

Ele já está no ar: responde `403` sem token e `200 {"ok":true}` com o token correto.

Para registrar/atualizar automaticamente o webhook na Kommo (evento `status_lead`):

```powershell
cd cloudflare
$env:KOMMO_URL="https://suaconta.kommo.com"
$env:KOMMO_TOKEN="token-de-longa-duracao"
$env:WEBHOOK_TOKEN="SEU_WEBHOOK_TOKEN"
node scripts/kommo-webhook.mjs
```

O token da Kommo precisa ser de um usuário administrador. O script mascara o token na saída.

Também é possível cadastrar manualmente em **Kommo → Configurações → Integrações → Webhooks**,
com a URL acima e o evento de **mudança de etapa do lead** (`status_lead`).

Sem `KOMMO_URL`/`KOMMO_TOKEN` (ou configuração salva no painel), a integração fica em
**modo simulação**.

## Notas de segurança operacional

- Em `*.pages.dev`, sem autenticação configurada o app responde **503** (a menos que
  `ALLOW_PUBLIC=1`).
- O painel estático é protegido pelo `functions/_middleware.js`; as rotas `/api/*` e
  `/webhook/*` autenticam por conta própria.
- POSTs do painel exigem `X-Requested-With: fetch` (mitiga CSRF).
- O token da Kommo salvo pelo painel fica em texto puro no D1; prefira variável de ambiente.
- `TRUST_ACCESS=1` só deve ser usado **com o Access na frente** do domínio **e** com a validação
  do JWT do Access implementada; hoje o app não valida esse JWT.
- `AUTH_CERT=1` só deve ser usado com um proxy/mTLS confiável e `PROXY_SECRET`.

## Limites do plano gratuito

- D1: 5 GB, 5 milhões de leituras/dia e 100 mil escritas/dia.
- Pages: 500 builds/mês, 20 mil arquivos e banda/requisições estáticas ilimitadas.
- Pages Functions: 100 mil requisições/dia.
- Resend: 3.000 e-mails/mês (100/dia).
- Workers: CPU por requisição; a importação de planilhas grandes pode encostar no limite.

## Monitoramento do D1

Comando rápido:

```bash
npx wrangler d1 info renovacao
```

Ele mostra tamanho, tabelas e leituras/escritas das últimas 24 h. Na revisão de 10/11/2026:
**73,7 kB**, 5 tabelas, 0 linhas, 2 consultas de leitura e 0 escritas em 24 h — uso praticamente zero.

Cada importação grava uma linha por renovação; cada evento da régua grava uma linha em `eventos`;
cada falha de login atualiza `login_tentativas`. As tentativas expiradas são removidas
automaticamente. `eventos` não tem limpeza automática; se crescer muito, agende a exclusão de
registros antigos.

Acompanhe também **Workers & Pages → renovacao → Metrics** (Functions: 100 mil requisições/dia) e
**D1 → renovacao → Metrics**. Desde setembro/2026 a D1 aplica os limites diários do Free: ao
estourar, as consultas falham até o reset.

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `503 Painel sem autenticação` | defina `PAINEL_SENHA` (no `pages.dev` não há Access) |
| `401` no navegador | sessão expirada; abra `/login` novamente |
| `403 e-mail do Access não cadastrado` | cadastre o e-mail na aba Usuários ou ajuste a política |
| `403 certificado inválido` | cabeçalho errado, `PROXY_SECRET` diferente ou certificado não cadastrado |
| `binding D1 (DB) não configurado` | binding `DB` ausente no projeto Pages |
| e-mail não sai | `RESEND_API_KEY`/`EMAIL_FROM` ausentes ou domínio não verificado |
| webhook `403` | `WEBHOOK_TOKEN` diferente do `?t=` |
