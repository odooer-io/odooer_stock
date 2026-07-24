-- ============================================================================
-- Fix stale stock_move.odooer_value / odooer_unit_cost / signed_odooer_value
-- and product_product.odooer_fifo_cost after odooer_fifo_link was rebuilt
-- via raw SQL (odooer.fifo.regenerate wizard) without going through the ORM.
--
-- Root cause: the regenerate wizard bulk rebuilds odooer_fifo_link via a
-- PL/pgSQL function (bypassing the ORM), so Odoo's @api.depends recompute
-- chain never fires for stock_move.odooer_value/odooer_unit_cost,
-- stock_move.signed_odooer_value, and product_product.odooer_fifo_cost.
-- Those stored columns keep whatever value they had before the last
-- regenerate, silently drifting out of sync with the freshly-rebuilt links.
--
-- This script recalculates those columns directly from the current
-- odooer_fifo_link data, mirroring the exact same formulas as the Python
-- compute methods:
--   stock_move._compute_odooer_value():
--       odooer_value      = SUM(odooer_fifo_link.outgoing_value) for this move
--       odooer_unit_cost  = odooer_value / quantity
--   stock_move._compute_signed_odooer_value():
--       signed_odooer_value = -odooer_value  (outgoing moves, is_out = true)
--                            = value          (incoming moves, else)
--   product_product._compute_odooer_fifo_cost():
--       unit cost of the oldest done incoming move that still has
--       unconsumed quantity (quantity - SUM(consumed via odooer_fifo_link))
--
-- SAFE TO RE-RUN: purely idempotent recalculation from source data, no
-- destructive operations. Wrap in a transaction so you can inspect the
-- diff report before committing.
--
-- Recommended workflow on production:
--   1. Run Section 1 (diagnostic) first to see the scope of the problem.
--   2. BEGIN; run Section 2 (fix); review row counts; COMMIT; (or ROLLBACK).
--   3. Re-run Section 1 to confirm 0 discrepancies remain.
-- ============================================================================


-- ── Section 1: Diagnostic — list discrepancies (read-only) ─────────────────

-- 1a. stock_move.odooer_value vs SUM(odooer_fifo_link.outgoing_value)
SELECT
    sm.id                        AS move_id,
    sm.product_id,
    sm.date,
    sm.odooer_value              AS stored_odooer_value,
    COALESCE(l.link_total, 0)    AS actual_link_total,
    sm.odooer_value - COALESCE(l.link_total, 0) AS diff
FROM stock_move sm
LEFT JOIN (
    SELECT outgoing_move_id, SUM(outgoing_value) AS link_total
    FROM odooer_fifo_link
    GROUP BY outgoing_move_id
) l ON l.outgoing_move_id = sm.id
WHERE sm.is_out = true
  AND sm.state = 'done'
  AND ABS(sm.odooer_value - COALESCE(l.link_total, 0)) > 0.01
ORDER BY sm.date;

-- 1b. stock_move.odooer_unit_cost vs recalculated (odooer_value / quantity)
SELECT
    sm.id AS move_id, sm.odooer_unit_cost AS stored_unit_cost,
    CASE WHEN sm.quantity > 0 THEN sm.odooer_value / sm.quantity ELSE 0 END AS expected_unit_cost
FROM stock_move sm
WHERE sm.is_out = true
  AND sm.state = 'done'
  AND sm.quantity > 0
  AND ABS(sm.odooer_unit_cost - (sm.odooer_value / sm.quantity)) > 0.01;

-- 1c. stock_move.signed_odooer_value vs expected
SELECT
    sm.id AS move_id, sm.is_in, sm.is_out,
    sm.signed_odooer_value AS stored_signed_value,
    CASE WHEN sm.is_out THEN -sm.odooer_value ELSE sm.value END AS expected_signed_value
FROM stock_move sm
WHERE sm.state = 'done'
  AND (sm.is_in = true OR sm.is_out = true)
  AND ABS(sm.signed_odooer_value - (CASE WHEN sm.is_out THEN -sm.odooer_value ELSE sm.value END)) > 0.01;


-- ── Section 2: Fix — recalculate and write back the correct values ─────────
-- Run inside a transaction so you can review before committing:
--   BEGIN;
--   ... (run the UPDATE statements below) ...
--   -- re-run Section 1 queries here to confirm 0 rows returned
--   COMMIT;   -- or ROLLBACK; if something looks wrong

BEGIN;

-- 2a. Fix odooer_value / odooer_unit_cost on outgoing moves
WITH link_totals AS (
    SELECT outgoing_move_id, SUM(outgoing_value) AS link_total
    FROM odooer_fifo_link
    GROUP BY outgoing_move_id
)
UPDATE stock_move sm
SET odooer_value = COALESCE(lt.link_total, 0),
    odooer_unit_cost = CASE
        WHEN sm.quantity > 0 THEN COALESCE(lt.link_total, 0) / sm.quantity
        ELSE 0
    END
FROM (SELECT id FROM stock_move WHERE is_out = true AND state = 'done') AS out_moves
LEFT JOIN link_totals lt ON lt.outgoing_move_id = out_moves.id
WHERE sm.id = out_moves.id
  AND (
      sm.odooer_value IS DISTINCT FROM COALESCE(lt.link_total, 0)
      OR sm.odooer_unit_cost IS DISTINCT FROM (
          CASE WHEN sm.quantity > 0 THEN COALESCE(lt.link_total, 0) / sm.quantity ELSE 0 END
      )
  );

-- 2b. Fix signed_odooer_value on all done incoming/outgoing moves
--     (depends on the freshly-corrected odooer_value from step 2a)
UPDATE stock_move sm
SET signed_odooer_value = CASE
    WHEN sm.is_out THEN -sm.odooer_value
    ELSE sm.value
END
WHERE sm.state = 'done'
  AND (sm.is_in = true OR sm.is_out = true)
  AND sm.signed_odooer_value IS DISTINCT FROM (
      CASE WHEN sm.is_out THEN -sm.odooer_value ELSE sm.value END
  );

-- 2c. Fix product_product.odooer_fifo_cost (current FIFO next-out cost)
--     = unit cost of the oldest done incoming move that still has
--       unconsumed quantity (quantity - SUM(consumed via odooer_fifo_link))
WITH consumed AS (
    SELECT incoming_move_id, SUM(quantity) AS consumed_qty
    FROM odooer_fifo_link
    WHERE incoming_move_id IS NOT NULL
    GROUP BY incoming_move_id
),
incoming_remaining AS (
    SELECT
        sm.id, sm.product_id, sm.date, sm.value, sm.quantity,
        sm.quantity - COALESCE(c.consumed_qty, 0) AS remaining
    FROM stock_move sm
    LEFT JOIN consumed c ON c.incoming_move_id = sm.id
    WHERE sm.is_in = true
      AND sm.state = 'done'
      AND sm.quantity > 0
),
next_out AS (
    -- Oldest move per product with remaining > 0 (rounding-tolerant)
    SELECT DISTINCT ON (product_id)
        product_id,
        CASE WHEN quantity > 0 THEN value / quantity ELSE 0 END AS fifo_cost
    FROM incoming_remaining
    WHERE remaining > 0.0000001
    ORDER BY product_id, date ASC, id ASC
)
UPDATE product_product pp
SET odooer_fifo_cost = COALESCE(no.fifo_cost, 0)
FROM (SELECT id FROM product_product) AS all_products
LEFT JOIN next_out no ON no.product_id = all_products.id
WHERE pp.id = all_products.id
  AND pp.odooer_fifo_cost IS DISTINCT FROM COALESCE(no.fifo_cost, 0);

-- Review the diff before committing: re-run Section 1 queries here.
-- If all three return 0 rows, it's safe to COMMIT.

COMMIT;
-- (Replace COMMIT with ROLLBACK above if you want to abort after reviewing.)
