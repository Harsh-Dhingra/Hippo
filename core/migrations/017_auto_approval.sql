-- ============================================================
-- Hippo — Auto-approval, without pretending a person clicked (P2-GOV-1)
--
-- P1-AGT-3 shipped the risk tiers and deliberately did not ship this:
-- ARCHITECTURE §11 unlocks risk-tiered auto-execution "after the approval UX
-- and rollback are proven". The UX landed with P1-SRF-2 and rollback with
-- P1-SYNC-5, so the condition is met and the label can start meaning
-- something.
--
-- WHO APPROVES, WHEN NOBODY CLICKS
--
-- The honest answer is: the operator did, in advance, by writing a policy
-- file. That is a real authorisation — it is how a deploy gate works — but it
-- is not the same act as a person reading a payload and deciding, and the
-- audit log must not blur them.
--
-- So approved_by stays a principal and stays NULL for these, and
-- approved_by_policy records which rule authorised it. Filling approved_by
-- with a service principal would have been easier and would have made every
-- future query about "which of these did a human actually look at"
-- unanswerable.
--
-- WHICH COMPONENT DOES IT
--
-- Not the agent: hippo_agent holds INSERT on actions and no UPDATE, so it
-- cannot approve its own proposal, and that stays true.
--
-- Not the sync worker either, which is the interesting choice. It holds the
-- credentials and does the executing, and letting it also approve would
-- collapse propose/approve/execute into one component — exactly what rule 2
-- exists to prevent. It has SELECT and UPDATE on actions for the execution
-- transitions, so the constraint below is what stops it: only hippo_api may
-- write approved_by_policy.
--
-- hippo_api is the role that represents people. An operator's policy standing
-- in for a person's click belongs there, and that role holds no source-system
-- credential — so the component that decides still cannot be the component
-- that acts.
-- ============================================================

ALTER TABLE actions ADD COLUMN approved_by_policy text;

COMMENT ON COLUMN actions.approved_by_policy IS
    'The policy rule that authorised this, when no person clicked. Never a '
    'stand-in for approved_by: the audit log has to be able to answer which '
    'actions a human actually looked at.';

CREATE INDEX actions_auto_approved_idx ON actions (approved_by_policy, created_at)
    WHERE approved_by_policy IS NOT NULL;

-- An approval is now either a person or a policy, and never neither.
ALTER TABLE actions DROP CONSTRAINT actions_execution_requires_approval;
ALTER TABLE actions ADD CONSTRAINT actions_execution_requires_approval
    CHECK (
        status IN ('pending', 'declined', 'failed')
        OR approved_by IS NOT NULL
        OR approved_by_policy IS NOT NULL
    );

-- Both at once is a contradiction rather than extra assurance: it would say a
-- person clicked and also that nobody did.
ALTER TABLE actions ADD CONSTRAINT actions_one_kind_of_approval
    CHECK (approved_by IS NULL OR approved_by_policy IS NULL);

-- ------------------------------------------------------------
-- Only the role that represents people may record a policy approval
-- ------------------------------------------------------------
-- hippo_sync needs UPDATE on actions for the execution transitions, so a
-- column grant is what keeps it from also approving. Postgres checks
-- column-level privileges on UPDATE, so this is enforced rather than agreed.
REVOKE UPDATE ON actions FROM hippo_sync;
GRANT UPDATE (
    status, executed_at, inverse_payload, receipt, error,
    execution_started_at, rolled_back_at, rolled_back_by
) ON actions TO hippo_sync;

COMMENT ON CONSTRAINT actions_one_kind_of_approval ON actions IS
    'A person or a policy, never both.';
