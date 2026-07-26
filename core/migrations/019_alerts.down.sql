-- Reverts 019_alerts.sql. The alerts go with it: they are derived from
-- conditions the system can observe again, and a stream that is still failing
-- will raise the same alert on its next run.

DROP FUNCTION IF EXISTS open_alerts(int);
DROP FUNCTION IF EXISTS acknowledge_alert(uuid, uuid);
DROP FUNCTION IF EXISTS raise_alert(text, uuid, text, text, text);
DROP TABLE IF EXISTS alerts;
