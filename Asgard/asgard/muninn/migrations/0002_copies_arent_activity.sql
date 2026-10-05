-- =====================================================================
-- Muninn schema v2  (PRAGMA user_version = 2), Asgard 0.3.1
--
-- Squash copies aren't activity. From 0.3.1 Baldur stores the squash
-- copies of your commits (a pull request squash-merged on GitHub, or a
-- local git merge --squash of commits already collected) in commits with
-- is_merge = 1 and no keys. Merge commits themselves are never collected,
-- so is_merge = 1 means a copy. A copy is never work: Baldur keeps its time
-- only so the sessions it links share one length limit. Its time may not
-- even be yours, since anyone can press merge, so v_activity leaves it out.
-- Work queries filter is_merge = 0 the same way.
-- =====================================================================
BEGIN;

DROP VIEW v_activity;

-- Everything you did, in time order. Squash copies (is_merge = 1) are left out.
CREATE VIEW v_activity AS
    SELECT c.authored_at AS at, 'commit' AS kind, c.repo_id, c.id AS ref_id, c.subject AS label
      FROM commits c WHERE c.is_mine = 1 AND c.is_merge = 0
    UNION ALL
    SELECT r.at, 'reflog', r.repo_id, r.id, r.action
      FROM reflog_entries r
    UNION ALL
    SELECT v.submitted_at, 'pr_review', p.repo_id, v.id, p.title
      FROM pr_reviews v JOIN pull_requests p ON p.id = v.pr_id WHERE v.is_mine = 1
    UNION ALL
    SELECT t.at, 'transition', NULL, t.id, w.key || ' -> ' || t.to_status
      FROM work_item_transitions t JOIN work_items w ON w.id = t.work_item_id WHERE t.by_me = 1;

PRAGMA user_version = 2;
COMMIT;
