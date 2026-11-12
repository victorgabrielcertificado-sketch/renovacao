-- Marca a data do último e-mail de vencimento enviado.
-- Execute uma vez no D1 local e no remoto:
--   npx wrangler d1 execute renovacao --local  --file=migrations/2026-11-12-ultimo-email.sql
--   npx wrangler d1 execute renovacao --remote --file=migrations/2026-11-12-ultimo-email.sql
-- Se o --file falhar com "Authentication error [code: 10000]", use --command:
--   npx wrangler d1 execute renovacao --remote --command "ALTER TABLE renovacoes ADD COLUMN ultimo_email TEXT;"
ALTER TABLE renovacoes ADD COLUMN ultimo_email TEXT;
