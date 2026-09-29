-- Invariants of the Change Database (docs/database.md "Invariants"). Every query returns the number of violations;
-- 0 everywhere = consistent. Run after tests, load or failure runs: `python scripts/verify_invariants.py`
-- (or `psql -f scripts/verify_invariants.sql`). Each statement returns one row: (check, violations).

-- 1. Versions of every product are contiguous 1..n (UNIQUE (partner_sku, version) already forbids duplicates).
SELECT 'versions_contiguous' AS check, count(*) AS violations
FROM (
  SELECT partner_sku FROM product_changes GROUP BY partner_sku
  HAVING min(version) <> 1 OR max(version) <> count(*)
) v;

-- 2. Every change chains to the previous one: v(n).prev_fingerprint = v(n-1).fingerprint, and it changed something.
SELECT 'fingerprint_chain' AS check, count(*) AS violations
FROM (
  SELECT prev_fingerprint, fingerprint,
         lag(fingerprint) OVER (PARTITION BY partner_sku ORDER BY version) AS expected_prev
  FROM product_changes
) c
WHERE prev_fingerprint IS DISTINCT FROM expected_prev OR prev_fingerprint = fingerprint;

-- 3. The current state equals the latest change of the product (and every product has a history).
SELECT 'state_matches_latest_change' AS check, count(*) AS violations
FROM products p
LEFT JOIN LATERAL (
  SELECT version, fingerprint FROM product_changes c
  WHERE c.partner_sku = p.partner_sku ORDER BY version DESC LIMIT 1
) last ON true
WHERE last.version IS NULL OR last.version <> p.version OR last.fingerprint <> p.fingerprint;

-- 4. Every processed webhook event has its job DONE (both are finished in the same transaction).
SELECT 'processed_event_has_done_job' AS check, count(*) AS violations
FROM inbox_event e
LEFT JOIN job j ON j.kind = 'webhook_event' AND j.ref_id = e.id::text
WHERE e.status = 'PROCESSED' AND j.status IS DISTINCT FROM 'DONE';

-- 5. Every event waiting for processing has an unfinished job (nothing accepted can be forgotten).
SELECT 'pending_event_has_job' AS check, count(*) AS violations
FROM inbox_event e
LEFT JOIN job j ON j.kind = 'webhook_event' AND j.ref_id = e.id::text
WHERE e.status = 'PENDING' AND j.status IS DISTINCT FROM 'PENDING' AND j.status IS DISTINCT FROM 'RUNNING';
