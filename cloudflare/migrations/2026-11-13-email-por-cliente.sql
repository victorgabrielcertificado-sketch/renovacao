-- E-mail de envio por cliente: alternativo ao importado e chave de ativação.
ALTER TABLE renovacoes ADD COLUMN email_envio TEXT;
ALTER TABLE renovacoes ADD COLUMN enviar_email INTEGER NOT NULL DEFAULT 1;
