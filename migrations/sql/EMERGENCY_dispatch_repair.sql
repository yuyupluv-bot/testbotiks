-- Safe one-time recovery for stuck VK messages and visibly free drivers.
-- Run only after stopping every old bot process.

UPDATE outbox_messages
SET status = 'pending', claimed_at = NULL, next_attempt_at = NOW()
WHERE status IN ('sending', 'failed') AND sent_at IS NULL;

UPDATE drivers_queue AS dq
SET status = 'waiting'
FROM users AS u
WHERE u.id = dq.driver_id
  AND u.is_on_line = TRUE
  AND u.driver_status = 'online'
  AND NOT EXISTS (
      SELECT 1 FROM orders AS o
      WHERE o.driver_id = u.id
        AND o.status IN ('assigned', 'arrived', 'in_progress', 'parallel_assigned')
  )
  AND NOT EXISTS (
      SELECT 1 FROM orders AS o
      WHERE o.offered_driver_id = u.id AND o.status = 'searching'
  );
