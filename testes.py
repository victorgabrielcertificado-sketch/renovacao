#!/usr/bin/env python3
"""Testes do protótipo de renovação (rodar: python testes.py -v)."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

BASE = Path(__file__).resolve().parent
os.environ["DB"] = str(Path(tempfile.mkdtemp(prefix="renov_teste_")) / "teste.db")
os.environ.pop("PAINEL_SENHA", None)
os.environ.pop("WEBHOOK_TOKEN", None)
os.environ.pop("SMTP_HOST", None)
os.environ.pop("AUTH_CERT", None)
os.environ.pop("PROXY_SECRET", None)
sys.path.insert(0, str(BASE))

import renovacao as R  # noqa: E402


def limpar():
    R.db().execute("DELETE FROM renovacoes")
    R.db().execute("DELETE FROM eventos")
    R.db().execute("DELETE FROM usuarios")


def gerar_cert(cn="Ana", dias_ini=-1, dias_fim=365):
    """Certificado autoassinado para os testes de login por certificado."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    agora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome)
            .public_key(chave.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(agora + timedelta(days=dias_ini))
            .not_valid_after(agora + timedelta(days=dias_fim))
            .sign(chave, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            cert.fingerprint(hashes.SHA256()).hex(),
            cert.public_bytes(serialization.Encoding.DER))


def gerar_pfx(senha="senha"):
    """PFX protegido por senha, para testar o upload de .pfx."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PFX")])
    agora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome)
            .public_key(chave.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(agora - timedelta(days=1))
            .not_valid_after(agora + timedelta(days=365))
            .sign(chave, hashes.SHA256()))
    dados = pkcs12.serialize_key_and_certificates(b"pfx", chave, cert, None,
                                                 BestAvailableEncryption(senha.encode()))
    return dados, cert.fingerprint(hashes.SHA256()).hex()


def usuario(nome, fp, perfil="admin", ativo=1, validade="2099-01-01"):
    R.db().execute("""INSERT INTO usuarios(nome,email,perfil,fp,sujeito,validade,ativo)
                      VALUES(?,?,?,?,?,?,?)""",
                   (nome, "x@y.com", perfil, fp, "CN=" + nome, validade, ativo))


def lead(cliente, vencimento, email="a@b.com", telefone="79 99999-1111", status="a_vencer"):
    cur = R.db().execute(
        """INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor,status)
           VALUES(?,?,?,?,?,?,?)""",
        (cliente, email, telefone, "e-CNPJ A1", vencimento.isoformat(), 200, status))
    return cur.lastrowid


def eventos(rid, tipo):
    return R.db().execute("SELECT COUNT(*) FROM eventos WHERE renovacao_id=? AND tipo=?",
                          (rid, tipo)).fetchone()[0]


class Importacao(unittest.TestCase):
    def setUp(self):
        limpar()

    def _arquivo(self, texto, nome="planilha.csv", encoding="utf-8"):
        p = Path(tempfile.mkdtemp(prefix="renov_imp_")) / nome
        p.write_text(texto, encoding=encoding)
        return str(p)

    def test_csv_ponto_virgula_com_bom(self):
        p = self._arquivo("\ufeffCliente;Vencimento;Valor;E-mail;WhatsApp\n"
                          "ACME;31/12/2026;200;a@b.com;79 99999-1111\n")
        r = R.importar(p)
        self.assertEqual(r["novas"], 1)
        row = R.db().execute("SELECT * FROM renovacoes").fetchone()
        self.assertEqual(row["vencimento"], "2026-12-31")
        self.assertEqual(row["valor"], 200)
        self.assertEqual(row["email"], "a@b.com")

    def test_extensao_maiuscula(self):
        p = self._arquivo("Cliente,Vencimento\nACME,31/12/2026\n", nome="PLANILHA.CSV")
        self.assertEqual(R.importar(p)["novas"], 1)

    def test_serial_do_excel(self):
        p = self._arquivo("Cliente,Vencimento\nACME,46000\n")
        R.importar(p)
        self.assertEqual(R.db().execute("SELECT vencimento FROM renovacoes").fetchone()[0], "2025-12-09")

    def test_contato_invalido_vira_nulo(self):
        p = self._arquivo("Cliente,Vencimento,E-mail,WhatsApp\nACME,31/12/2026,nao-e-email,123\n")
        R.importar(p)
        row = R.db().execute("SELECT email, telefone FROM renovacoes").fetchone()
        self.assertIsNone(row["email"])
        self.assertIsNone(row["telefone"])

    def test_linha_sem_cliente_ou_data_e_ignorada(self):
        p = self._arquivo("Cliente,Vencimento\n,31/12/2026\nACME,data-invalida\nBETA,31/12/2026\n")
        r = R.importar(p)
        self.assertEqual((r["novas"], r["ignoradas"]), (1, 2))

    def test_colunas_obrigatorias(self):
        p = self._arquivo("Foo,Bar\n1,2\n")
        with self.assertRaises(ValueError):
            R.importar(p)

    def test_regras_de_validade_por_tipo(self):
        self.assertEqual(R.DURACOES, {"a1": [12], "a3": [12, 24, 36], "safeid": [3, 12]})
        self.assertEqual(R._tipo_regra("e-CNPJ A1"), "a1")
        self.assertEqual(R._tipo_regra("e-CPF A3"), "a3")
        self.assertEqual(R._tipo_regra("SafeID"), "safeid")
        self.assertEqual(R._duracao_meses("2 anos", "e-CNPJ A3"), 24)
        self.assertEqual(R._duracao_meses("3 meses", "SafeID"), 3)
        self.assertEqual(R._duracao_meses(3, "e-CNPJ A3"), 36)
        self.assertEqual(R._duracao_meses(3, "SafeID"), 3)
        self.assertEqual(R._duracao_meses(None, "e-CNPJ A3 2 anos"), 24)
        self.assertFalse(R._duracao_valida("e-CNPJ A1", 24))
        self.assertTrue(R._duracao_valida("e-CNPJ A3", 24))
        self.assertTrue(R._duracao_valida("SafeID", 3))
        self.assertEqual(R._add_meses(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(R._add_meses(date(2024, 2, 29), 12), date(2025, 2, 28))

    def test_calcula_vencimento_por_emissao_e_duracao(self):
        p = self._arquivo("Cliente;Tipo;Emissão;Duração\n"
                          "ACME;e-CNPJ A1;10/01/2026;1 ano\n"
                          "BETA;e-CPF A3;10/01/2026;2 anos\n"
                          "GAMA;SafeID;10/01/2026;3 meses\n")
        r = R.importar(p)
        self.assertEqual(r["novas"], 3)
        self.assertEqual(r["avisos"], 0)
        rows = R.db().execute("SELECT cliente, vencimento FROM renovacoes ORDER BY cliente").fetchall()
        self.assertEqual([(x["cliente"], x["vencimento"]) for x in rows],
                         [("ACME", "2027-01-10"), ("BETA", "2028-01-10"),
                          ("GAMA", "2026-04-10")])

    def test_validade_como_duracao(self):
        p = self._arquivo("Cliente;Tipo;Emissão;Validade\nACME;e-CNPJ A3;10/01/2026;3 anos\n")
        r = R.importar(p)
        self.assertEqual(r["novas"], 1)
        self.assertEqual(R.db().execute("SELECT vencimento FROM renovacoes").fetchone()[0], "2029-01-10")

    def test_avisa_vencimento_incompativel(self):
        p = self._arquivo("Cliente;Tipo;Emissão;Vencimento\nACME;e-CNPJ A1;10/01/2026;10/01/2028\n")
        r = R.importar(p)
        self.assertEqual(r["novas"], 1)
        self.assertEqual(r["avisos"], 1)
        self.assertIn("não confere", r["detalhes"][0])

    def test_reimportar_atualiza_e_preserva_status(self):
        p1 = self._arquivo("Cliente,Vencimento,E-mail\nACME,31/12/2026,velho@b.com\n")
        R.importar(p1)
        rid = R.db().execute("SELECT id FROM renovacoes").fetchone()[0]
        R.avancar(rid, "pago", origem="demo")
        p2 = self._arquivo("Cliente,Vencimento,E-mail\nACME,31/12/2026,novo@b.com\n")
        r = R.importar(p2)
        self.assertEqual((r["novas"], r["atualizadas"]), (0, 1))
        row = R.db().execute("SELECT email, status FROM renovacoes").fetchone()
        self.assertEqual(row["email"], "novo@b.com")
        self.assertEqual(row["status"], "pago")


class Regua(unittest.TestCase):
    HOJE = date(2026, 1, 10)

    def setUp(self):
        limpar()
        R.K["url"] = None
        R.K["token"] = None
        R.K["etapas"] = {}

    def test_limiares_e_deduplicacao(self):
        casos = {
            "D60": (self.HOJE + timedelta(days=60), 1),
            "D59": (self.HOJE + timedelta(days=59), 1),
            "D30": (self.HOJE + timedelta(days=30), 1),
            "D20": (self.HOJE + timedelta(days=20), 1),
            "D7": (self.HOJE + timedelta(days=7), 1),
            "D0": (self.HOJE, 1),
            "D-1": (self.HOJE - timedelta(days=1), 1),
            "D-31": (self.HOJE - timedelta(days=31), 0),
            "D90": (self.HOJE + timedelta(days=90), 0),
        }
        ids = {nome: lead(nome, venc) for nome, (venc, _) in casos.items()}
        self.assertEqual(R.regua(self.HOJE), 7)
        for nome, (_, esperado) in casos.items():
            self.assertEqual(eventos(ids[nome], "email"), esperado, nome)
        self.assertEqual(R.regua(self.HOJE), 0)   # não repete

    def test_sem_contato_nao_quebra_e_marca_regua(self):
        rid = lead("SEM CONTATO", self.HOJE + timedelta(days=10), email=None, telefone=None)
        self.assertEqual(R.regua(self.HOJE), 0)
        row = R.db().execute("SELECT regua_ultima FROM renovacoes WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["regua_ultima"], 15)
        self.assertEqual(eventos(rid, "email"), 0)
        self.assertEqual(eventos(rid, "regua"), 1)

    def test_email_invalido_usa_whatsapp(self):
        rid = lead("EMAIL RUIM", self.HOJE + timedelta(days=10), email="invalido")
        R.regua(self.HOJE)
        self.assertEqual(eventos(rid, "email"), 0)
        self.assertEqual(eventos(rid, "whatsapp"), 1)
        self.assertEqual(R.db().execute("SELECT status FROM renovacoes WHERE id=?", (rid,)).fetchone()[0],
                         "whatsapp_enviado")

    def test_vencido_nao_usa_texto_negativo(self):
        self.assertEqual(R._quando(-1), "venceu há 1 dia")
        self.assertEqual(R._quando(-3), "venceu há 3 dias")
        self.assertEqual(R._quando(0), "vence hoje")
        self.assertEqual(R._quando(1), "vence em 1 dia")
        self.assertEqual(R._quando(2), "vence em 2 dias")


class Metricas(unittest.TestCase):
    def setUp(self):
        limpar()

    def test_total_taxa_e_receita(self):
        lead("PAGO", date(2026, 1, 5), status="pago")
        lead("PERDIDO", date(2026, 1, 6), status="perdido")
        lead("ABERTO", date(2026, 1, 7))
        m = R.metricas("2026-01", hoje=date(2026, 2, 1))
        self.assertEqual(m["total"], 3)
        self.assertEqual(m["funil"]["pago"], 1)
        self.assertEqual(m["taxa_geral"], 33.3)
        self.assertEqual(m["receita"], 200)
        json.dumps(m, allow_nan=False)   # precisa ser JSON válido para o navegador

    def test_mes_invalido(self):
        with self.assertRaises(ValueError):
            R.metricas("2026-13")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = R.ThreadingHTTPServer(("127.0.0.1", 0), R.H)
        cls.srv.daemon_threads = True
        cls.porta = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        limpar()
        R.K["etapas"] = {}
        for chave in ("PAINEL_SENHA", "WEBHOOK_TOKEN", "AUTH_CERT", "PROXY_SECRET"):
            os.environ.pop(chave, None)

    def cert_headers(self, pem, segredo="segredo"):
        return {"X-Client-Cert": quote(pem.decode(), safe=""), "X-Proxy-Secret": segredo}

    def req(self, caminho, metodo="GET", corpo=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.porta}{caminho}", method=metodo,
                                   data=corpo, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def post_json(self, caminho, dados):
        return self.req(caminho, "POST", json.dumps(dados).encode(),
                        {"Content-Type": "application/json", "X-Requested-With": "fetch"})

    def test_painel(self):
        status, headers, corpo = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn("Renovação de certificados", corpo.decode())
        self.assertIn("Content-Security-Policy", headers)

    def test_csrf_bloqueia_post_sem_header(self):
        status, _, _ = self.req("/api/regua", "POST", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_avancar_valida_status(self):
        rid = lead("HTTP", date(2026, 1, 1))
        self.assertEqual(self.post_json("/api/avancar", {"id": rid, "status": "pago"})[0], 200)
        self.assertEqual(self.post_json("/api/avancar", {"id": rid, "status": "hack"})[0], 400)
        self.assertEqual(R.db().execute("SELECT status FROM renovacoes WHERE id=?", (rid,)).fetchone()[0], "pago")

    def test_importar_pelo_painel(self):
        corpo = "Cliente;Vencimento\nACME;31/12/2026\n".encode()
        status, _, resp = self.req("/api/importar?nome=a.csv", "POST", corpo,
                                   {"Content-Type": "text/csv", "X-Requested-With": "fetch"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp)["novas"], 1)

    def test_metricas_mes_invalido(self):
        self.assertEqual(self.req("/api/metricas?mes=2026-13")[0], 400)

    def test_renovacoes_json_valido(self):
        lead("ACME", date(2026, 1, 1))
        status, _, corpo = self.req("/api/renovacoes")
        self.assertEqual(status, 200)
        self.assertNotIn(b"NaN", corpo)
        json.loads(corpo)

    def test_webhook_muda_status(self):
        rid = lead("LEAD", date(2026, 1, 1))
        R.db().execute("UPDATE renovacoes SET kommo_lead_id=999 WHERE id=?", (rid,))
        R.K["etapas"] = {"a_vencer": 10, "email_enviado": 11, "pago": 12}
        corpo = b"leads[status][0][id]=999&leads[status][0][status_id]=12"
        status, _, _ = self.req("/webhook/kommo", "POST", corpo,
                                {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertEqual(R.db().execute("SELECT status FROM renovacoes WHERE id=?", (rid,)).fetchone()[0], "pago")

    def test_webhook_token(self):
        os.environ["WEBHOOK_TOKEN"] = "segredo"
        try:
            self.assertEqual(self.req("/webhook/kommo", "POST", b"")[0], 403)
            self.assertEqual(self.req("/webhook/kommo?t=segredo", "POST", b"")[0], 200)
        finally:
            os.environ.pop("WEBHOOK_TOKEN", None)

    # ---------- login por certificado ----------
    def test_certificado_autoriza_e_rejeita(self):
        pem, fp, _ = gerar_cert("Ana")
        usuario("Ana", fp)
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            status, _, corpo = self.req("/api/eu", headers=self.cert_headers(pem))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(corpo)["nome"], "Ana")
            self.assertEqual(self.req("/api/eu")[0], 401)
            self.assertEqual(self.req("/api/eu", headers=self.cert_headers(pem, "errado"))[0], 401)
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)

    def test_certificado_vencido_ou_inativo(self):
        pem, fp, _ = gerar_cert("Vencido", dias_ini=-400, dias_fim=-30)
        usuario("Vencido", fp)
        pem2, fp2, _ = gerar_cert("Inativo")
        usuario("Inativo", fp2, ativo=0)
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            self.assertEqual(self.req("/api/eu", headers=self.cert_headers(pem))[0], 403)
            self.assertEqual(self.req("/api/eu", headers=self.cert_headers(pem2))[0], 403)
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)

    def test_operador_nao_acessa_administracao(self):
        pem, fp, _ = gerar_cert("Op")
        usuario("Op", fp, perfil="operador")
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            h = self.cert_headers(pem)
            self.assertEqual(self.req("/api/renovacoes", headers=h)[0], 200)
            self.assertEqual(self.req("/api/usuarios", headers=h)[0], 403)
            self.assertEqual(self.req("/api/config", headers=h)[0], 403)
            self.assertEqual(self.req("/api/config", "POST", b"{}",
                                      {**h, "X-Requested-With": "fetch"})[0], 403)
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)

    def test_cadastrar_certificado_pela_api(self):
        admin_pem, admin_fp, _ = gerar_cert("Admin")
        usuario("Admin", admin_fp)
        novo_pem, novo_fp, _ = gerar_cert("Novo")
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            h = {**self.cert_headers(admin_pem), "X-Requested-With": "fetch"}
            caminho = "/api/usuarios?nome=Novo&email=n@b.com&perfil=operador&arquivo=novo.pem"
            self.assertEqual(self.req(caminho, "POST", novo_pem, h)[0], 200)
            row = R.db().execute("SELECT nome, perfil FROM usuarios WHERE fp=?", (novo_fp,)).fetchone()
            self.assertEqual((row["nome"], row["perfil"]), ("Novo", "operador"))
            self.assertEqual(self.req(caminho, "POST", novo_pem, h)[0], 409)   # duplicado
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)

    def test_cadastrar_pfx_com_senha(self):
        admin_pem, admin_fp, _ = gerar_cert("Admin")
        usuario("Admin", admin_fp)
        pfx, fp_novo = gerar_pfx("senha")
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            h = {**self.cert_headers(admin_pem), "X-Requested-With": "fetch", "X-Pfx-Senha": "senha"}
            self.assertEqual(self.req("/api/usuarios?nome=PFX&arquivo=x.pfx", "POST", pfx, h)[0], 200)
            self.assertIsNotNone(R.db().execute("SELECT id FROM usuarios WHERE fp=?", (fp_novo,)).fetchone())
            h["X-Pfx-Senha"] = "errada"
            self.assertEqual(self.req("/api/usuarios?nome=PFX2&arquivo=y.pfx", "POST", pfx, h)[0], 400)
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)

    def test_cadastrar_certificado_der(self):
        admin_pem, admin_fp, _ = gerar_cert("Admin")
        usuario("Admin", admin_fp)
        _, fp_novo, der = gerar_cert("DER")
        os.environ["AUTH_CERT"] = "1"
        os.environ["PROXY_SECRET"] = "segredo"
        try:
            h = {**self.cert_headers(admin_pem), "X-Requested-With": "fetch"}
            status, _, _ = self.req("/api/usuarios?nome=DER&arquivo=der.cer", "POST", der, h)
            self.assertEqual(status, 200)
            self.assertIsNotNone(R.db().execute("SELECT id FROM usuarios WHERE fp=?", (fp_novo,)).fetchone())
        finally:
            os.environ.pop("AUTH_CERT", None)
            os.environ.pop("PROXY_SECRET", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
