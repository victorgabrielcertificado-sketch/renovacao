-- Adiciona senha individual (hash PBKDF2) aos usuários.
-- Execute uma vez no D1 local e no remoto:
--   npx wrangler d1 execute renovacao --local  --file=migrations/2026-11-11-senha-usuarios.sql
--   npx wrangler d1 execute renovacao --remote --file=migrations/2026-11-11-senha-usuarios.sql
ALTER TABLE usuarios ADD COLUMN senha_hash TEXT;
