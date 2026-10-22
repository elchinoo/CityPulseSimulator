-- Cities and companies (the simulator's own state). The kit creates app_setting first; the admin password hash lives there.
CREATE TABLE IF NOT EXISTS app_setting (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS cpsim_city (name text PRIMARY KEY, spec jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS cpsim_company (name text PRIMARY KEY, spec jsonb NOT NULL);

-- Upgrade from the first release, which kept its settings (the admin password hash) in cpsim_setting.
DO $$
BEGIN
    IF to_regclass('public.cpsim_setting') IS NOT NULL THEN
        INSERT INTO app_setting (key, value) SELECT key, value FROM cpsim_setting ON CONFLICT (key) DO NOTHING;
    END IF;
END $$;
