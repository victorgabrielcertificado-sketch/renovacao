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
"""
import argparse, base64, hmac, json, os, random, re, smtplib, sqlite3, tempfile, threading, time, unicodedata, urllib.error, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import pandas as pd

REGUA = [60, 30, 15, 7, 1, -3]   # dias para o vencimento em que cada aviso dispara (negativo = vencido)
LIMITE_ATRASO = 30               # não notifica quem venceu há mais de X dias
STATUS = ["a_vencer", "email_enviado", "whatsapp_enviado", "agendado", "emitido", "pago", "perdido"]
MAX_UPLOAD = 20 * 1024 * 1024    # 20 MB
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
  kommo_lead_id INTEGER, regua_ultima INTEGER,
  UNIQUE(cliente, tipo, vencimento));
CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS eventos(id INTEGER PRIMARY KEY, renovacao_id INTEGER, ts TEXT NOT NULL, tipo TEXT NOT NULL,
  canal TEXT, resultado TEXT, detalhe TEXT);
CREATE INDEX IF NOT EXISTS ix_renov_venc   ON renovacoes(vencimento);
CREATE INDEX IF NOT EXISTS ix_renov_status ON renovacoes(status);
CREATE INDEX IF NOT EXISTS ix_renov_lead   ON renovacoes(kommo_lead_id);
CREATE INDEX IF NOT EXISTS ix_eventos_rid  ON eventos(renovacao_id);
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
         "vencimento": ["vencimento", "validade", "data de vencimento", "data vencimento", "expira em"],
         "valor": ["valor", "preco", "valor da renovacao"]}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


_ALIAS = {k: {_norm(a) for a in v} for k, v in ALIAS.items()}


def _coluna(df, chave):
    for c in df.columns:
        if _norm(c) in _ALIAS[chave]:
            return c
    return None


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
    faltando = [k for k in ("cliente", "vencimento") if not col[k]]
    if faltando:
        raise ValueError("colunas obrigatórias ausentes: " + ", ".join(faltando)
                         + " (encontradas: " + ", ".join(map(str, df.columns)) + ")")

    venc_serie = df[col["vencimento"]]
    if pd.api.types.is_numeric_dtype(venc_serie):   # serial do Excel (ex.: 46000)
        venc = pd.to_datetime(venc_serie, unit="D", origin="1899-12-30", errors="coerce")
    else:
        venc = pd.to_datetime(venc_serie, dayfirst=True, errors="coerce")

    dados = pd.DataFrame({
        "cliente": df[col["cliente"]],
        "email": df[col["email"]] if col["email"] else None,
        "telefone": df[col["telefone"]] if col["telefone"] else None,
        "tipo": df[col["tipo"]] if col["tipo"] else "e-CNPJ",
        "vencimento": venc,
        "valor": pd.to_numeric(df[col["valor"]], errors="coerce") if col["valor"] else None,
    })

    c = db()
    novas = atualizadas = ignoradas = 0
    c.execute("BEGIN IMMEDIATE")          # importação tudo-ou-nada
    try:
        for r in dados.to_dict("records"):
            cliente = _texto(r["cliente"])
            dia = r["vencimento"]
            if not cliente or pd.isna(dia):
                ignoradas += 1
                continue
            tipo = _texto(r["tipo"]) or "e-CNPJ"
            email = _texto(r["email"])
            email = email if email and EMAIL_RE.match(email) else None
            fone = _telefone(r["telefone"])
            valor = None if pd.isna(r["valor"]) else float(r["valor"])
            iso = dia.strftime("%Y-%m-%d")
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
    print(f"{novas} novas, {atualizadas} atualizadas, {ignoradas} ignoradas ({len(dados)} linhas)")
    return {"novas": novas, "atualizadas": atualizadas, "ignoradas": ignoradas}


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
                                 WHERE status IN ('a_vencer','email_enviado','whatsapp_enviado')
                                 ORDER BY vencimento""").fetchall()
        for r in linhas:
            try:
                dias = (date.fromisoformat(r["vencimento"]) - hoje).days
            except (TypeError, ValueError):
                continue
            if dias < -LIMITE_ATRASO:
                continue
            # menor limiar ainda não ultrapassado: é o aviso que está "valendo" hoje
            alvo = min((d for d in REGUA if dias <= d), default=None)
            if alvo is None:
                continue
            ultima = r["regua_ultima"] if r["regua_ultima"] is not None else 10 ** 6
            if alvo >= ultima:
                continue

            rotulo = f"D{alvo:+d}"
            tem_email = bool(_texto(r["email"]) and EMAIL_RE.match(r["email"]))
            tem_fone = bool(_telefone(r["telefone"])) and alvo <= 15
            if not tem_email and not tem_fone:
                log(r["id"], "regua", "sistema", "ignorado", "sem contato válido")
                db().execute("UPDATE renovacoes SET regua_ultima=? WHERE id=?", (alvo, r["id"]))
                continue

            transitorio = False
            enviados = 0
            if tem_email:
                enviado, permanente = enviar_email(r, dias)
                if enviado:
                    log(r["id"], "email", "email", "enviado", f"régua {rotulo} (faltam {dias} dias)")
                    enviados += 1
                elif not permanente:
                    transitorio = True
                    log(r["id"], "email", "email", "falhou", "erro temporário no envio")

            novo = r["status"]
            if tem_fone:
                if enviar_whatsapp(r, dias):
                    log(r["id"], "whatsapp", "whatsapp", "enviado", f"régua {rotulo} (faltam {dias} dias)")
                    enviados += 1
                    novo = "whatsapp_enviado"
                else:
                    transitorio = True
                    log(r["id"], "whatsapp", "whatsapp", "falhou", "erro no envio")

            if transitorio:
                continue   # não marca regua_ultima: tenta de novo no próximo ciclo

            db().execute("UPDATE renovacoes SET regua_ultima=? WHERE id=?", (alvo, r["id"]))
            if enviados and STATUS.index(novo) > STATUS.index(r["status"]):
                avancar(r["id"], novo, "regua")
            if enviados:
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


# ---------- Painel web + webhook ----------
HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>Renovação de certificados</title>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f6f8;--sf:#fff;--tx:#14263a;--mu:#5d6b7a;--ln:#dce2e8;--ac:#0f6f73;--ac2:#e3f1f1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1820;--sf:#16212c;--tx:#e8eef4;--mu:#9aa9b8;--ln:#293846;--ac:#5cc4c7;--ac2:#13343a}}
:root[data-theme="dark"]{--bg:#0f1820;--sf:#16212c;--tx:#e8eef4;--mu:#9aa9b8;--ln:#293846;--ac:#5cc4c7;--ac2:#13343a}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 Figtree,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
header{background:var(--sf);border-bottom:1px solid var(--ln);padding:14px 20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}header h1{font-size:16px;font-weight:800;margin:0 auto 0 0}
nav button{background:none;border:0;font:inherit;font-weight:600;color:var(--mu);padding:6px 2px;margin-left:16px;cursor:pointer;border-bottom:2px solid transparent}nav button[aria-current="true"]{color:var(--tx);border-color:var(--ac)}
main{max-width:1080px;margin:0 auto;padding:20px}.topo{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}.topo h2{font-size:34px;font-weight:800;letter-spacing:-.02em;margin:0 auto 0 0}
button.b{font:inherit;font-weight:600;background:var(--sf);color:var(--tx);border:1px solid var(--ln);border-radius:8px;padding:7px 12px;cursor:pointer}button.p{background:var(--ac);color:#fff;border-color:var(--ac)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) button.p{color:#0b1a1c}}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
.nums{display:flex;gap:28px;flex-wrap:wrap;margin:10px 0 14px}.nums div{font-size:13px;color:var(--mu)}.nums b{display:block;font-size:26px;color:var(--tx);font-weight:800;line-height:1.1}
.fita{display:flex;height:26px;border-radius:6px;overflow:hidden;background:var(--ln)}.fita i{display:block;min-width:3px}
.leg{display:flex;gap:6px 16px;flex-wrap:wrap;margin:10px 0 20px;font-size:13px}.leg span{display:flex;align-items:center;gap:6px}.leg u{width:10px;height:10px;border-radius:3px;display:inline-block}
.cx{background:var(--sf);border:1px solid var(--ln);border-radius:10px;overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:640px}th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--ln);white-space:nowrap}
th{font-size:12px;color:var(--mu);font-weight:600}tr:last-child td{border-bottom:0}tr.sel td{background:var(--ac2)}
select,input{font:inherit;color:var(--tx);background:var(--sf);border:1px solid var(--ln);border-radius:6px;padding:4px 6px}.lk{background:none;border:0;color:var(--ac);font:inherit;font-weight:600;cursor:pointer;padding:0}
h3{font-size:15px;margin:24px 0 8px}.hist{margin:0;padding:12px 16px;list-style:none;font-size:13px}.hist li{padding:4px 0;border-bottom:1px dashed var(--ln)}.hist li:last-child{border:0}.hist time{color:var(--mu);margin-right:10px}
.bar{display:inline-block;height:8px;border-radius:4px;background:var(--ac);vertical-align:middle;margin-right:8px}
.cfg label{display:block;margin:10px 0;font-size:13px;color:var(--mu)}.cfg input{display:block;margin-top:4px;width:min(420px,100%)}.et{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:6px 14px;margin:8px 0 14px}.et label{margin:0}
#msg{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(18px + env(safe-area-inset-bottom,0px));background:var(--tx);color:var(--bg);padding:9px 16px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:5}#msg.on{opacity:1}
</style></head><body>
<header><h1>Renovação de certificados</h1><nav><button id="n1" onclick="aba(1)" aria-current="true">Renovações do mês</button><button id="n2" onclick="aba(2)" aria-current="false">Configurações</button></nav></header>
<main><section id="s1">
<div class="topo"><h2 id="tit"></h2><button class="b" onclick="nav(-1)" aria-label="Mês anterior">◀</button><button class="b" onclick="nav(1)" aria-label="Próximo mês">▶</button><button class="b p" onclick="regua()">Rodar régua agora</button></div>
<div class="nums" id="nums"></div><div class="fita" id="fita" aria-hidden="true"></div><div class="leg" id="leg"></div>
<div class="cx"><table id="tab"></table></div>
<h3>Histórico do lead</h3><div class="cx"><ul class="hist" id="hst"><li>Clique em “Histórico” em uma linha da tabela.</li></ul></div>
<h3>Taxa de renovação por mês de vencimento</h3><div class="cx"><table id="meses"></table></div></section>
<section id="s2" hidden class="cfg"><h3>Dados</h3>
<input type="file" id="arq" aria-label="Planilha" accept=".xlsx,.xls,.csv"> <button class="b" onclick="imp()">Importar planilha</button>
<h3>Integração com a Kommo</h3>
<label>URL da conta<input id="cu" placeholder="https://suaconta.kommo.com" inputmode="url"></label><label>Token de longa duração<input id="ct" type="password" autocomplete="off"></label><label>ID do funil<input id="cp" inputmode="numeric"></label>
<p style="font-size:13px;color:var(--mu)">ID da etapa no Kommo para cada status:</p><div class="et" id="ce"></div>
<button class="b p" onclick="salvar()">Salvar</button> <button class="b" onclick="testar()">Testar conexão</button> <button class="b" onclick="limparToken()">Limpar token</button>
<p style="font-size:13px;color:var(--mu)">Webhook para cadastrar na Kommo (mudança de etapa): <code id="wh"></code> (acrescente <code>?t=SEU_WEBHOOK_TOKEN</code> se definiu essa variável)</p></section></main>
<div id="msg" role="status" aria-live="polite"></div>
<script>
const ST=[["a_vencer","A vencer","#8f9aa7"],["email_enviado","E-mail enviado","#6a8fc2"],["whatsapp_enviado","WhatsApp enviado","#2e9c8e"],["agendado","Agendado","#d6a01e"],["emitido","Emitido","#8f6cc0"],["pago","Pago","#2d7d4e"],["perdido","Perdido","#b4432f"]];
const NM=Object.fromEntries(ST.map(s=>[s[0],s[1]]));
const MN=["janeiro","fevereiro","março","abril","maio","junho","julho","agosto","setembro","outubro","novembro","dezembro"];
const mesNome=i=>MN[i][0].toUpperCase()+MN[i].slice(1);
const el=id=>document.getElementById(id);
const j=async(u,o)=>{const r=await fetch(u,o);if(!r.ok){let m=r.status+" "+r.statusText;try{m=(await r.json()).erro||m}catch(_){}throw new Error(m)}return r.json()};
const post=(u,b)=>j(u,{method:"POST",headers:{"X-Requested-With":"fetch",...(b instanceof Blob?{}:{"Content-Type":"application/json"})},body:b instanceof Blob?b:JSON.stringify(b??{})});
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const br=s=>s?String(s).slice(8,10)+"/"+String(s).slice(5,7)+"/"+String(s).slice(0,4):"—";
const moeda=v=>v==null||v===""?"—":"R$ "+Number(v).toLocaleString("pt-BR",{maximumFractionDigits:2});
const hj=new Date();let mes=hj.getFullYear()+"-"+String(hj.getMonth()+1).padStart(2,"0"),aberto=null,LISTA=[];
function toast(t){const m=el("msg");m.textContent=t;m.classList.add("on");clearTimeout(toast.h);toast.h=setTimeout(()=>m.classList.remove("on"),2800)}
function aba(n){el("s1").hidden=n!=1;el("s2").hidden=n!=2;el("n1").setAttribute("aria-current",String(n==1));el("n2").setAttribute("aria-current",String(n==2));if(n==2)cfgLoad()}
function nav(d){const[y,m]=mes.split("-").map(Number),t=new Date(y,m-1+d,1);mes=t.getFullYear()+"-"+String(t.getMonth()+1).padStart(2,"0");aberto=null;render()}
async function render(){
 try{
  const q="?mes="+encodeURIComponent(mes),[m,L]=await Promise.all([j("/api/metricas"+q),j("/api/renovacoes"+q)]);LISTA=L;
  const[y,mm]=mes.split("-");el("tit").textContent=mesNome(Number(mm)-1)+" de "+y;
  el("nums").innerHTML=`<div><b>${m.total}</b>vencem no mês</div><div><b>${m.taxa_geral}%</b>renovados</div><div><b>${moeda(m.receita)}</b>recebidos</div>`;
  el("fita").innerHTML=ST.filter(s=>m.funil[s[0]]).map(s=>`<i style="flex:${m.funil[s[0]]};background:${s[2]}" title="${s[1]}: ${m.funil[s[0]]}"></i>`).join("");
  el("leg").innerHTML=ST.map(s=>`<span><u style="background:${s[2]}"></u>${s[1]} ${m.funil[s[0]]}</span>`).join("");
  el("tab").innerHTML="<tr><th>Cliente</th><th>Tipo</th><th>Vencimento</th><th>Valor</th><th>Situação</th><th></th></tr>"+
   (L.map(x=>`<tr class="${x.id==aberto?"sel":""}"><td>${esc(x.cliente)}</td><td>${esc(x.tipo)}</td><td>${br(x.vencimento)}</td><td>${moeda(x.valor)}</td><td><select aria-label="Situação de ${esc(x.cliente)}" onchange="mudar(${x.id},this.value)">${ST.map(s=>`<option value="${s[0]}" ${s[0]==x.status?"selected":""}>${s[1]}</option>`).join("")}</select></td><td><button class="lk" onclick="abrir(${x.id})">Histórico</button></td></tr>`).join("")||"<tr><td colspan=6>Nenhum certificado vence neste mês.</td></tr>");
  el("meses").innerHTML="<tr><th>Mês</th><th>Base</th><th>Renovados</th><th>Taxa</th></tr>"+m.por_mes.filter(x=>x.grupo).map(x=>{
   const[yy,mn]=String(x.grupo).split("-");
   return `<tr><td>${mesNome(Number(mn)-1)} de ${yy}</td><td>${x.total}</td><td>${x.renovados}</td><td><span class="bar" style="width:${Math.min(100,Number(x.taxa))*0.9}px"></span>${x.taxa}%</td></tr>`;
  }).join("");
  if(aberto)await hist(aberto);
 }catch(e){toast("Falha ao carregar: "+e.message)}
}
async function hist(id){
 try{
  const e=await j("/api/historico?id="+id),x=LISTA.find(o=>o.id==id);
  el("hst").innerHTML=(x?`<li><b>${esc(x.cliente)}</b> · ${esc(x.tipo)}</li>`:"")+
   (e.length?e.map(v=>`<li><time>${esc(String(v.ts).replace("T"," "))}</time>${esc(v.tipo)}${v.canal?" · "+esc(v.canal):""}${v.resultado?" · "+esc(v.resultado):""}${v.detalhe?" · "+esc(v.detalhe):""}</li>`).join(""):"<li>Sem eventos.</li>");
 }catch(e){toast("Falha no histórico: "+e.message)}
}
async function abrir(id){aberto=id;await render()}
async function mudar(id,status){try{await post("/api/avancar",{id,status});toast("Status atualizado: "+NM[status]);await render()}catch(e){toast("Falha ao atualizar: "+e.message)}}
async function regua(){try{const r=await post("/api/regua",{});toast(r.notificadas?`${r.notificadas} ${r.notificadas===1?"renovação notificada":"renovações notificadas"}.`:"Nenhum aviso novo: a régua não repete envios.");await render()}catch(e){toast("Falha na régua: "+e.message)}}
async function cfgLoad(){
 try{
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
async function imp(){const f=el("arq").files[0];if(!f)return toast("Escolha uma planilha .xlsx ou .csv.");if(f.size>20971520)return toast("Arquivo maior que 20 MB.");try{const r=await post("/api/importar?nome="+encodeURIComponent(f.name),f);toast(r.erro||`${r.novas} novas, ${r.atualizadas} atualizadas, ${r.ignoradas} ignoradas.`);await render()}catch(e){toast("Falha ao importar: "+e.message)}}
aba(1);render();
</script></body></html>"""


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

    def _auth(self):   # login básico do painel; o webhook usa WEBHOOK_TOKEN
        senha = os.getenv("PAINEL_SENHA")
        if not senha or urlparse(self.path).path.startswith("/webhook/"):
            return True
        cabecalho = self.headers.get("Authorization", "")
        if cabecalho.startswith("Basic "):
            try:
                usuario, _, informada = base64.b64decode(cabecalho[6:]).decode("utf-8").partition(":")
            except Exception:
                usuario = informada = ""
            if hmac.compare_digest(usuario.encode(), os.getenv("PAINEL_USER", "admin").encode()) \
               and hmac.compare_digest(informada.encode(), senha.encode()):
                return True
        self._send({"erro": "autenticação necessária"}, code=401,
                   extra={"WWW-Authenticate": 'Basic realm="Renovacoes", charset="UTF-8"'})
        return False

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
            return self._send(HTML, "text/html")
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
        if tamanho < 0 or tamanho > MAX_UPLOAD:
            return self._send({"erro": "corpo inválido ou grande demais"}, code=413)
        body = self.rfile.read(tamanho)

        if p == "/webhook/kommo":
            return self._webhook(body, u)
        if p == "/api/importar":
            return self._importar(body, u)
        # CSRF: navegador não consegue mandar este header cross-site sem preflight (que não atendemos)
        if self.headers.get("X-Requested-With") != "fetch":
            return self._send({"erro": "requisição inválida"}, code=403)
        try:
            d = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._send({"erro": "JSON inválido"}, code=400)
        if not isinstance(d, dict):
            return self._send({"erro": "JSON inválido"}, code=400)

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

    def ciclo():   # régua automática a cada 6h (idempotente: não repete avisos)
        while True:
            try:
                regua()
            except Exception as e:
                print("erro na régua:", e)
            time.sleep(6 * 3600)

    threading.Thread(target=ciclo, daemon=True, name="regua").start()
    porta = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("PAINEL_SENHA") else "127.0.0.1")
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
