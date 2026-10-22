-- Credentials the simulator presents to the apps it drives (a PayBroker merchant key, a gateway client key...). Only the vault's
-- ciphertext is stored: the data key is wrapped under the platform secret (CPSIM_ENCRYPTION_KEY) and the admin password.
CREATE TABLE IF NOT EXISTS cpsim_credential (name text PRIMARY KEY, ciphertext text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
