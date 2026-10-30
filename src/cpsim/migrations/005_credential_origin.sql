-- A saved credential is bound to the origin (or fleet reference such as ${PAYBROKER_URL}) it may be sent to.
-- Rows saved before this column existed stay NULL: a well-known name (PAYBROKER_SECRET_KEY ...) is then bound to its own ecosystem app,
-- any other name is not sent anywhere until it is saved again with an origin.
ALTER TABLE cpsim_credential ADD COLUMN IF NOT EXISTS origin text;
