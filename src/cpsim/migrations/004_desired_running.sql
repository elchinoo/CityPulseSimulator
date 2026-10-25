-- The desired run state: what a person (or a schedule) started and nobody stopped. After a restart the app resumes everything with the flag set.
-- A company has two: its one-off run and its recurring schedule. Existing rows start false; saving a spec never touches the flags.
ALTER TABLE cpsim_city ADD COLUMN IF NOT EXISTS desired_running boolean NOT NULL DEFAULT false;
ALTER TABLE cpsim_workload ADD COLUMN IF NOT EXISTS desired_running boolean NOT NULL DEFAULT false;
ALTER TABLE cpsim_company ADD COLUMN IF NOT EXISTS desired_running boolean NOT NULL DEFAULT false;
ALTER TABLE cpsim_company ADD COLUMN IF NOT EXISTS desired_schedule boolean NOT NULL DEFAULT false;
