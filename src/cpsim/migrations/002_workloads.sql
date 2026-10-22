-- Workloads: scheduled pushes to the other apps of the ecosystem. Like cities and companies the spec is JSON; it holds only the NAME of
-- the environment variable that carries a credential, never the credential. Existing tables are untouched.
CREATE TABLE IF NOT EXISTS cpsim_workload (name text PRIMARY KEY, spec jsonb NOT NULL);
