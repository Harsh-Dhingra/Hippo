-- ============================================================
-- Hippo — Telling somebody when sync breaks (P2-OBS-2)
--
-- Two failures are currently invisible unless a person is reading logs.
--
-- SCHEMA DRIFT. _warn_on_drift logs a line and stores the payload anyway,
-- which is the right behaviour — drift must never lose a record. But a warning
-- nobody reads is not a notification, and drift is precisely the thing that
-- degrades quietly: a connector keeps working, extracts slightly less, and
-- answers get slightly worse for weeks.
--
-- SYNC FAILURE. sync_state.last_error has existed since 001 and nothing ever
-- wrote to it. A stream that has been failing for a day looks, from every
-- surface Hippo has, exactly like a stream with nothing to do. For the ACL
-- stream those two states are a stale permission and a current one.
--
-- DEDUPLICATED, OR IT IS NOISE
--
-- The ACL stream runs every four minutes. A failing one would produce three
-- hundred and sixty alerts a day, and a list of three hundred and sixty
-- identical rows is a list nobody reads. So an alert is identified by what it
-- is about — kind, connector, stream, fingerprint — and a repeat bumps a
-- counter and a timestamp rather than adding a row.
--
-- ACKNOWLEDGEMENT RATHER THAN DELETION
--
-- Clearing an alert says a person saw it, which is a fact worth keeping. It
-- also resets the notification: an acknowledged alert that recurs fires again,
-- because "we fixed it and it came back" is different news from "it is still
-- broken".
-- ============================================================

CREATE TABLE alerts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,
    connector_id    uuid REFERENCES connectors(id) ON DELETE CASCADE,
    stream          text,
    -- What makes two occurrences the same problem. A changed error message is
    -- a new alert; the same one recurring is not.
    fingerprint     text NOT NULL,
    detail          text NOT NULL,
    occurrences     int NOT NULL DEFAULT 1,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    acknowledged_by uuid REFERENCES principals(id),
    notified_at     timestamptz,

    CONSTRAINT alerts_kind_known
        CHECK (kind IN ('schema_drift', 'sync_failure', 'acl_stale', 'action_stuck')),
    CONSTRAINT alerts_acknowledged_names_its_person
        CHECK (acknowledged_at IS NULL OR acknowledged_by IS NOT NULL)
);

-- One live alert per distinct problem. The partial index is what makes the
-- upsert below deduplicate while still allowing the same problem to be raised
-- again after somebody has cleared it.
CREATE UNIQUE INDEX alerts_open_key ON alerts (kind, connector_id, stream, fingerprint)
    WHERE acknowledged_at IS NULL;
CREATE INDEX alerts_open_idx ON alerts (last_seen_at DESC) WHERE acknowledged_at IS NULL;

COMMENT ON TABLE alerts IS
    'Deduplicated by (kind, connector, stream, fingerprint) while open. A stream '
    'failing every four minutes is one alert with a counter, not three hundred '
    'and sixty rows.';

-- ------------------------------------------------------------
-- Raising one
-- ------------------------------------------------------------
-- SECURITY DEFINER so the sync worker can raise an alert without holding write
-- access to a table it should not otherwise touch, and so the same function
-- serves every role that might notice a problem.
CREATE FUNCTION raise_alert(
    p_kind        text,
    p_connector   uuid,
    p_stream      text,
    p_fingerprint text,
    p_detail      text
)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    INSERT INTO alerts (kind, connector_id, stream, fingerprint, detail)
    VALUES (p_kind, p_connector, p_stream, p_fingerprint, p_detail)
    ON CONFLICT (kind, connector_id, stream, fingerprint) WHERE acknowledged_at IS NULL
    DO UPDATE SET
        occurrences = alerts.occurrences + 1,
        last_seen_at = now(),
        detail = EXCLUDED.detail
    RETURNING id;
$$;

CREATE FUNCTION acknowledge_alert(p_alert uuid, p_principal uuid)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH cleared AS (
        UPDATE alerts SET acknowledged_at = now(), acknowledged_by = p_principal
        WHERE id = p_alert AND acknowledged_at IS NULL
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM cleared);
$$;

CREATE FUNCTION open_alerts(p_limit int DEFAULT 100)
RETURNS TABLE (
    id            uuid,
    kind          text,
    connector_id  uuid,
    connector     text,
    stream        text,
    detail        text,
    occurrences   int,
    first_seen_at timestamptz,
    last_seen_at  timestamptz,
    notified_at   timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT a.id, a.kind, a.connector_id, c.display_name, a.stream, a.detail,
           a.occurrences, a.first_seen_at, a.last_seen_at, a.notified_at
    FROM alerts a
    LEFT JOIN connectors c ON c.id = a.connector_id
    WHERE a.acknowledged_at IS NULL
    ORDER BY a.last_seen_at DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500);
$$;

REVOKE ALL ON FUNCTION raise_alert(text, uuid, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION acknowledge_alert(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION open_alerts(int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION raise_alert(text, uuid, text, text, text) TO hippo_sync;
GRANT EXECUTE ON FUNCTION raise_alert(text, uuid, text, text, text) TO hippo_api;
GRANT EXECUTE ON FUNCTION open_alerts(int) TO hippo_api;
GRANT EXECUTE ON FUNCTION acknowledge_alert(uuid, uuid) TO hippo_api;

-- Marking one as notified is the webhook's business, and the webhook runs in
-- the API process.
GRANT SELECT ON alerts TO hippo_api;
GRANT UPDATE (notified_at) ON alerts TO hippo_api;
