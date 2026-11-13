#!/usr/bin/env python3
"""Protótipo - renovação de certificados digitais.

Uso web:  python renovacao.py serve
Uso CLI:  python renovacao.py demo | importar <planilha> | regua [--hoje AAAA-MM-DD] |
          metricas [--mes AAAA-MM] | avancar <id> <status> | exportar [arquivo.xlsx]

Config por variáveis de ambiente (sem elas, e-mail e Kommo rodam em modo simulação):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_TLS=0 (desliga STARTTLS)
  LINK_AGENDA
  KOMMO_URL (https://SUB.kommo.com), KOMMO_TOKEN, KOMMO_PIPELINE,
  KOMMO_ETAPAS='{"a_vencer":123,"email_enviado":124,...}'  (status -> id da etapa)
  Painel: PORT, HOST, PAINEL_USER, PAINEL_SENHA, WEBHOOK_TOKEN, DB
  Login por certificado: AUTH_CERT=1, PROXY_SECRET (o proxy repassa X-Client-Cert em PEM urlencoded)
"""
import argparse, base64, calendar, hmac, json, os, random, re, smtplib, sqlite3, tempfile, threading, time, unicodedata, urllib.error, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import pandas as pd

try:                              # login por certificado é opcional
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import pkcs12
    _CRYPTO_OK = True
except ImportError:
    x509 = hashes = pkcs12 = None
    _CRYPTO_OK = False

REGUA = [60, 30, 15, 7, 1, -3]   # limiares do WhatsApp (negativo = vencido)
LIMITE_ATRASO = 30               # WhatsApp não notifica quem venceu há mais de X dias
EMAIL_INICIO = 30                # primeiro e-mail de vencimento: 30 dias antes
EMAIL_INTERVALO = 7              # repete o e-mail a cada 7 dias até concluir a renovação
STATUS = ["a_vencer", "email_enviado", "whatsapp_enviado", "agendado", "emitido", "pago", "perdido"]
# Durações permitidas, em meses, por tipo de certificado.
DURACOES = {"a1": [12], "a3": [12, 24, 36], "safeid": [3, 12]}
MAX_UPLOAD = 20 * 1024 * 1024    # 20 MB (planilhas)
MAX_CERT = 1 * 1024 * 1024       # 1 MB (certificados)
MES_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _json_env(nome, padrao):
    """json.loads tolerante: configuração errada não impede o sistema de subir."""
    bruto = os.getenv(nome)
    if not bruto:
        return padrao
    try:
        valor = json.loads(bruto)
        return valor if isinstance(valor, type(padrao)) else padrao
    except json.JSONDecodeError as e:
        print(f"[aviso] {nome} inválido ({e}); usando o padrão")
        return padrao


_ENV_K = {"url": os.getenv("KOMMO_URL"), "token": os.getenv("KOMMO_TOKEN"),
          "pipeline": os.getenv("KOMMO_PIPELINE"), "etapas": _json_env("KOMMO_ETAPAS", {})}
K = dict(_ENV_K)
_cfg_lock = threading.Lock()
_local = threading.local()
_regua_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS renovacoes(id INTEGER PRIMARY KEY, cliente TEXT NOT NULL, email TEXT, telefone TEXT,
  tipo TEXT NOT NULL, vencimento TEXT NOT NULL, valor REAL, status TEXT NOT NULL DEFAULT 'a_vencer',
  kommo_lead_id INTEGER, regua_ultima INTEGER, ultimo_email TEXT,
  UNIQUE(cliente, tipo, vencimento));
CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, email TEXT,
  perfil TEXT NOT NULL DEFAULT 'operador', fp TEXT UNIQUE, sujeito TEXT, validade TEXT,
  ativo INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, renovacao_id INTEGER, ts TEXT NOT NULL, tipo TEXT NOT NULL,
  canal TEXT, resultado TEXT, detalhe TEXT);
CREATE INDEX IF NOT EXISTS ix_renov_venc   ON renovacoes(vencimento);
CREATE INDEX IF NOT EXISTS ix_renov_status ON renovacoes(status);
CREATE INDEX IF NOT EXISTS ix_renov_lead   ON renovacoes(kommo_lead_id);
CREATE INDEX IF NOT EXISTS ix_eventos_rid  ON eventos(renovacao_id);
CREATE INDEX IF NOT EXISTS ix_usuarios_fp  ON usuarios(fp);
"""


def db():
    """Uma conexão SQLite por thread (o painel é multi-thread e a régua roda em outra thread)."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(os.getenv("DB", "renovacao.db"), isolation_level=None,
                            check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        # Migração leve para bancos criados antes da coluna ultimo_email.
        colunas = {linha[1] for linha in c.execute("PRAGMA table_info(renovacoes)")}
        if "ultimo_email" not in colunas:
            try:
                c.execute("ALTER TABLE renovacoes ADD COLUMN ultimo_email TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        _local.conn = c
    return c


def _dict(r):
    """sqlite3.Row -> dict, trocando NaN/Infinity por None (JSON válido para o navegador)."""
    saida = {}
    for k in r.keys():
        v = r[k]
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            v = None
        saida[k] = v
    return saida


def log(rid, tipo, canal="", resultado="", detalhe=""):  # histórico/tabulação do lead (só insere)
    db().execute("INSERT INTO eventos(renovacao_id,ts,tipo,canal,resultado,detalhe) VALUES(?,?,?,?,?,?)",
                 (rid, datetime.now().isoformat(timespec="seconds"), tipo, canal, resultado, detalhe))


# ---------- 1. Planilhas -> banco ----------
ALIAS = {"cliente": ["cliente", "nome", "nome do cliente", "razao social", "empresa"],
         "email": ["email", "e-mail", "e mail", "correio eletronico"],
         "telefone": ["telefone", "whatsapp", "celular", "fone", "tel"],
         "tipo": ["tipo", "certificado", "produto", "tipo de certificado"],
         "vencimento": ["vencimento", "data de vencimento", "data vencimento", "expira em", "validade"],
         "valor": ["valor", "preco", "valor da renovacao"],
         "emissao": ["emissao", "data de emissao", "data emissao", "emitido em",
                     "inicio", "data de inicio", "inicio da validade"],
         "duracao": ["duracao", "periodo", "tempo", "duracao em meses", "duracao em anos",
                     "validade em meses", "validade em anos", "validade"]}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


_ALIAS = {k: {_norm(a) for a in v} for k, v in ALIAS.items()}


def _coluna(df, chave):
    for alias in ALIAS[chave]:
        alvo = _norm(alias)
        for c in df.columns:
            if _norm(c) == alvo:
                return c
    return None


def _tipo_regra(tipo):
    t = _norm(tipo)
    if re.search(r"\bsafe\s*id\b|\bsafeid\b", t):
        return "safeid"
    if re.search(r"\ba1\b", t):
        return "a1"
    if re.search(r"\ba3\b", t):
        return "a3"
    return None


def _meses_texto(valor):
    t = _norm(valor)
    m = re.search(r"(\d+)\s*(mes|meses)\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(ano|anos)\b", t)
    if m:
        return int(m.group(1)) * 12
    return None


def _duracao_meses(valor, tipo):
    regra = _tipo_regra(tipo)
    if valor is not None and not (isinstance(valor, float) and pd.isna(valor)) and str(valor).strip() != "":
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            n = int(valor)
            if regra == "a3" and n in (1, 2, 3):
                return n * 12
            if regra == "a1" and n == 1:
                return 12
            if regra == "safeid" and n == 1:
                return 12
            return n if n > 0 else None
        por_texto = _meses_texto(valor)
        if por_texto is not None:
            return por_texto
        m = re.fullmatch(r"(\d+)", _norm(valor))
        if m:
            n = int(m.group(1))
            if regra == "a3" and n in (1, 2, 3):
                return n * 12
            if regra == "a1" and n == 1:
                return 12
            if regra == "safeid" and n == 1:
                return 12
            return n if n > 0 else None
    return _meses_texto(tipo)


def _duracao_valida(tipo, meses):
    regra = _tipo_regra(tipo)
    if not regra or not meses:
        return True
    return meses in DURACOES[regra]


def _add_meses(dia, meses):
    if not isinstance(dia, date) or not isinstance(meses, int) or meses <= 0:
        return None
    total = dia.month - 1 + meses
    ano = dia.year + total // 12
    mes = total % 12 + 1
    return date(ano, mes, min(dia.day, calendar.monthrange(ano, mes)[1]))


def _texto(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "nat", "<na>") else None


def _telefone(v):
    s = _texto(v)
    if not s:
        return None
    return s if len(re.sub(r"\D", "", s)) >= 10 else None


def _ler_planilha(path):
    low = path.lower()
    if low.endswith(".csv"):
        erro = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                df = pd.read_csv(path, encoding=enc, sep=None, engine="python")
            except (UnicodeDecodeError, pd.errors.ParserError) as e:
                erro = e
                continue
            if df.shape[1] == 1:            # separador ';' não detectado pelo sniffer
                try:
                    alt = pd.read_csv(path, encoding=enc, sep=";")
                    if alt.shape[1] > 1:
                        return alt
                except (UnicodeDecodeError, pd.errors.ParserError):
                    pass
            return df
        raise ValueError(f"CSV ilegível: {erro}")
    if low.endswith((".xls", ".xlsx", ".xlsm")):
        try:
            return pd.read_excel(path)
        except ImportError as e:
            raise ValueError("instale openpyxl para ler .xlsx (pip install openpyxl)") from e
    raise ValueError("formato não suportado: use .csv, .xls ou .xlsx")


def importar(path):
    df = _ler_planilha(path)
    df = df.loc[:, ~df.columns.duplicated()]
    col = {k: _coluna(df, k) for k in ALIAS}
    faltando = []
    if not col["cliente"]:
        faltando.append("cliente")
    if not col["vencimento"] and not col["emissao"]:
        faltando.append("vencimento ou emissao")
    if faltando:
        raise ValueError("colunas obrigatórias ausentes: " + ", ".join(faltando)
                         + " (encontradas: " + ", ".join(map(str, df.columns)) + ")")

    if col["vencimento"]:
        venc_serie = df[col["vencimento"]]
        if pd.api.types.is_numeric_dtype(venc_serie):   # serial do Excel (ex.: 46000)
            venc = pd.to_datetime(venc_serie, unit="D", origin="1899-12-30", errors="coerce")
        else:
            venc = pd.to_datetime(venc_serie, dayfirst=True, errors="coerce")
    else:
        venc = pd.Series(pd.NaT, index=df.index)

    if col["emissao"]:
        em_serie = df[col["emissao"]]
        if pd.api.types.is_numeric_dtype(em_serie):
            emissao = pd.to_datetime(em_serie, unit="D", origin="1899-12-30", errors="coerce")
        else:
            emissao = pd.to_datetime(em_serie, dayfirst=True, errors="coerce")
    else:
        emissao = pd.Series(pd.NaT, index=df.index)

    dados = pd.DataFrame({
        "cliente": df[col["cliente"]],
        "email": df[col["email"]] if col["email"] else None,
        "telefone": df[col["telefone"]] if col["telefone"] else None,
        "tipo": df[col["tipo"]] if col["tipo"] else "e-CNPJ",
        "vencimento": venc,
        "vencimento_bruto": df[col["vencimento"]] if col["vencimento"] else None,
        "emissao": emissao,
        "duracao": df[col["duracao"]] if col["duracao"] else None,
        "valor": pd.to_numeric(df[col["valor"]], errors="coerce") if col["valor"] else None,
    })

    c = db()
    novas = atualizadas = ignoradas = avisos = 0
    detalhes = []

    def avisar(msg):
        nonlocal avisos
        avisos += 1
        if len(detalhes) < 10:
            detalhes.append(msg)

    c.execute("BEGIN IMMEDIATE")          # importação tudo-ou-nada
    try:
        for r in dados.to_dict("records"):
            cliente = _texto(r["cliente"])
            tipo = _texto(r["tipo"]) or "e-CNPJ"
            regra = _tipo_regra(tipo)
            dia = r["vencimento"]
            vencimento = None if pd.isna(dia) else dia.date()
            em = r["emissao"]
            emissao_dia = None if pd.isna(em) else em.date()
            duracao = _duracao_meses(r["duracao"], tipo)

            # coluna "Validade" pode vir como duração ("1 ano", "3 meses")
            if vencimento is None and r["vencimento_bruto"] is not None:
                alt = _duracao_meses(r["vencimento_bruto"], tipo)
                if alt is not None:
                    duracao = duracao if duracao is not None else alt

            if vencimento is None:
                if emissao_dia and duracao is not None:
                    vencimento = _add_meses(emissao_dia, duracao)
                elif emissao_dia and regra and len(DURACOES[regra]) == 1:
                    duracao = DURACOES[regra][0]
                    vencimento = _add_meses(emissao_dia, duracao)

            if not cliente or vencimento is None:
                ignoradas += 1
                continue

            if duracao is not None and not _duracao_valida(tipo, duracao):
                permitidas = ", ".join(map(str, DURACOES[regra])) if regra else "—"
                avisar(f"{cliente}: {tipo} com {duracao} meses; permitido: {permitidas} meses")

            iso = vencimento.isoformat()
            if emissao_dia:
                if duracao is not None:
                    esperado = _add_meses(emissao_dia, duracao)
                    if esperado and esperado.isoformat() != iso:
                        avisar(f"{cliente}: vencimento {iso} não confere com "
                               f"{emissao_dia.isoformat()} + {duracao} meses ({esperado.isoformat()})")
                elif regra:
                    permitidas = DURACOES[regra]
                    if not any(_add_meses(emissao_dia, m).isoformat() == iso for m in permitidas):
                        avisar(f"{cliente}: vencimento {iso} não confere com "
                               f"{emissao_dia.isoformat()} + {'/'.join(map(str, permitidas))} meses")

            email = _texto(r["email"])
            email = email if email and EMAIL_RE.match(email) else None
            fone = _telefone(r["telefone"])
            valor = None if pd.isna(r["valor"]) else float(r["valor"])
            atual = c.execute("SELECT id FROM renovacoes WHERE cliente=? AND tipo=? AND vencimento=?",
                              (cliente, tipo, iso)).fetchone()
            if atual:
                # não mexe em status/régua: só completa dados de contato
                c.execute("""UPDATE renovacoes SET email=COALESCE(?,email), telefone=COALESCE(?,telefone),
                             valor=COALESCE(?,valor) WHERE id=?""", (email, fone, valor, atual["id"]))
                log(atual["id"], "atualizado", "planilha")
                atualizadas += 1
            else:
                cur = c.execute("""INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor)
                                   VALUES(?,?,?,?,?,?)""", (cliente, email, fone, tipo, iso, valor))
                log(cur.lastrowid, "importado", "planilha")
                novas += 1
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    print(f"{novas} novas, {atualizadas} atualizadas, {ignoradas} ignoradas, "
          f"{avisos} avisos ({len(dados)} linhas)")
    return {"novas": novas, "atualizadas": atualizadas, "ignoradas": ignoradas,
            "avisos": avisos, "detalhes": detalhes}


# ---------- 2. Kommo ----------
def _int_ou_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def kommo(method, path, body=None):
    if not (K["url"] and K["token"]):
        print(f"  [kommo simulado] {method} {path} {json.dumps(body, ensure_ascii=False)[:110]}")
        return {}
    data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
    req = urllib.request.Request(K["url"].rstrip("/") + path, method=method, data=data,
                                 headers={"Authorization": "Bearer " + K["token"],
                                          "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detalhe = e.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Kommo HTTP {e.code}: {detalhe}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Kommo indisponível: {e.reason}") from e
    return json.loads(raw) if raw else {}


def sync_kommo(rid, nota=None):
    """Sincroniza o lead. Nunca levanta exceção: falha de integração não derruba painel/régua."""
    r = db().execute("SELECT * FROM renovacoes WHERE id=?", (rid,)).fetchone()
    if not r:
        return False
    if not (K["url"] and K["token"]):
        etapa = K["etapas"].get(r["status"])
        if r["kommo_lead_id"]:
            kommo("PATCH", f"/api/v4/leads/{r['kommo_lead_id']}", {"status_id": etapa})
        else:
            kommo("POST", "/api/v4/leads", [{"name": f"Renovação {r['tipo']} - {r['cliente']}",
                                             "status_id": etapa}])
        return True

    body = {"name": f"Renovação {r['tipo']} - {r['cliente']}", "price": int(r["valor"] or 0)}
    pipeline = _int_ou_none(K["pipeline"])
    if pipeline:
        body["pipeline_id"] = pipeline
    etapa = _int_ou_none(K["etapas"].get(r["status"]))
    if etapa:
        body["status_id"] = etapa
    elif K["etapas"]:
        log(rid, "kommo", "kommo", "aviso", f"sem etapa mapeada para {r['status']}")

    lid = r["kommo_lead_id"]
    try:
        if lid:
            kommo("PATCH", f"/api/v4/leads/{lid}", body)
        else:
            resp = kommo("POST", "/api/v4/leads", [body])
            lid = ((resp.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
            if lid:
                db().execute("UPDATE renovacoes SET kommo_lead_id=? WHERE id=?", (lid, rid))
        if nota and lid:
            kommo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {"text": nota}}])
    except Exception as e:
        print("  [kommo] erro:", e)
        log(rid, "kommo", "kommo", "falhou", str(e)[:300])
        return False
    return True


# ---------- 3. Fluxo de status ----------
def avancar(rid, novo, origem="sistema", detalhe=""):
    if novo not in STATUS:
        raise ValueError(f"status inválido: {novo!r}")
    r = db().execute("SELECT * FROM renovacoes WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] == novo:
        return False
    db().execute("UPDATE renovacoes SET status=? WHERE id=?", (novo, rid))
    log(rid, "mudanca_etapa", origem, novo, f"{r['status']} -> {novo} {detalhe}".strip())
    if origem not in ("kommo", "demo"):   # kommo = veio do webhook, não reenvia
        sync_kommo(rid)
    return True


# ---------- 4. Régua de notificações ----------
def _quando(dias):
    if dias > 0:
        return f"vence em {dias} dia" + ("s" if dias != 1 else "")
    if dias == 0:
        return "vence hoje"
    d = -dias
    return f"venceu há {d} dia" + ("s" if d != 1 else "")


def enviar_email(r, dias):
    """Retorna (enviado, permanente). permanente=True => repetir não adianta."""
    destino = _texto(r["email"])
    if not destino or not EMAIL_RE.match(destino):
        log(r["id"], "email", "email", "ignorado", "sem e-mail válido")
        return False, True
    quando = _quando(dias)
    m = EmailMessage()
    m["Subject"] = f"Seu certificado {r['tipo']} {quando}"
    m["From"] = os.getenv("SMTP_FROM", "renovacao@exemplo.com")
    m["To"] = destino
    m.set_content(f"Olá, {r['cliente']}!\n\nSeu certificado digital {r['tipo']} {quando} ({r['vencimento']}). "
                  f"Para renovar sem interrupção, agende aqui: {os.getenv('LINK_AGENDA', 'https://exemplo.com/agendar')}\n")

    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"  [e-mail simulado] {destino}: {m['Subject']}")
        return True, False
    porta = int(os.getenv("SMTP_PORT", "587"))
    try:
        smtp = smtplib.SMTP_SSL(host, porta, timeout=20) if porta == 465 else smtplib.SMTP(host, porta, timeout=20)
        with smtp as s:
            if porta != 465 and os.getenv("SMTP_TLS", "1") != "0":
                s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.send_message(m)
        return True, False
    except smtplib.SMTPRecipientsRefused as e:   # endereço inexistente: não adianta repetir
        print("  [e-mail] destinatário recusado:", e)
        return False, True
    except Exception as e:
        print("  [e-mail] falhou:", e)
        return False, False


def enviar_whatsapp(r, dias):
    """Ponto de extensão: Salesbot da Kommo / canal WhatsApp conectado. Aqui registramos nota no lead."""
    if not _telefone(r["telefone"]):
        return False
    if not (K["url"] and K["token"]):
        print(f"  [whatsapp simulado] {r['telefone']}: certificado {_quando(dias)}")
        return True
    return sync_kommo(r["id"], nota=f"WhatsApp de renovação: certificado {r['tipo']} {_quando(dias)} ({r['vencimento']})")


def regua(hoje=None):
    if not _regua_lock.acquire(blocking=False):
        print("régua: já em execução, ignorando esta chamada")
        return 0
    try:
        hoje = hoje or date.today()
        n = 0
        linhas = db().execute("""SELECT * FROM renovacoes
                                 WHERE status NOT IN ('emitido','pago','perdido')
                                 ORDER BY vencimento""").fetchall()
        for r in linhas:
            try:
                dias = (date.fromisoformat(r["vencimento"]) - hoje).days
            except (TypeError, ValueError):
                continue

            novo = r["status"]
            notificou = False

            # E-mail: aviso a partir de D30 e repetição a cada 7 dias, até concluir.
            tem_email = bool(_texto(r["email"]) and EMAIL_RE.match(r["email"]))
            if tem_email and dias <= EMAIL_INICIO:
                ultimo = r["ultimo_email"]
                desde = None
                if ultimo:
                    try:
                        desde = (hoje - date.fromisoformat(str(ultimo)[:10])).days
                    except ValueError:
                        desde = None
                if ultimo is None or desde is None or desde >= EMAIL_INTERVALO:
                    enviado, permanente = enviar_email(r, dias)
                    if enviado:
                        log(r["id"], "email", "email", "enviado",
                            f"aviso de vencimento ({_quando(dias)})")
                        db().execute("UPDATE renovacoes SET ultimo_email=? WHERE id=?",
                                     (hoje.isoformat(), r["id"]))
                        notificou = True
                        if novo == "a_vencer":
                            novo = "email_enviado"
                    elif not permanente:
                        log(r["id"], "email", "email", "falhou", "erro temporário no envio")

            # WhatsApp: mantém os limiares antigos (D15/D7/D1/D-3), sem repetir.
            if r["status"] in ("a_vencer", "email_enviado", "whatsapp_enviado") and dias >= -LIMITE_ATRASO:
                alvo = min((d for d in REGUA if dias <= d), default=None)
                if alvo is not None:
                    ultima = r["regua_ultima"] if r["regua_ultima"] is not None else 10 ** 6
                    if alvo < ultima:
                        tem_fone = bool(_telefone(r["telefone"])) and alvo <= 15
                        if not tem_email and not tem_fone:
                            log(r["id"], "regua", "sistema", "ignorado", "sem contato válido")
                            db().execute("UPDATE renovacoes SET regua_ultima=? WHERE id=?",
                                         (alvo, r["id"]))
                            continue
                        if tem_fone:
                            if enviar_whatsapp(r, dias):
                                log(r["id"], "whatsapp", "whatsapp", "enviado",
                                    f"régua D{alvo:+d} (faltam {dias} dias)")
                                db().execute("UPDATE renovacoes SET regua_ultima=? WHERE id=?",
                                             (alvo, r["id"]))
                                notificou = True
                                if STATUS.index("whatsapp_enviado") > STATUS.index(novo):
                                    novo = "whatsapp_enviado"
                            else:
                                log(r["id"], "whatsapp", "whatsapp", "falhou", "erro no envio")

            if notificou and novo != r["status"]:
                avancar(r["id"], novo, "regua")
            if notificou:
                n += 1
        print(f"régua: {n} renovações notificadas")
        return n
    finally:
        _regua_lock.release()


# ---------- 5. Métricas ----------
def carregar_cfg():   # configurações salvas no painel (sobrepõem as variáveis de ambiente)
    with _cfg_lock:
        novo = dict(_ENV_K)
        for r in db().execute("SELECT chave, valor FROM config"):
            chave, valor = r["chave"], r["valor"]
            if chave in ("url", "token", "pipeline") and valor is not None:
                novo[chave] = valor
            elif chave == "etapas" and valor:
                try:
                    etapas = json.loads(valor)
                    if isinstance(etapas, dict):
                        novo["etapas"] = etapas
                except json.JSONDecodeError:
                    print("[aviso] etapas inválidas no banco; mantendo as do ambiente")
        K.clear()
        K.update(novo)


def metricas(mes=None, hoje=None):
    if mes and not MES_RE.match(mes):
        raise ValueError("mês inválido: use AAAA-MM")
    hoje = (hoje or date.today()).isoformat()
    c = db()
    filtro_mes, params_mes = ("WHERE substr(vencimento,1,7)=?", (mes,)) if mes else ("", ())
    funil = {s: 0 for s in STATUS}
    funil.update({r[0]: r[1] for r in c.execute(f"SELECT status, COUNT(*) FROM renovacoes {filtro_mes} GROUP BY 1", params_mes)})

    # fora do mês: já venceu (data local, não UTC) ou já pagou
    base = "(vencimento<=? OR status='pago')"

    def taxa(g, filtro, p):
        return [_dict(r) for r in c.execute(f"""SELECT {g} AS grupo, COUNT(*) AS total,
            COALESCE(SUM(status='pago'),0) AS renovados,
            COALESCE(ROUND(100.0*SUM(status='pago')/COUNT(*),1),0) AS taxa,
            COALESCE(SUM(CASE WHEN status='pago' THEN valor END),0) AS receita
            FROM renovacoes WHERE {filtro} GROUP BY 1 ORDER BY 1""", p)]

    g = taxa("'x'", "substr(vencimento,1,7)=?" if mes else base, (mes,) if mes else (hoje,))
    return {"funil": funil,
            "total": g[0]["total"] if g else 0,
            "taxa_geral": g[0]["taxa"] if g else 0,
            "receita": g[0]["receita"] if g else 0,
            "por_mes": taxa("substr(vencimento,1,7)", base, (hoje,)),
            "por_tipo": taxa("tipo", base, (hoje,))}


# ---------- Login por certificado digital ----------
# O proxy (nginx) exige o certificado no TLS (prova de posse da chave privada) e repassa o PEM em
# X-Client-Cert. Aqui só conferimos se a impressão digital (SHA-256) é de um usuário ativo e dentro
# da validade. O cabeçalho só é aceito quando X-Proxy-Secret confere com PROXY_SECRET.
def ler_certificado(nome, dados, senha=""):
    if not _CRYPTO_OK:
        raise RuntimeError("instale cryptography: pip install cryptography")
    if nome.lower().endswith((".pfx", ".p12")):
        # só extrai a parte pública; a chave privada é descartada
        _chave, cert, _extras = pkcs12.load_key_and_certificates(dados, senha.encode() if senha else None)
        if cert is None:
            raise ValueError("PFX sem certificado")
        return cert
    if b"BEGIN CERTIFICATE" in dados:
        return x509.load_pem_x509_certificate(dados)
    return x509.load_der_x509_certificate(dados)


def cert_info(c):
    ini = getattr(c, "not_valid_before_utc", None) or c.not_valid_before
    fim = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
    return {"fp": c.fingerprint(hashes.SHA256()).hex(), "sujeito": c.subject.rfc4514_string(),
            "ini": ini.date().isoformat(), "fim": fim.date().isoformat()}


# ---------- Painel web + webhook ----------
HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>Renovação de certificados</title>
<script>try{var _t=localStorage.getItem("tema");if(_t==="light"||_t==="dark"){document.documentElement.dataset.theme=_t;document.documentElement.style.colorScheme=_t}}catch(e){}</script>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f6f8;--sf:#fff;--tx:#14263a;--mu:#5d6b7a;--ln:#dce2e8;--ac:#0f6f73;--ac2:#e3f1f1;--red:#b4432f;--orange:#d6a01e;--green:#2d7d4e;--blue:#6a8fc2;--teal:#2e9c8e;--purple:#8f6cc0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1820;--sf:#16212c;--tx:#e8eef4;--mu:#9aa9b8;--ln:#293846;--ac:#5cc4c7;--ac2:#13343a}}
:root[data-theme="dark"]{--bg:#0f1820;--sf:#16212c;--tx:#e8eef4;--mu:#9aa9b8;--ln:#293846;--ac:#5cc4c7;--ac2:#13343a}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 Figtree,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)}
.app{display:flex;min-height:100vh}
.side{width:236px;flex-shrink:0;background:var(--sf);border-right:1px solid var(--ln);display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:18px 16px;border-bottom:1px solid var(--ln)}
.logo{width:34px;height:34px;flex-shrink:0}
.brand-txt{font-size:11px;font-weight:800;letter-spacing:.03em;line-height:1.3;text-transform:uppercase}
.menu{display:flex;flex-direction:column;gap:2px;padding:12px 8px;flex:1;overflow-y:auto}
.mi{display:flex;align-items:center;gap:10px;width:100%;background:none;border:0;font:inherit;font-weight:600;color:var(--mu);padding:9px 12px;border-radius:8px;cursor:pointer;text-align:left}
.mi:hover{background:var(--ac2);color:var(--tx)}
.mi[aria-current="true"]{background:var(--ac2);color:var(--tx);font-weight:700}
.mi .ic{width:18px;text-align:center;font-size:15px;flex-shrink:0}
.side-foot{padding:12px;border-top:1px solid var(--ln);font-size:12px;color:var(--mu)}
.side-foot .mi{color:var(--tx);padding:6px 0}
.wrap{flex:1;min-width:0;display:flex;flex-direction:column}
.top{background:var(--sf);border-bottom:1px solid var(--ln);padding:14px 24px;display:flex;align-items:center;gap:16px}
.top h1{font-size:16px;font-weight:800;margin:0 auto 0 0}
.bell{width:38px;height:38px;border-radius:50%;border:1px solid var(--ln);background:var(--sf);font-size:17px;cursor:pointer;display:grid;place-items:center}
.bell:hover{background:var(--ac2)}
main{padding:24px;width:100%;max-width:1440px;margin:0 auto}
h2{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0 0 18px}
h3{font-size:16px;margin:0 0 12px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:12px;padding:16px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.card .lbl{font-size:12px;color:var(--mu);font-weight:600;line-height:1.3}
.card .num{font-size:28px;font-weight:800;line-height:1.1}
.card .ic{font-size:22px;opacity:.65}
.card.red .num{color:var(--red)}.card.orange .num{color:var(--orange)}.card.green .num{color:var(--green)}
.dash{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:20px;align-items:start}
.panel{background:var(--sf);border:1px solid var(--ln);border-radius:12px;padding:16px;min-width:0}
.panel-head{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.panel-head h3{margin:0 auto 0 0}
.cx{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:640px}th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--ln);white-space:nowrap;vertical-align:middle}
th{font-size:12px;color:var(--mu);font-weight:600}tr:last-child td{border-bottom:0}tbody tr{cursor:pointer}tbody tr:hover{background:var(--bg)}tr.sel td{background:var(--ac2)}
.progress{display:flex;flex-direction:column;gap:4px;min-width:130px}
.progress .plabel{font-size:12px;color:var(--mu)}
.progress .track{height:6px;border-radius:3px;background:var(--ln);overflow:hidden}
.progress .fill{display:block;height:100%;border-radius:3px}
.detail{background:var(--sf);border:1px solid var(--ln);border-radius:12px;padding:16px;position:sticky;top:80px}
.detail h3{margin:0 0 14px;font-size:15px}
.steps{list-style:none;margin:0 0 16px;padding:0}
.steps li{display:flex;gap:12px;position:relative;padding-bottom:18px}
.steps li:last-child{padding-bottom:0}
.steps li::before{content:"";position:absolute;left:11px;top:24px;bottom:0;width:2px;background:var(--ln)}
.steps li:last-child::before{display:none}
.steps .dot{width:24px;height:24px;border-radius:50%;background:var(--ln);color:var(--mu);display:grid;place-items:center;font-size:12px;font-weight:700;flex-shrink:0}
.steps li.done .dot{background:var(--ac);color:#fff}
.steps li.active .dot{background:var(--ac);color:#fff;box-shadow:0 0 0 4px var(--ac2)}
.steps .st{font-size:14px;font-weight:600}
.steps .sd{font-size:12px;color:var(--mu)}
.info{display:grid;gap:6px;font-size:13px;margin-bottom:12px}
.info div{display:flex;justify-content:space-between;gap:10px}
.info span{color:var(--mu)}
.hist{margin:0;padding:0;list-style:none;font-size:12px;max-height:180px;overflow-y:auto}
.hist li{padding:5px 0;border-bottom:1px dashed var(--ln)}
.hist li:last-child{border:0}
.hist time{color:var(--mu);margin-right:8px}
button.b{font:inherit;font-weight:600;background:var(--sf);color:var(--tx);border:1px solid var(--ln);border-radius:8px;padding:7px 12px;cursor:pointer}
button.b:hover{background:var(--bg)}
button.p{background:var(--ac);color:#fff;border-color:var(--ac)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) button.p{color:#0b1a1c}}
button.sm{padding:4px 9px;font-size:12px;border-radius:6px}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
select,input{font:inherit;color:var(--tx);background:var(--sf);border:1px solid var(--ln);border-radius:6px;padding:4px 6px}
.lk{background:none;border:0;color:var(--ac);font:inherit;font-weight:600;cursor:pointer;padding:0}
.cfg label{display:block;margin:10px 0;font-size:13px;color:var(--mu)}.cfg input,.cfg select{display:block;margin-top:4px;width:min(420px,100%)}.et{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:6px 14px;margin:8px 0 14px}.et label{margin:0}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.4);display:grid;place-items:center;z-index:20;padding:20px}
.modal-card{background:var(--sf);border-radius:14px;padding:20px;width:100%;max-width:440px;max-height:90vh;overflow-y:auto}
.modal-card label{display:block;margin:10px 0;font-size:13px;color:var(--mu)}
.modal-card input,.modal-card select{display:block;margin-top:4px;width:100%}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
#msg{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(18px + env(safe-area-inset-bottom,0px));background:var(--tx);color:var(--bg);padding:9px 16px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:50}#msg.on{opacity:1}
@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}.dash{grid-template-columns:1fr}.detail{position:static}}
@media(max-width:700px){.app{flex-direction:column}.side{width:100%;height:auto;position:static;flex-direction:row;align-items:center;border-right:0;border-bottom:1px solid var(--ln)}.brand{border:0;padding:12px}.menu{flex-direction:row;padding:8px;overflow-x:auto;flex:0 1 auto}.mi{width:auto;white-space:nowrap}.side-foot{display:none}.top{padding:12px 16px}main{padding:16px}h2{font-size:22px}.cards{grid-template-columns:1fr}.card{padding:12px}}
</style></head><body>
<div class="app">
<aside class="side">
 <div class="brand">
  <svg class="logo" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2l8 3v6c0 5-3.5 9.5-8 11-4.5-1.5-8-6-8-11V5l8-3z" fill="#0f6f73"/><path d="M8.5 12l2.5 2.5 4.5-4.5" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
  <div class="brand-txt">Sistema de<br>Renovação de<br>Certificados</div>
 </div>
 <nav class="menu">
  <button id="m1" class="mi" onclick="aba(1)" aria-current="true"><span class="ic">▦</span>Painel Geral</button>
  <button id="m2" class="mi" onclick="aba(2)" aria-current="false"><span class="ic">▤</span>Certificados Ativos</button>
  <button id="m3" class="mi" onclick="aba(3)" aria-current="false"><span class="ic">☰</span>Fila de Renovação</button>
  <button id="m4" class="mi" onclick="aba(4)" aria-current="false"><span class="ic">↺</span>Histórico</button>
  <button id="m5" class="mi" onclick="aba(5)" aria-current="false"><span class="ic">⚙</span>Configurações</button>
  <!--USUARIOS_NAV_INICIO--><button id="m6" class="mi" onclick="aba(6)" aria-current="false" hidden><span class="ic">👥</span>Usuários</button><!--USUARIOS_NAV_FIM-->
 </nav>
 <div class="side-foot"><span id="eu"></span></div>
</aside>
<div class="wrap">
<header class="top"><h1>Renovação de certificados</h1><button class="bell" onclick="regua()" title="Rodar régua agora" aria-label="Rodar régua agora">🔔</button></header>
<main>
<section id="s1">
 <h2>Processo de Renovação de Certificados</h2>
 <div class="cards">
  <div class="card red"><div><div class="lbl">Vencidos</div><div class="num" id="c-vencidos">0</div></div><div class="ic">⚠️</div></div>
  <div class="card orange"><div><div class="lbl">A Vencer - Aguardando em 30 dias</div><div class="num" id="c-vencer">0</div></div><div class="ic">⏳</div></div>
  <div class="card orange"><div><div class="lbl">Em Renovação</div><div class="num" id="c-renovacao">0</div></div><div class="ic">🔄</div></div>
  <div class="card green"><div><div class="lbl">Renovados este mês</div><div class="num" id="c-renovados">0</div></div><div class="ic">✅</div></div>
 </div>
 <div class="dash">
  <div class="panel">
   <div class="panel-head"><h3>Renovações pendentes</h3><button class="b p" onclick="novo()">Iniciar Renovação</button></div>
   <div class="cx"><table id="tab"></table></div>
  </div>
  <aside class="detail" id="detalhe">
   <h3 id="d-tit">Selecione uma renovação</h3>
   <ol class="steps" id="steps"></ol>
   <div class="info" id="d-info"></div>
   <h3 style="margin-top:14px">Histórico</h3>
   <ul class="hist" id="d-hist"></ul>
  </aside>
 </div>
</section>
<section id="s2" hidden>
 <h2>Certificados Ativos</h2>
 <div class="panel"><div class="cx"><table id="tab-ativos"></table></div></div>
</section>
<section id="s3" hidden>
 <h2>Fila de Renovação</h2>
 <div class="panel"><div class="cx"><table id="tab-fila"></table></div></div>
</section>
<section id="s4" hidden>
 <h2>Histórico</h2>
 <div class="panel"><div class="cx"><table id="tab-hist"></table></div></div>
</section>
<section id="s5" hidden class="cfg"><h2>Configurações</h2>
<h3>Aparência</h3>
<label>Tema<select id="tema" onchange="aplicarTema(this.value)"><option value="auto">Automático (sistema)</option><option value="light">Claro</option><option value="dark">Escuro</option></select></label>
<p style="font-size:13px;color:var(--mu)">A escolha fica salva neste navegador.</p>
<h3>Dados</h3>
<input type="file" id="arq" aria-label="Planilha" accept=".xlsx,.xls,.csv"> <button class="b" onclick="imp()">Importar planilha</button>
<h3>Integração com a Kommo</h3>
<label>URL da conta<input id="cu" placeholder="https://suaconta.kommo.com" inputmode="url"></label><label>Token de longa duração<input id="ct" type="password" autocomplete="off"></label><label>ID do funil<input id="cp" inputmode="numeric"></label>
<p style="font-size:13px;color:var(--mu)">ID da etapa no Kommo para cada status:</p><div class="et" id="ce"></div>
<button class="b p" onclick="salvar()">Salvar</button> <button class="b" onclick="testar()">Testar conexão</button> <button class="b" onclick="limparToken()">Limpar token</button>
<p style="font-size:13px;color:var(--mu)">Webhook para cadastrar na Kommo (mudança de etapa): <code id="wh"></code> (acrescente <code>?t=SEU_WEBHOOK_TOKEN</code> se definiu essa variável)</p></section>
<!--USUARIOS_SECAO_INICIO--><section id="s6" hidden class="cfg"><h2>Usuários e certificados</h2>
<div class="cx"><table id="usrs"></table></div>
<h3>Cadastrar certificado</h3>
<label>Nome<input id="un" autocomplete="off"></label>
<label>E-mail<input id="ue" type="email" autocomplete="off"></label>
<label>Perfil<select id="up"><option value="operador">Operador</option><option value="admin">Administrador</option></select></label>
<label>Certificado (.cer/.crt/.pem/.pfx)<input type="file" id="uf" accept=".cer,.crt,.pem,.pfx,.p12"></label>
<label>Senha do .pfx (se houver)<input id="us" type="password" autocomplete="off"></label>
<button class="b p" onclick="cadastrarCert()">Cadastrar</button>
<p style="font-size:13px;color:var(--mu)">O login por certificado exige <code>AUTH_CERT=1</code>, <code>PROXY_SECRET</code> e o proxy repassando <code>X-Client-Cert</code> (PEM urlencoded). A chave privada do .pfx é descartada; só a parte pública é guardada.</p></section><!--USUARIOS_SECAO_FIM-->
</main>
</div>
</div>
<div class="modal" id="modal" hidden>
 <div class="modal-card">
  <h3>Nova renovação</h3>
  <label>Cliente / Titular<input id="nv-cliente" autocomplete="off"></label>
  <label>Tipo<select id="nv-tipo"><option value="e-CNPJ A1">e-CNPJ A1</option><option value="e-CNPJ A3">e-CNPJ A3</option><option value="e-CPF A1">e-CPF A1</option><option value="e-CPF A3">e-CPF A3</option><option value="SSL">SSL</option><option value="SafeID">SafeID</option></select></label>
  <label>Vencimento<input id="nv-venc" type="date"></label>
  <label>Valor<input id="nv-valor" inputmode="decimal"></label>
  <label>E-mail<input id="nv-email" type="email" autocomplete="off"></label>
  <label>Telefone<input id="nv-tel" autocomplete="off"></label>
  <div class="modal-actions"><button class="b p" onclick="salvarNovo()">Salvar</button><button class="b" onclick="fecharModal()">Cancelar</button></div>
 </div>
</div>
<div id="msg" role="status" aria-live="polite"></div>
<script>
const ST=[["a_vencer","A vencer","#8f9aa7"],["email_enviado","E-mail enviado","#6a8fc2"],["whatsapp_enviado","WhatsApp enviado","#2e9c8e"],["agendado","Agendado","#d6a01e"],["emitido","Emitido","#8f6cc0"],["pago","Pago","#2d7d4e"],["perdido","Perdido","#b4432f"]];
const NM=Object.fromEntries(ST.map(s=>[s[0],s[1]]));
const PROG={a_vencer:{l:"Aguardando",p:20,c:"#8f9aa7"},email_enviado:{l:"Assinatura Pendente",p:40,c:"#6a8fc2"},whatsapp_enviado:{l:"Aprovação",p:60,c:"#2e9c8e"},agendado:{l:"Em Renovação",p:80,c:"#d6a01e"},emitido:{l:"Emitido",p:100,c:"#8f6cc0"},pago:{l:"Pago",p:100,c:"#2d7d4e"},perdido:{l:"Perdido",p:0,c:"#b4432f"}};
const el=id=>document.getElementById(id);
function temaSalvo(){try{return localStorage.getItem("tema")||"auto"}catch(_){return "auto"}}
function aplicarTema(v){const r=document.documentElement;if(v==="light"||v==="dark"){r.dataset.theme=v;r.style.colorScheme=v}else{delete r.dataset.theme;r.style.colorScheme=""}try{localStorage.setItem("tema",v)}catch(_){}const s=el("tema");if(s)s.value=v}
const j=async(u,o)=>{const r=await fetch(u,o);if(!r.ok){let m=r.status+" "+r.statusText;try{m=(await r.json()).erro||m}catch(_){}throw new Error(m)}return r.json()};
const post=(u,b)=>j(u,{method:"POST",headers:{"X-Requested-With":"fetch",...(b instanceof Blob?{}:{"Content-Type":"application/json"})},body:b instanceof Blob?b:JSON.stringify(b??{})});
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const br=s=>s?String(s).slice(8,10)+"/"+String(s).slice(5,7)+"/"+String(s).slice(0,4):"—";
const moeda=v=>v==null||v===""?"—":"R$ "+Number(v).toLocaleString("pt-BR",{maximumFractionDigits:2});
const hj=new Date(),pad=n=>String(n).padStart(2,"0"),hoje=hj.getFullYear()+"-"+pad(hj.getMonth()+1)+"-"+pad(hj.getDate()),mesAtual=hoje.slice(0,7);
const d30=new Date(hj.getTime()+30*864e5),dia30=d30.getFullYear()+"-"+pad(d30.getMonth()+1)+"-"+pad(d30.getDate());
let aberto=null,LISTA=[],EU={nome:"local",perfil:"admin"};
function toast(t){const m=el("msg");m.textContent=t;m.classList.add("on");clearTimeout(toast.h);toast.h=setTimeout(()=>m.classList.remove("on"),2800)}
function aba(n){if(n===6&&EU.perfil!=="admin")return;for(let i=1;i<=6;i++){const s=el("s"+i),m=el("m"+i);if(s)s.hidden=i!=n;if(m)m.setAttribute("aria-current",String(i==n))}if(n===5)cfgLoad();if(n===6)usuarios();if(n===1)renderDashboard();if(n===2)renderAtivos();if(n===3)renderFila();if(n===4)renderHistorico()}
function progress(s){const p=PROG[s]||{l:NM[s]||s,p:0,c:"#8f9aa7"};return `<span class="progress"><span class="plabel">${p.l}</span><span class="track"><span class="fill" style="width:${p.p}%;background:${p.c}"></span></span></span>`}
function acoes(x){const b=[];if(x.status==="a_vencer")b.push(`<button class="b p sm" onclick="event.stopPropagation();mudar(${x.id},'email_enviado')">Solicitar Renovação</button>`);else if(x.status==="email_enviado")b.push(`<button class="b sm" onclick="event.stopPropagation();mudar(${x.id},'whatsapp_enviado')">Enviar Documentos</button>`);else if(x.status==="whatsapp_enviado")b.push(`<button class="b sm" onclick="event.stopPropagation();mudar(${x.id},'agendado')">Notificar Usuário</button>`);else if(x.status==="agendado")b.push(`<button class="b sm" onclick="event.stopPropagation();mudar(${x.id},'emitido')">Emitir</button>`);else if(x.status==="emitido")b.push(`<button class="b sm" onclick="event.stopPropagation();mudar(${x.id},'pago')">Marcar como pago</button>`);b.push(`<button class="lk" onclick="event.stopPropagation();abrir(${x.id})">Histórico</button>`);return b.join(" ")}
function tabela(id,lista,opts={}){const cols=opts.cols||["Cliente","Tipo","Vencimento","Valor","Progresso","Ações"];el(id).innerHTML="<tr>"+cols.map(c=>`<th>${c}</th>`).join("")+"</tr>"+(lista.map(x=>`<tr class="${x.id==aberto?"sel":""}" onclick="abrir(${x.id})"><td>${esc(x.cliente)}</td><td>${esc(x.tipo)}</td><td>${br(x.vencimento)}</td><td>${moeda(x.valor)}</td>${opts.status?`<td>${NM[x.status]||x.status}</td>`:`<td>${progress(x.status)}</td>`}<td>${opts.acoes===false?"":acoes(x)}</td></tr>`).join("")||`<tr><td colspan="${cols.length}">Nenhum registro.</td></tr>`)}
function calcStats(L){const done=s=>["emitido","pago","perdido"].includes(s);return{vencidos:L.filter(x=>x.vencimento<hoje&&!done(x.status)).length,a_vencer:L.filter(x=>x.vencimento>=hoje&&x.vencimento<=dia30&&!done(x.status)).length,em_renovacao:L.filter(x=>["email_enviado","whatsapp_enviado","agendado"].includes(x.status)).length,renovados_mes:L.filter(x=>["emitido","pago"].includes(x.status)&&x.vencimento.startsWith(mesAtual)).length}}
function renderDashboard(){const L=LISTA||[];const st=calcStats(L);el("c-vencidos").textContent=st.vencidos;el("c-vencer").textContent=st.a_vencer;el("c-renovacao").textContent=st.em_renovacao;el("c-renovados").textContent=st.renovados_mes;const pend=L.filter(x=>!["emitido","pago","perdido"].includes(x.status)).sort((a,b)=>a.vencimento<b.vencimento?-1:1);tabela("tab",pend);if(aberto){const x=L.find(o=>o.id==aberto);if(x)detalhe(x);else{aberto=null;detalheVazio()}}else detalheVazio()}
function detalheVazio(){el("d-tit").textContent="Selecione uma renovação";el("steps").innerHTML="";el("d-info").innerHTML="<p style='color:var(--mu);font-size:13px'>Clique em uma linha para ver os detalhes.</p>";el("d-hist").innerHTML=""}
function detalhe(x){const passos=["Solicitação","Validação","Emissão","Instalação"];const idx={a_vencer:0,email_enviado:1,whatsapp_enviado:2,agendado:3,emitido:4,pago:4,perdido:4}[x.status]??0;el("d-tit").textContent=`Renovação ${x.tipo} - ${x.cliente}`;el("steps").innerHTML=passos.map((p,i)=>{const done=i<idx,active=i===idx&&!done;return `<li class="${done?"done":active?"active":""}"><span class="dot">${done?"✓":i+1}</span><span><span class="st">${p}</span><br><span class="sd">${done?"Concluído":active?"Em andamento":"Aguardando"}</span></span></li>`}).join("");el("d-info").innerHTML=`<div><span>Vencimento</span><b>${br(x.vencimento)}</b></div><div><span>Valor</span><b>${moeda(x.valor)}</b></div><div><span>Status</span><b>${NM[x.status]||x.status}</b></div>${x.email?`<div><span>E-mail</span><b>${esc(x.email)}</b></div>`:""}${x.telefone?`<div><span>Telefone</span><b>${esc(x.telefone)}</b></div>`:""}`;j("/api/historico?id="+x.id).then(e=>{el("d-hist").innerHTML=e.length?e.map(v=>`<li><time>${esc(String(v.ts).replace("T"," "))}</time>${esc(v.tipo)}${v.canal?" · "+esc(v.canal):""}${v.resultado?" · "+esc(v.resultado):""}</li>`).join(""):"<li>Sem eventos.</li>"}).catch(()=>{})}
async function abrir(id){aberto=id;renderDashboard()}
async function mudar(id,status){try{await post("/api/avancar",{id,status});toast("Status atualizado: "+NM[status]);await carregarTudo()}catch(e){toast("Falha ao atualizar: "+e.message)}}
async function regua(){try{const r=await post("/api/regua",{});toast(r.notificadas?`${r.notificadas} ${r.notificadas===1?"renovação notificada":"renovações notificadas"}.`:"Nenhum aviso novo: nada venceu ou o intervalo de 7 dias ainda não foi atingido.");await carregarTudo()}catch(e){toast("Falha na régua: "+e.message)}}
function renderAtivos(){const L=LISTA||[];const at=L.filter(x=>["emitido","pago"].includes(x.status)).sort((a,b)=>a.vencimento<b.vencimento?-1:1);tabela("tab-ativos",at,{status:true,cols:["Cliente","Tipo","Vencimento","Valor","Status","Ações"]})}
function renderFila(){const L=LISTA||[];const f=L.filter(x=>!["emitido","pago","perdido"].includes(x.status)).sort((a,b)=>a.vencimento<b.vencimento?-1:1);tabela("tab-fila",f)}
function renderHistorico(){const L=LISTA||[];const h=L.filter(x=>["emitido","pago","perdido"].includes(x.status)).sort((a,b)=>a.vencimento>b.vencimento?-1:1);tabela("tab-hist",h,{status:true,cols:["Cliente","Tipo","Vencimento","Valor","Status","Ações"]})}
async function carregarTudo(){try{LISTA=await j("/api/renovacoes");renderDashboard();renderAtivos();renderFila();renderHistorico()}catch(e){toast("Falha ao carregar: "+e.message)}}
async function cfgLoad(){
 try{
  el("tema").value=temaSalvo();
  const c=await j("/api/config");el("cu").value=c.url||"";el("cp").value=c.pipeline||"";el("ct").value="";
  el("ct").placeholder=c.token_definido?"(salvo; deixe vazio para manter)":"token de longa duração";
  el("ce").innerHTML=ST.map(s=>`<label>${s[1]}<input id="e_${s[0]}" inputmode="numeric" value="${esc((c.etapas||{})[s[0]]??"")}"></label>`).join("");
  el("wh").textContent=location.origin+"/webhook/kommo";
 }catch(e){toast("Falha ao carregar configurações: "+e.message)}
}
async function salvar(){
 const etapas={};ST.forEach(s=>{const v=el("e_"+s[0]).value.trim();if(v)etapas[s[0]]=Number(v)});
 const url=el("cu").value.trim();if(url&&!/^https?:\/\//i.test(url))return toast("A URL deve começar com https://");
 try{await post("/api/config",{url,token:el("ct").value,pipeline:el("cp").value.trim(),etapas});el("ct").value="";toast("Configurações salvas.")}catch(e){toast("Falha ao salvar: "+e.message)}
}
async function limparToken(){if(!confirm("Remover o token salvo?"))return;try{await post("/api/config",{limpar_token:true});el("ct").value="";toast("Token removido.")}catch(e){toast("Falha: "+e.message)}}
async function testar(){try{toast((await post("/api/kommo/testar",{})).msg)}catch(e){toast("Falha: "+e.message)}}
async function imp(){const f=el("arq").files[0];if(!f)return toast("Escolha uma planilha .xlsx ou .csv.");if(f.size>20971520)return toast("Arquivo maior que 20 MB.");try{const r=await post("/api/importar?nome="+encodeURIComponent(f.name),f);toast(r.erro||`${r.novas} novas, ${r.atualizadas} atualizadas, ${r.ignoradas} ignoradas.`);await carregarTudo()}catch(e){toast("Falha ao importar: "+e.message)}}
async function carregarEu(){try{EU=await j("/api/eu");el("eu")&&(el("eu").textContent=`Logado como ${EU.nome} (${EU.perfil})`);const adm=EU.perfil==="admin";const m6=el("m6"),s6=el("s6");if(m6)m6.hidden=!adm;if(!adm){if(m6)m6.remove();if(s6)s6.remove()}}catch(e){}}
async function usuarios(){if(EU.perfil!=="admin")return;try{const us=await j("/api/usuarios");el("usrs").innerHTML="<tr><th>Nome</th><th>E-mail</th><th>Perfil</th><th>Validade</th><th>Ativo</th><th></th></tr>"+(us.map(u=>`<tr><td>${esc(u.nome)}</td><td>${esc(u.email||"—")}</td><td>${esc(u.perfil)}</td><td>${br(u.validade)}</td><td><input type="checkbox" ${u.ativo?"checked":""} onchange="ativar(${u.id},this.checked)" aria-label="Usuário ativo"></td><td><button class="lk" onclick="remover(${u.id})">Remover</button></td></tr>`).join("")||"<tr><td colspan=6>Nenhum usuário cadastrado.</td></tr>")}catch(e){toast("Falha ao carregar usuários: "+e.message)}}
async function ativar(id,ativo){try{await post("/api/usuarios/ativo",{id,ativo});toast(ativo?"Usuário ativado.":"Usuário desativado.")}catch(e){toast("Falha: "+e.message);usuarios()}}
async function remover(id){if(!confirm("Remover este usuário?"))return;try{await post("/api/usuarios/remover",{id});toast("Usuário removido.");usuarios()}catch(e){toast("Falha: "+e.message)}}
async function cadastrarCert(){const f=el("uf").files[0];if(!f)return toast("Escolha o certificado.");const nome=el("un").value.trim();if(!nome)return toast("Informe o nome.");const q=new URLSearchParams({nome,email:el("ue").value.trim(),perfil:el("up").value,arquivo:f.name});const h={"X-Requested-With":"fetch"};const s=el("us").value;if(s)h["X-Pfx-Senha"]=s;try{const r=await j("/api/usuarios?"+q,{method:"POST",headers:h,body:f});toast("Certificado cadastrado. Válido até "+br(r.validade)+".");el("uf").value="";el("us").value="";usuarios()}catch(e){toast("Falha: "+e.message)}}
function novo(){el("modal").hidden=false;el("nv-cliente").value="";el("nv-tipo").value="e-CNPJ A1";el("nv-venc").value="";el("nv-valor").value="";el("nv-email").value="";el("nv-tel").value="";el("nv-cliente").focus()}
function fecharModal(){el("modal").hidden=true}
async function salvarNovo(){const cliente=el("nv-cliente").value.trim(),tipo=el("nv-tipo").value,vencimento=el("nv-venc").value;if(!cliente||!vencimento)return toast("Informe cliente e vencimento.");const valor=el("nv-valor").value.trim(),email=el("nv-email").value.trim(),telefone=el("nv-tel").value.trim();try{await post("/api/renovacoes",{cliente,tipo,vencimento,valor:valor||undefined,email,telefone});fecharModal();toast("Renovação criada.");await carregarTudo()}catch(e){toast("Falha: "+e.message)}}
aplicarTema(temaSalvo());carregarEu();carregarTudo();
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    server_version = "Renovacao/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, body, ctype="application/json", code=200, extra=None):
        if isinstance(body, bytes):
            b = body
        elif ctype == "application/json":
            b = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        else:
            b = str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype == "text/html":
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _auth(self):
        """Certificado digital (via proxy) e/ou senha básica de emergência. Webhook usa WEBHOOK_TOKEN."""
        self.user = None
        if urlparse(self.path).path.startswith("/webhook/"):
            return True
        senha = os.getenv("PAINEL_SENHA")
        cert_on = os.getenv("AUTH_CERT") == "1"
        if not senha and not cert_on:          # uso local: sem login
            self.user = {"nome": "local", "perfil": "admin", "validade": None}
            return True

        cabecalho = self.headers.get("Authorization", "")
        if senha and cabecalho.startswith("Basic "):
            try:
                usuario, _, informada = base64.b64decode(cabecalho[6:]).decode("utf-8").partition(":")
            except Exception:
                usuario = informada = ""
            if hmac.compare_digest(usuario.encode(), os.getenv("PAINEL_USER", "admin").encode()) \
               and hmac.compare_digest(informada.encode(), senha.encode()):
                self.user = {"nome": "Administrador", "perfil": "admin", "validade": None}
                return True

        if cert_on:
            pem = unquote(self.headers.get("X-Client-Cert", ""))
            segredo = os.getenv("PROXY_SECRET")
            if pem and segredo and hmac.compare_digest(self.headers.get("X-Proxy-Secret", "").encode(),
                                                       segredo.encode()):
                try:
                    i = cert_info(x509.load_pem_x509_certificate(pem.encode()))
                except Exception:
                    i = None
                if i:
                    hj = date.today().isoformat()
                    u = db().execute("SELECT * FROM usuarios WHERE fp=? AND ativo=1", (i["fp"],)).fetchone()
                    if u and i["ini"] <= hj <= i["fim"]:
                        self.user = {"id": u["id"], "nome": u["nome"], "perfil": u["perfil"],
                                     "validade": u["validade"]}
                        return True
                    return self._send({"erro": "certificado não autorizado, inativo ou vencido"}, code=403)

        extra = {"WWW-Authenticate": 'Basic realm="Renovacoes", charset="UTF-8"'} if senha else None
        self._send({"erro": "autenticação necessária"}, code=401, extra=extra)
        return False

    def _admin(self):
        return bool(self.user and self.user.get("perfil") == "admin")

    def _erro(self, e):
        print("erro HTTP:", e)
        try:
            self._send({"erro": "erro interno"}, code=500)
        except Exception:
            pass

    def do_GET(self):
        if not self._auth():
            return
        try:
            self._rota_get()
        except Exception as e:
            self._erro(e)

    def _rota_get(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        mes = q.get("mes", [None])[0]
        if mes and not MES_RE.match(mes):
            return self._send({"erro": "mês inválido: use AAAA-MM"}, code=400)
        if u.path == "/":
            html = HTML
            if not self._admin():
                html = re.sub(r"<!--USUARIOS_NAV_INICIO-->.*?<!--USUARIOS_NAV_FIM-->", "", html, flags=re.S)
                html = re.sub(r"<!--USUARIOS_SECAO_INICIO-->.*?<!--USUARIOS_SECAO_FIM-->", "", html, flags=re.S)
            return self._send(html, "text/html")
        if u.path == "/api/metricas":
            return self._send(metricas(mes))
        if u.path == "/api/renovacoes":
            sql = "SELECT * FROM renovacoes" + (" WHERE substr(vencimento,1,7)=?" if mes else "") + " ORDER BY vencimento"
            return self._send([_dict(r) for r in db().execute(sql, (mes,) if mes else ())])
        if u.path == "/api/historico":
            try:
                rid = int(q.get("id", [""])[0])
            except (TypeError, ValueError):
                return self._send({"erro": "id inválido"}, code=400)
            return self._send([_dict(r) for r in db().execute("SELECT * FROM eventos WHERE renovacao_id=? ORDER BY id", (rid,))])
        if u.path == "/api/eu":
            return self._send(self.user)
        if u.path == "/api/usuarios":
            if not self._admin():
                return self._send({"erro": "apenas administradores"}, code=403)
            return self._send([_dict(r) for r in db().execute(
                "SELECT id,nome,email,perfil,sujeito,validade,ativo FROM usuarios ORDER BY nome")])
        if u.path == "/api/config":
            return self._send({"url": K["url"], "pipeline": K["pipeline"], "etapas": K["etapas"],
                               "token_definido": bool(K["token"])})
        return self._send({"erro": "não encontrado"}, code=404)

    def do_POST(self):
        if not self._auth():
            return
        try:
            self._rota_post()
        except Exception as e:
            self._erro(e)

    def _rota_post(self):
        u = urlparse(self.path)
        p = u.path
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            tamanho = -1
        limite = MAX_CERT if p == "/api/usuarios" else MAX_UPLOAD
        if tamanho < 0 or tamanho > limite:
            return self._send({"erro": "corpo inválido ou grande demais"}, code=413)
        body = self.rfile.read(tamanho)

        if p == "/webhook/kommo":
            return self._webhook(body, u)
        if p == "/api/importar":
            return self._importar(body, u)
        # CSRF: navegador não consegue mandar este header cross-site sem preflight (que não atendemos)
        if self.headers.get("X-Requested-With") != "fetch":
            return self._send({"erro": "requisição inválida"}, code=403)
        if p.startswith("/api/usuarios") and not self._admin():
            return self._send({"erro": "apenas administradores"}, code=403)
        if p == "/api/usuarios":       # corpo = .cer/.crt/.pem (ou .pfx)
            return self._cadastrar_usuario(body, u)
        try:
            d = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._send({"erro": "JSON inválido"}, code=400)
        if not isinstance(d, dict):
            return self._send({"erro": "JSON inválido"}, code=400)

        if p == "/api/renovacoes":
            return self._criar_renovacao(d)
        if p == "/api/avancar":
            try:
                rid = int(d.get("id"))
            except (TypeError, ValueError):
                return self._send({"erro": "id inválido"}, code=400)
            if d.get("status") not in STATUS:
                return self._send({"erro": "status inválido"}, code=400)
            avancar(rid, d["status"], origem="painel")
            return self._send({"ok": True})
        if p == "/api/evento":       # tabulação manual: ligação, motivo de perda, observação...
            try:
                rid = int(d.get("id"))
            except (TypeError, ValueError):
                return self._send({"erro": "id inválido"}, code=400)
            log(rid, str(d.get("tipo") or "contato")[:40], str(d.get("canal") or "")[:40],
                str(d.get("resultado") or "")[:40], str(d.get("detalhe") or "")[:500])
            return self._send({"ok": True})
        if p == "/api/regua":
            return self._send({"notificadas": regua()})
        if p == "/api/usuarios/ativo":
            try:
                uid = int(d.get("id"))
            except (TypeError, ValueError):
                return self._send({"erro": "id inválido"}, code=400)
            if uid == self.user.get("id"):
                return self._send({"erro": "não é possível desativar o próprio usuário"}, code=400)
            db().execute("UPDATE usuarios SET ativo=? WHERE id=?", (1 if d.get("ativo") else 0, uid))
            return self._send({"ok": True})
        if p == "/api/usuarios/remover":
            try:
                uid = int(d.get("id"))
            except (TypeError, ValueError):
                return self._send({"erro": "id inválido"}, code=400)
            if uid == self.user.get("id"):
                return self._send({"erro": "não é possível remover o próprio usuário"}, code=400)
            db().execute("DELETE FROM usuarios WHERE id=?", (uid,))
            return self._send({"ok": True})
        if p == "/api/config":
            return self._config(d)
        if p == "/api/kommo/testar":
            if not (K["url"] and K["token"]):
                return self._send({"msg": "Sem URL/token: modo simulação"})
            try:
                kommo("GET", "/api/v4/account")
                return self._send({"msg": "Conexão OK"})
            except Exception as e:
                return self._send({"msg": f"Falhou: {e}"})
        return self._send({"erro": "não encontrado"}, code=404)

    def _criar_renovacao(self, d):
        cliente = str(d.get("cliente") or "").strip()[:200]
        tipo = str(d.get("tipo") or "").strip()[:80]
        venc = str(d.get("vencimento") or "").strip()
        if not cliente or not tipo or not venc:
            return self._send({"erro": "cliente, tipo e vencimento são obrigatórios"}, code=400)
        try:
            dt = datetime.strptime(venc, "%Y-%m-%d").date()
        except ValueError:
            try:
                dt = datetime.strptime(venc, "%d/%m/%Y").date()
            except ValueError:
                return self._send({"erro": "vencimento inválido"}, code=400)
        valor = d.get("valor")
        if valor not in (None, ""):
            try:
                valor = float(valor)
            except (TypeError, ValueError):
                return self._send({"erro": "valor inválido"}, code=400)
        else:
            valor = None
        email = str(d.get("email") or "").strip()[:200] or None
        telefone = str(d.get("telefone") or "").strip()[:40] or None
        try:
            cur = db().execute(
                "INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor,status) "
                "VALUES(?,?,?,?,?,?,'a_vencer')",
                (cliente, email, telefone, tipo, dt.isoformat(), valor))
        except sqlite3.IntegrityError:
            return self._send({"erro": "já existe uma renovação com esse cliente, tipo e vencimento"}, code=409)
        row = _dict(db().execute("SELECT * FROM renovacoes WHERE id=?", (cur.lastrowid,)).fetchone())
        log(row["id"], "criado", "painel")
        return self._send(row, code=201)

    def _config(self, d):
        with _cfg_lock:
            if "url" in d:
                url = str(d.get("url") or "").strip()
                if url and not url.lower().startswith(("http://", "https://")):
                    return self._send({"erro": "a URL deve começar com http:// ou https://"}, code=400)
                db().execute("INSERT OR REPLACE INTO config VALUES('url',?)", (url,))
            if "pipeline" in d:
                pipeline = str(d.get("pipeline") or "").strip()
                if pipeline and not pipeline.isdigit():
                    return self._send({"erro": "o ID do funil deve ser numérico"}, code=400)
                db().execute("INSERT OR REPLACE INTO config VALUES('pipeline',?)", (pipeline,))
            if d.get("token"):
                db().execute("INSERT OR REPLACE INTO config VALUES('token',?)", (str(d["token"]).strip(),))
            if d.get("limpar_token"):
                db().execute("DELETE FROM config WHERE chave='token'")
            if "etapas" in d:
                etapas = {}
                if isinstance(d.get("etapas"), dict):
                    for k, v in d["etapas"].items():
                        if k in STATUS and str(v).strip().isdigit():
                            etapas[k] = int(v)
                db().execute("INSERT OR REPLACE INTO config VALUES('etapas',?)", (json.dumps(etapas),))
        carregar_cfg()
        return self._send({"ok": True})

    def _cadastrar_usuario(self, body, u):
        q = parse_qs(u.query)

        def g(k):
            return (q.get(k, [""])[0] or "").strip()

        nome = g("nome")[:120]
        if not nome:
            return self._send({"erro": "informe o nome"}, code=400)
        email = g("email")[:200]
        perfil = g("perfil") if g("perfil") in ("admin", "operador") else "operador"
        try:
            i = cert_info(ler_certificado(g("arquivo"), body, self.headers.get("X-Pfx-Senha", "")))
        except RuntimeError as e:
            return self._send({"erro": str(e)}, code=500)
        except Exception:
            return self._send({"erro": "certificado inválido ou senha do .pfx incorreta"}, code=400)
        try:
            cur = db().execute("""INSERT INTO usuarios(nome,email,perfil,fp,sujeito,validade)
                                  VALUES(?,?,?,?,?,?)""",
                               (nome, email or None, perfil, i["fp"], i["sujeito"], i["fim"]))
        except sqlite3.IntegrityError:
            return self._send({"erro": "este certificado já está cadastrado"}, code=409)
        return self._send({"ok": True, "id": cur.lastrowid, "validade": i["fim"]})

    def _webhook(self, body, u):
        esperado = os.getenv("WEBHOOK_TOKEN")
        if esperado and not hmac.compare_digest(parse_qs(u.query).get("t", [""])[0].encode(), esperado.encode()):
            return self._send({"erro": "token inválido"}, code=403)
        ctype = self.headers.get("Content-Type", "")
        pares = []
        if "application/json" in ctype:
            try:
                dados = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._send({"erro": "JSON inválido"}, code=400)
            leads = ((dados.get("leads") or {}).get("status") or []) if isinstance(dados, dict) else []
            pares = [(str(x.get("id")), str(x.get("status_id"))) for x in leads if isinstance(x, dict)]
        else:
            f = parse_qs(body.decode("utf-8", "replace"))
            i = 0
            while f"leads[status][{i}][id]" in f:
                pares.append((f[f"leads[status][{i}][id]"][0], f.get(f"leads[status][{i}][status_id]", [""])[0]))
                i += 1
        inv = {str(_int_ou_none(v)): k for k, v in K["etapas"].items() if _int_ou_none(v) is not None}
        for lid, st in pares:
            st = st.strip()
            r = db().execute("SELECT id FROM renovacoes WHERE kommo_lead_id=?", (lid,)).fetchone()
            if r and st in inv:
                try:
                    avancar(r["id"], inv[st], origem="kommo")
                except ValueError:
                    pass
        return self._send({"ok": True})

    def _importar(self, body, u):
        nome = os.path.basename(parse_qs(u.query).get("nome", ["up.xlsx"])[0]) or "up.xlsx"
        if not nome.lower().endswith((".xlsx", ".xls", ".xlsm", ".csv")):
            return self._send({"erro": "use .xlsx, .xls ou .csv"}, code=400)
        tmp = os.path.join(tempfile.gettempdir(), f"renovacao_{os.getpid()}_{int(time.time() * 1000)}_{nome}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(body)
            return self._send(importar(tmp))
        except ValueError as e:
            return self._send({"erro": str(e)}, code=400)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def finish(self):
        try:
            super().finish()
        finally:
            c = getattr(_local, "conn", None)
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
                _local.conn = None

    def log_message(self, *a):
        pass


def servir():
    carregar_cfg()
    if not os.getenv("PAINEL_SENHA"):
        print("[aviso] PAINEL_SENHA não definida: painel sem login (restrito a 127.0.0.1).")
    if not os.getenv("WEBHOOK_TOKEN"):
        print("[aviso] WEBHOOK_TOKEN não definido: webhook aceita chamadas sem token.")
    if os.getenv("AUTH_CERT") == "1":
        if not _CRYPTO_OK:
            print("[aviso] AUTH_CERT=1 sem cryptography instalado: login por certificado não funciona.")
        if not os.getenv("PROXY_SECRET"):
            print("[aviso] AUTH_CERT=1 sem PROXY_SECRET: o cabeçalho X-Client-Cert não é confiável.")

    def ciclo():   # régua automática a cada 6h (e-mail repete a cada 7 dias até concluir)
        while True:
            try:
                regua()
            except Exception as e:
                print("erro na régua:", e)
            time.sleep(6 * 3600)

    threading.Thread(target=ciclo, daemon=True, name="regua").start()
    porta = int(os.getenv("PORT", "8000"))
    # com login por certificado o app deve ficar atrás do proxy: escuta só local
    publico = bool(os.getenv("PAINEL_SENHA")) and os.getenv("AUTH_CERT") != "1"
    host = os.getenv("HOST") or ("0.0.0.0" if publico else "127.0.0.1")
    srv = ThreadingHTTPServer((host, porta), H)
    srv.daemon_threads = True
    print(f"Sistema web em http://localhost:{porta} (escutando em {host}:{porta})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando")
    finally:
        srv.server_close()


# ---------- Demo / CLI ----------
def demo():
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise SystemExit("demo precisa de openpyxl: pip install openpyxl")
    rnd = random.Random(1)
    hoje = date.today()
    tipos = ["e-CPF A1", "e-CNPJ A1", "e-CPF A3", "e-CNPJ A3"]
    pd.DataFrame([{"Cliente": f"Cliente {i:02d} Ltda", "E-mail": f"cliente{i:02d}@exemplo.com",
                   "WhatsApp": f"79 9{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}", "Tipo": rnd.choice(tipos),
                   "Vencimento": hoje + timedelta(days=rnd.randint(-90, 75)), "Valor": rnd.choice([180, 220, 310])}
                  for i in range(1, 41)]).to_excel("certificados_exemplo.xlsx", index=False)
    importar("certificados_exemplo.xlsx")
    for r in db().execute("SELECT id FROM renovacoes WHERE vencimento<?", (hoje.isoformat(),)).fetchall():
        avancar(r["id"], rnd.choice(["pago", "pago", "pago", "perdido"]), origem="demo")   # histórico fictício
    regua()


def exportar(arquivo="relatorio.xlsx"):
    with pd.ExcelWriter(arquivo) as w:
        pd.read_sql_query("SELECT * FROM renovacoes ORDER BY vencimento", db()).to_excel(w, sheet_name="renovacoes", index=False)
        pd.read_sql_query("SELECT * FROM eventos ORDER BY id", db()).to_excel(w, sheet_name="historico", index=False)
    print(f"relatório gerado: {arquivo}")


def main():
    p = argparse.ArgumentParser(description="Renovação de certificados digitais")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sub.add_parser("serve")
    pi = sub.add_parser("importar")
    pi.add_argument("planilha")
    pr = sub.add_parser("regua")
    pr.add_argument("--hoje", help="AAAA-MM-DD (para simular outra data)")
    pm = sub.add_parser("metricas")
    pm.add_argument("--mes", help="AAAA-MM")
    pa = sub.add_parser("avancar")
    pa.add_argument("id", type=int)
    pa.add_argument("status", choices=STATUS)
    pe = sub.add_parser("exportar")
    pe.add_argument("arquivo", nargs="?", default="relatorio.xlsx")
    a = p.parse_args()

    carregar_cfg()
    if a.cmd == "demo":
        demo()
    elif a.cmd == "importar":
        importar(a.planilha)
    elif a.cmd == "regua":
        regua(date.fromisoformat(a.hoje) if a.hoje else None)
    elif a.cmd == "metricas":
        print(json.dumps(metricas(a.mes), ensure_ascii=False, indent=1))
    elif a.cmd == "avancar":
        avancar(a.id, a.status, "cli")
    elif a.cmd == "exportar":
        exportar(a.arquivo)
    else:
        servir()


if __name__ == "__main__":
    main()
