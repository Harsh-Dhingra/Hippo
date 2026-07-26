-- Reverses 022_skill_schedules.sql.
--
-- Schedules are configuration rather than history, so removing them loses
-- nothing that cannot be recreated from the skills that are still on disk.

DROP FUNCTION IF EXISTS my_schedules(uuid);
DROP FUNCTION IF EXISTS claim_due_skills(int);
DROP TABLE IF EXISTS skill_schedules;
DROP FUNCTION IF EXISTS next_skill_run(text, int, int, timestamptz);
