-- Banco D1 (SQLite) da versão Cloudflare Pages.
-- Aplique com: npx wrangler d1 execute renovacao --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS renovacoes(
  id INTEGER PRIMARY KEY,
  cliente TEXT NOT NULL,
  email TEXT,
  telefone TEXT,
  tipo TEXT NOT NULL,
  vencimento TEXT NOT NULL,
  valor REAL,
  status TEXT NOT NULL DEFAULT 'a_vencer',
  kommo_lead_id INTEGER,
  regua_ultima INTEGER,
  UNIQUE(cliente, tipo, vencimento)
);

CREATE TABLE IF NOT EXISTS usuarios(
  id INTEGER PRIMARY KEY,
  nome TEXT NOT NULL,
  email TEXT,
  perfil TEXT NOT NULL DEFAULT 'operador',
  fp TEXT UNIQUE,
  sujeito TEXT,
  validade TEXT,
  ativo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT);

CREATE TABLE IF NOT EXISTS eventos(
  id INTEGER PRIMARY KEY,
  renovacao_id INTEGER,
  ts TEXT NOT NULL,
  tipo TEXT NOT NULL,
  canal TEXT,
  resultado TEXT,
  detalhe TEXT
);

CREATE TABLE IF NOT EXISTS login_tentativas(
  chave TEXT PRIMARY KEY,
  contagem INTEGER NOT NULL DEFAULT 0,
  expira INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_renov_venc   ON renovacoes(vencimento);
CREATE INDEX IF NOT EXISTS ix_renov_status ON renovacoes(status);
CREATE INDEX IF NOT EXISTS ix_renov_lead   ON renovacoes(kommo_lead_id);
CREATE INDEX IF NOT EXISTS ix_eventos_rid  ON eventos(renovacao_id);
CREATE INDEX IF NOT EXISTS ix_usuarios_fp  ON usuarios(fp);
CREATE INDEX IF NOT EXISTS ix_login_expira ON login_tentativas(expira);
