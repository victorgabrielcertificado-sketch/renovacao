# Revisão do protótipo `renovacao.py`

Arquivos entregues nesta pasta:

| Arquivo | O que é |
|---|---|
| `renovacao.py` | protótipo corrigido (mesma estrutura/comandos do original) |
| `testes.py` | 33 testes automatizados (importação, régua, métricas, HTTP/webhook) |
| `REVISAO.md` | este relatório |

Ambiente preparado nesta máquina: **Python 3.12.10** (winget) + **pandas 3.1.1** + **openpyxl 3.1.5**.

```powershell
cd renovacao
python renovacao.py demo        # gera certificados_exemplo.xlsx, importa e roda a régua
python renovacao.py serve       # painel em http://localhost:8000
python testes.py -v             # 33 testes
```

> Se `python` não for reconhecido, abra um terminal novo (o winget atualiza o PATH) ou use
> `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.

---

## Bugs corrigidos

### Críticos (quebravam o fluxo)

1. **Régua abortava no primeiro lead sem e-mail.**
   `enviar_email()` fazia `m["To"] = r["email"]` sem validar. Com e-mail nulo o header virava
   `To: None` e o envio estourava; como não havia `try/except`, **nenhuma renovação seguinte era
   notificada**. Agora o e-mail é validado, o envio retorna `(ok, permanente)` e cada lead é
   isolado: uma falha não interrompe os demais.

2. **Falha de SMTP ou Kommo derrubava a régua inteira.**
   `sync_kommo()` e `enviar_email()` podiam levantar exceção no meio do `for`. Agora
   `sync_kommo()` nunca levanta (loga `falhou`), e falha temporária de e-mail mantém
   `regua_ultima` sem avançar para tentar de novo no próximo ciclo.

3. **`.CSV` em maiúsculas não importava.**
   `importar()` testava `path.endswith(".csv")` (case-sensitive) e mandava `ARQUIVO.CSV` para
   `read_excel`. Corrigido com `path.lower()` e aceitando `.xls/.xlsx/.xlsm`.

4. **CSV com BOM ou separador `;` era lido como uma coluna só.**
   Planilhas exportadas pelo Excel brasileiro têm `;` e BOM (`\ufeffcliente`). Agora lê com
   `utf-8-sig`, detecta o separador (`sep=None, engine="python"`) e cai para `sep=";"` quando
   necessário. Também normaliza acentos/maiúsculas nos nomes das colunas.

5. **Data serial do Excel virava 1970.**
   `46000` era interpretado como nanossegundos. Agora, coluna numérica é convertida com
   `unit="D", origin="1899-12-30"` (46000 → 09/12/2025).

6. **Métricas e régua discordavam por 1 dia.**
   `metricas()` usava `date('now')` (UTC) e a régua `date.today()` (local). No Brasil (UTC−3),
   entre 21h e meia-noite os dois discordam. Agora a data local é passada como parâmetro.

7. **SQLite compartilhado entre threads + servidor single-thread.**
   Uma conexão global com `check_same_thread=False` era usada pelo servidor e pela thread da
   régua, podendo dar `database is locked`; e `HTTPServer` puro travava o painel durante um envio
   de e-mail. Agora: **uma conexão por thread**, WAL, `busy_timeout=30s` e
   `ThreadingHTTPServer`.

8. **Régua duplicava avisos.**
   A thread de 6h e o botão “Rodar régua agora” podiam rodar juntos. Adicionado lock
   (`_regua_lock`) que ignora a segunda chamada.

9. **Status arbitrário pela API.**
   `POST /api/avancar` aceitava qualquer string; o lead sumia da régua e `STATUS.index()` podia
   estourar. Agora `avancar()` e a API validam contra `STATUS`.

10. **Configuração não podia ser limpa.**
    `carregar_cfg()` só aplicava valores “truthy”, então salvar URL/token vazio mantinha o valor
    antigo. `int(K["pipeline"])` com texto derrubava a request. Agora o painel faz atualização
    parcial, valida URL/ID e permite remover o token (botão “Limpar token”).

11. **Segurança do painel/webhook.**
    Sem `PAINEL_SENHA`, o painel subia em `0.0.0.0` sem login; o webhook aceitava qualquer um sem
    `WEBHOOK_TOKEN`; não havia proteção CSRF. Agora:
    - sem senha, escuta só em `127.0.0.1` (e avisa no console);
    - comparação de credenciais com `hmac.compare_digest`;
    - POSTs do painel exigem `X-Requested-With: fetch` (bloqueia CSRF, pois o navegador não manda
      esse header cross-site sem preflight);
    - webhook valida `WEBHOOK_TOKEN`;
    - CSP, `X-Content-Type-Options`, `Cache-Control: no-store` e limite de 20 MB no upload.

12. **Sem timeout em SMTP/Kommo.**
    Uma conexão pendurada travava request/régua. Agora SMTP tem `timeout=20`, Kommo `timeout=15`,
    e porta 465 usa `SMTP_SSL` (antes `starttls()` incondicional falhava na 465).

### Médios

- `telefone` ausente virava a string `"nan"` e contava como contato válido; e-mail inválido ia
  para o SMTP. Agora telefone precisa de 10+ dígitos e e-mail passa por regex.
- Importação não era transacional: erro no meio deixava lixo. Agora `BEGIN IMMEDIATE`/`ROLLBACK`.
- Reimportar não atualizava contato e podia duplicar. Agora atualiza e-mail/telefone/valor
  **sem** mexer em status/régua.
- `regua_ultima` era gravado mesmo sem contato; agora registra evento `ignorado`.
- Textos: `"venceu há 1 dias"` e `"vence em -3 dias"` → `_quando()` com singular/plural e
  “vence hoje”.
- `json.dumps` podia emitir `NaN` (JSON inválido para o navegador) e quebrar o painel inteiro.
  Agora `_dict()` troca NaN/Infinity por `null` e o servidor usa `allow_nan=False`.
- `K` só era lido do ambiente; `KOMMO_ETAPAS` inválido derrubava o startup. Agora `_json_env()`
  tolerante e recarga do banco a cada salvamento.
- `demo()` sem `openpyxl` dava erro obscuro; agora mensagem clara.
- CLI sem validação de argumentos (`IndexError` em `avancar` sem args); agora `argparse` com
  `choices=STATUS` e `--hoje`/`--mes` para testar datas.
- Faltavam índices em `vencimento`, `status`, `kommo_lead_id` e `eventos.renovacao_id`.

### Painel (UI)

- `padding-top: env(safe-area-inset-top)` estava no `:root` e não fazia efeito → movido para `body`.
- Possível XSS no `value` dos IDs de etapa → `esc()`.
- `abrir()` chamava `render()` duas vezes e o painel fazia 3 requisições (uma redundante) →
  agora 2 e o histórico é pintado depois da tabela.
- Valores `R$ 180.0` → formatação `pt-BR`; `null` → `—`.
- Falha de rede deixava a tela em branco → `try/catch` com toast.
- Título “Novembro **De** 2026” por `text-transform:capitalize` → removido e mês capitalizado no JS.
- `aria-current` agora é `"true"/"false"` (string), não booleano.

---

## O que **não** mudou (de propósito)

- O e-mail de vencimento começa em D30 e se repete a cada 7 dias até a renovação ser concluída
  (`emitido`/`pago`/`perdido`). O WhatsApp continua com os limiares fixos `[60, 30, 15, 7, 1, -3]` e
  atraso máximo de 30 dias (`LIMITE_ATRASO`).
- WhatsApp continua sendo um **ponto de extensão** (nota no lead da Kommo), não envio real.
- O token da Kommo continua em texto puro na tabela `config` do SQLite (protótipo). Para produção,
  use variável de ambiente ou cofre de segredos.
- O schema ganhou `renovacoes.ultimo_email` (migração leve automática no SQLite e migração D1).
- `demo`/`exportar` escrevem arquivos no diretório atual.

## Atualização: login por certificado digital

Recursos adicionados depois da revisão inicial:

- Tabela `usuarios` (nome, e-mail, perfil, impressão digital SHA-256, sujeito, validade, ativo).
- `ler_certificado()` / `cert_info()`: aceita `.cer`, `.crt`, `.pem`, `.pfx` e `.p12`; do `.pfx`
  só a parte pública é extraída (a chave privada é descartada e a senha não é armazenada).
- `_auth()`: certificado via proxy **ou** senha básica de emergência. O cabeçalho `X-Client-Cert`
  só é aceito quando `X-Proxy-Secret` confere com `PROXY_SECRET`.
- Rotas `/api/eu`, `/api/usuarios` (GET/POST), `/api/usuarios/ativo` e `/api/usuarios/remover`.
- Aba **Usuários** no painel: lista, ativa/desativa, remove e cadastra certificados.
- Perfis `admin` e `operador`: operador acessa Renovações e Configurações (importação e Kommo),
  mas não acessa a aba/rotas de Usuários (GET e POST).
- Com `AUTH_CERT=1` o app escuta só em `127.0.0.1`; o proxy (nginx) é quem fica exposto e exige o
  certificado no TLS.
- Primeiro administrador: subir com `PAINEL_SENHA` (login de emergência), cadastrar o certificado
  admin e depois remover a senha se desejar.

## Validação executada

- `python -m py_compile renovacao.py testes.py` → OK
- `python testes.py -v` → **27 testes, todos OK** (inclui certificado válido, vencido, inativo,
  operador × admin, cadastro PEM/DER/PFX e senha errada)
- Teste manual do painel no navegador: renderização, troca de status, histórico, abas de
  configurações/usuários e webhook (token certo/errado) — OK
- JavaScript embutido validado com `node --check` → OK
