-- Dados de teste para o ambiente local (D1 --local).
DELETE FROM login_tentativas;
DELETE FROM eventos;
DELETE FROM renovacoes;
DELETE FROM usuarios;
DELETE FROM config;

INSERT INTO usuarios(nome,email,perfil,fp,sujeito,validade,ativo) VALUES
  ('Admin','admin@teste.com','admin',NULL,NULL,NULL,1),
  ('Operador','op@teste.com','operador',NULL,NULL,NULL,1);

INSERT INTO renovacoes(cliente,email,telefone,tipo,vencimento,valor,status,kommo_lead_id) VALUES
  ('Alfa Ltda','alfa@exemplo.com','79 99999-1111','e-CNPJ A1','2026-11-20',220,'a_vencer',999),
  ('Beta Ltda','beta@exemplo.com','79 99999-2222','e-CPF A1','2026-11-05',180,'pago',NULL),
  ('Gama Ltda','gama@exemplo.com',NULL,'e-CNPJ A3','2026-12-10',310,'a_vencer',NULL),
  ('Delta Ltda',NULL,'79 99999-3333','e-CPF A3','2026-10-01',220,'perdido',NULL);

INSERT OR REPLACE INTO config(chave,valor) VALUES('etapas','{"agendado":777}');
