-- Reverts 004_jobs.sql.

DROP TRIGGER IF EXISTS jobs_notify_ready ON jobs;
DROP FUNCTION IF EXISTS notify_job_ready();
DROP TABLE IF EXISTS jobs;
