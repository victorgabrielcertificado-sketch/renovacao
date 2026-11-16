# Renovação de certificados digitais

Protótipo em Python para acompanhar o vencimento de certificados digitais, disparar avisos
automáticos (e-mail/WhatsApp), sincronizar leads com a Kommo CRM e medir a taxa de renovação.

## O que faz

- **Importa planilhas** `.csv`, `.xls`, `.xlsx` (detecta `;`, BOM, acentos e serial de data do Excel).
- **Régua de avisos** em D−60, D−30, D−15, D−7, D−1 e D+3 (vencido), sem repetir envios.
- **E-mail** via SMTP (porta 465 com SSL ou 587 com STARTTLS) e **WhatsApp** como ponto de
  extensão via nota no lead da Kommo.
- **Sincroniza com a Kommo**: cria/atualiza lead, move etapa e registra nota.
- **Webhook** para receber mudanças de etapa da Kommo.
- **Login por certificado digital** (e-CPF/e-CNPJ) via proxy TLS, com perfis `admin`/`operador`.
- **Painel web** responsivo (claro/escuro) com funil, taxa de renovação por mês, histórico do lead,
  importação de planilha, configuração da integração e gestão de usuários/certificados.
- **Métricas**: total que vence no mês, taxa de renovação, receita recebida, por mês e por tipo.

## Requisitos

- Python 3.10+
- `pandas`
- `openpyxl` (para `.xlsx`)
- `cryptography` (somente para o login por certificado)

```bash
pip install pandas openpyxl cryptography
```

## Uso

```bash
python renovacao.py demo                  # gera dados de exemplo, importa e roda a régua
python renovacao.py importar planilha.xlsx
python renovacao.py regua                 # ou: regua --hoje 2026-11-08
python renovacao.py metricas              # ou: metricas --mes 2026-11
python renovacao.py avancar 1 pago
python renovacao.py exportar relatorio.xlsx
python renovacao.py serve                 # painel em http://localhost:8000
```

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

## Configuração (variáveis de ambiente)

Sem elas, e-mail e Kommo rodam em **modo simulação** (só imprimem no console).

| Variável | Descrição |
|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | envio de e-mail (fallback) |
| `SMTP_SEGURANCA` | `starttls` (padrão), `ssl` ou `none` |
| `EMAIL_ASSUNTO`, `EMAIL_CORPO` | modelo do e-mail (a aba **Mensagens** tem prioridade) |
| `EMAIL_REMETENTE_NOME`, `EMAIL_RESPOSTA` | nome do remetente e endereço de resposta |
| `MENSAGEM_WHATSAPP`, `MENSAGEM_KOMMO` | modelos de WhatsApp e Kommo (a aba **Mensagens** tem prioridade) |
| `LINK_AGENDA` | link de agendamento usado nas mensagens |
| `KOMMO_URL` | `https://sua-conta.kommo.com` |
| `KOMMO_TOKEN` | token de longa duração |
| `KOMMO_PIPELINE` | ID do funil |
| `KOMMO_ETAPAS` | JSON `{"a_vencer":123,"email_enviado":124,...}` (status → ID da etapa) |
| `PORT`, `HOST` | porta e interface do painel (padrão `8000` / `127.0.0.1` sem senha) |
| `PAINEL_USER`, `PAINEL_SENHA` | login básico de emergência (admin) |
| `AUTH_CERT=1` | habilita login por certificado digital |
| `PROXY_SECRET` | segredo que o proxy envia em `X-Proxy-Secret` (obrigatório com `AUTH_CERT=1`) |
| `WEBHOOK_TOKEN` | token exigido em `/webhook/kommo?t=...` |
| `DB` | caminho do SQLite (padrão `renovacao.db`) |

As configurações salvas pelo painel têm prioridade sobre as variáveis de ambiente.

### Webhook da Kommo

Cadastre a URL `https://seu-servidor/webhook/kommo` (acrescente `?t=SEU_WEBHOOK_TOKEN` se
definido). A mudança de etapa é aplicada ao lead correspondente em `renovacoes.kommo_lead_id`.

## Login por certificado digital

O certificado é exigido no **TLS** pelo proxy (prova de posse da chave privada). O proxy repassa o
PEM em `X-Client-Cert` (urlencoded) e um segredo em `X-Proxy-Secret`. O app confere a impressão
digital SHA-256 contra a tabela `usuarios`, se o usuário está ativo e se o certificado está dentro
da validade. A chave privada nunca chega ao app.

Exemplo de `nginx`:

```nginx
server {
    listen 443 ssl;
    server_name renovacao.exemplo.com;

    ssl_certificate         /etc/nginx/certs/servidor.crt;
    ssl_certificate_key     /etc/nginx/certs/servidor.key;
    ssl_client_certificate  /etc/nginx/certs/ca.crt;   # CA dos certificados dos usuários
    ssl_verify_client       on;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Client-Cert $ssl_client_escaped_cert;
        proxy_set_header   X-Proxy-Secret "troque-este-segredo";
    }
}
```

No app:

```bash
AUTH_CERT=1 PROXY_SECRET=troque-este-segredo PAINEL_SENHA=senha-inicial python renovacao.py serve
```

Com `AUTH_CERT=1` o app escuta só em `127.0.0.1` (o proxy é quem fica exposto).

**Primeiro administrador:** suba com `PAINEL_SENHA` definida (login básico de emergência), entre em
**Usuários**, cadastre o primeiro certificado como `admin` e depois remova a senha se quiser. O
cadastro aceita `.cer`, `.crt`, `.pem`, `.pfx` e `.p12` (a senha do `.pfx` é usada só para extrair
o certificado e não é armazenada).

**Perfis:** o `admin` vê todas as abas; o `operador` vê **Renovações**, **Mensagens** e
**Configurações** (importação de planilha e integração Kommo). A aba **Usuários** é exclusiva do
perfil `admin`.

**Tema e menu:** o botão de tema no canto superior alterna **Automático → Claro → Escuro**. O menu
lateral pode ser recolhido e fixado pelos botões ao lado do título; o tema e o estado fixado ficam
salvos no navegador.

## Mensagens de renovação

A aba **Mensagens** reúne três modelos com as mesmas variáveis: `{cliente}`, `{tipo}`,
`{vencimento}`, `{quando}`, `{dias}`, `{valor}`, `{link}`, `{empresa}` e `{remetente}`. Clique em
uma variável para inseri-la no campo em foco.

- **E-mail:** assunto e corpo enviados pela régua e pelo teste.
- **WhatsApp:** usada no botão **WhatsApp** do cliente (abre o `wa.me` com o texto pronto) e nas
  notas da régua.
- **Kommo CRM:** usada no botão **Enviar para Kommo** do cliente, que cria uma nota no lead.

A aba também configura o **remetente** (nome, e-mail, responda-para e link de agendamento) e o
**servidor SMTP** (servidor, porta, usuário, senha e segurança `STARTTLS`, `SSL/TLS` ou sem TLS).
O botão **Teste** envia uma mensagem de exemplo. A senha fica salva no banco local; o campo vazio
mantém a senha atual. A API nunca devolve a senha, apenas o indicador `smtp_senha_definida`.

Nos **dados do cliente**, o painel mostra o e-mail importado e permite:
- ativar/desativar os avisos de renovação para aquele cliente;
- usar o e-mail importado ou cadastrar um **e-mail alternativo** de envio;
- abrir o **WhatsApp** com a mensagem padrão ou **enviar a mensagem para a Kommo**.

A régua usa o e-mail alternativo quando ele existe; se o envio estiver desativado, o cliente é
ignorado sem alterar o status. A importação não sobrescreve o e-mail alternativo já escolhido.

## Versão Cloudflare Pages (`*.pages.dev`)

A pasta [`cloudflare/`](cloudflare/README.md) traz a mesma aplicação para rodar em
**Cloudflare Pages + Functions + D1**, publicada em `https://renovacao.pages.dev`.

```bash
cd cloudflare
npm install
npm run db:local && npm run db:seed
npm run dev          # http://127.0.0.1:8788
npm test             # 12 testes unitários
npm run test:api     # 28 verificações de integração
```

Deploy resumido (detalhes em [`cloudflare/README.md`](cloudflare/README.md)):

```bash
npx wrangler login
npx wrangler d1 create renovacao          # copie o database_id para wrangler.toml
npx wrangler d1 execute renovacao --remote --file=schema.sql
npx wrangler pages project create renovacao --production-branch main
npx wrangler pages secret put PAINEL_SENHA --project-name renovacao
npx wrangler pages secret put WEBHOOK_TOKEN --project-name renovacao
npx wrangler pages secret put CRON_TOKEN --project-name renovacao
npx wrangler pages deploy public --project-name renovacao
```

Diferenças: banco **D1** em vez de SQLite, planilhas via **SheetJS**, e-mail via **Resend**
(Workers não abrem SMTP), régua disparada por **cron externo** e login por **formulário com cookie**,
**Cloudflare Access** ou certificado via cabeçalho `X-Client-Cert` (`.pfx`/`.p12` não são aceitos nessa versão).
O painel aplica HSTS, CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, cookies `__Host-`
(HttpOnly/Secure/SameSite=Lax), CSRF por token e limite de tentativas de login; detalhes na
seção **Segurança** de [`cloudflare/README.md`](cloudflare/README.md).

## Modelo de dados (SQLite)

- `renovacoes` — cliente, contato, tipo, vencimento, valor, status, lead da Kommo, último aviso.
- `usuarios` — nome, e-mail, perfil, impressão digital SHA-256 do certificado, validade e ativo.
- `eventos` — histórico append-only (importação, e-mail, WhatsApp, mudança de etapa, erros).
- `config` — configuração salva pelo painel.

Status: `a_vencer → email_enviado → whatsapp_enviado → agendado → emitido → pago` (ou `perdido`).

## Testes

```bash
python testes.py -v
```

38 testes cobrindo importação (BOM, `;`, `.CSV`, serial do Excel, contatos inválidos, regras de validade), régua
(aviso em D30, repetição a cada 7 dias, deduplicação, sem contato), métricas, API HTTP, CSRF, webhook,
modelos de mensagem, mensagem para a Kommo e login por certificado
(certificado válido, vencido, inativo, operador × admin, cadastro PEM e DER).

## Segurança

- Sem `PAINEL_SENHA` e sem `AUTH_CERT=1`, o painel escuta apenas em `127.0.0.1`.
- Com `AUTH_CERT=1`, o app também escuta em `127.0.0.1` e só aceita `X-Client-Cert` se
  `X-Proxy-Secret` conferir com `PROXY_SECRET` — nunca exponha o app direto sem o proxy.
- POSTs do painel exigem `X-Requested-With: fetch` (mitiga CSRF).
- O token da Kommo fica em texto puro no SQLite — para produção, prefira variável de ambiente ou
  um cofre de segredos.
- Sirva atrás de HTTPS (proxy reverso) se for expor na internet.

## Limitações

- WhatsApp é um ponto de extensão (nota no lead), não um envio real.
- E-mail de vencimento começa em D30 e se repete a cada 7 dias até a renovação ser concluída
  (`emitido`/`pago`/`perdido`); WhatsApp mantém os limiares `[60, 30, 15, 7, 1, -3]` e o atraso máximo de 30 dias.
- Migração leve automática para adicionar `ultimo_email` em bancos SQLite antigos.

## Estrutura

```
renovacao.py   aplicação (CLI + servidor + painel)
testes.py      testes automatizados
REVISAO.md     relatório de revisão e correções
```
