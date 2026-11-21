WITH events AS (
    SELECT i.scope_id, i.record_id, i.event_id, d.device_id, c.category_id,
           i.occurred_us, i.history_complete, i.lifecycle_complete,
           COUNT(*) OVER (PARTITION BY i.scope_id, i.event_id) AS identity_count
    FROM mfg_fault_incident i
    LEFT JOIN mfg_device d ON d.scope_id = i.scope_id AND d.device_id = i.device_id
    LEFT JOIN mfg_fault_category c ON c.scope_id = i.scope_id AND c.raw_code = i.raw_code
    WHERE i.scope_id = :authorized_scope AND i.occurred_us < :end_us
), buckets AS (
    SELECT scope_id, device_id, category_id, occurred_us, COUNT(*) AS simultaneous,
           MIN(record_id) AS representative
    FROM events GROUP BY scope_id, device_id, category_id, occurred_us
), ordered AS (
    SELECT b.*,
           LAG(simultaneous) OVER (PARTITION BY scope_id, device_id, category_id ORDER BY occurred_us) AS prev_count,
           LAG(representative) OVER (PARTITION BY scope_id, device_id, category_id ORDER BY occurred_us) AS prev_record
    FROM buckets b
), pairs AS (
    SELECT n.scope_id, n.record_id, n.event_id, n.device_id, n.category_id, n.occurred_us,
           n.history_complete, n.identity_count, b.simultaneous, b.prev_count,
           p.record_id AS prior_record, p.event_id AS prior_event, p.identity_count AS prior_identity_count,
           p.lifecycle_complete, p.occurred_us AS prior_us
    FROM events n
    LEFT JOIN ordered b ON n.scope_id=b.scope_id AND n.device_id=b.device_id
        AND n.category_id=b.category_id AND n.occurred_us=b.occurred_us
    LEFT JOIN events p ON p.scope_id=n.scope_id AND p.record_id=b.prev_record AND b.prev_count=1
    WHERE n.occurred_us >= :start_us
), possible_gaps AS (
    SELECT n.scope_id, n.record_id, COUNT(*) AS gaps
    FROM pairs n JOIN events u ON u.scope_id=n.scope_id AND u.occurred_us<=n.occurred_us
        AND (n.prior_us IS NULL OR u.occurred_us>=n.prior_us)
        AND (u.device_id IS NULL OR u.device_id=n.device_id)
        AND (u.category_id IS NULL OR u.category_id=n.category_id)
    WHERE u.device_id IS NULL OR u.category_id IS NULL
    GROUP BY n.scope_id, n.record_id
), changes AS (
    SELECT n.scope_id,n.record_id,l.transition_id,l.state,l.effective_us,
           MAX(l.effective_us) OVER (PARTITION BY n.scope_id,n.record_id) AS latest_us,
           MAX(CASE WHEN l.effective_us IS NULL THEN 1 ELSE 0 END)
               OVER (PARTITION BY n.scope_id,n.record_id) AS missing_time
    FROM pairs n JOIN mfg_fault_lifecycle l ON l.scope_id=n.scope_id AND l.event_id=n.prior_event
    WHERE l.effective_us<=n.occurred_us OR l.effective_us IS NULL
), effective AS (
    SELECT scope_id,record_id,COUNT(*) AS transitions,MIN(state) AS last_state,
           MIN(transition_id) AS transition_id,MAX(effective_us) AS last_us,MAX(missing_time) AS missing_time
    FROM changes WHERE effective_us=latest_us OR effective_us IS NULL GROUP BY scope_id,record_id
), ownership AS (
    SELECT n.scope_id,n.record_id,COUNT(*) AS relations,MIN(r.relation_id) AS relation_id,
           MIN(c.customer_id) AS customer_id,MIN(r.valid_from_us) AS valid_from_us,MIN(r.valid_to_us) AS valid_to_us
    FROM pairs n JOIN mfg_device_customer_relation r ON r.scope_id=n.scope_id AND r.device_id=n.device_id
    LEFT JOIN mfg_customer c ON c.scope_id=r.scope_id AND c.customer_id=r.customer_id
    WHERE r.role='using_customer' AND r.valid_from_us<=n.occurred_us
        AND (r.valid_to_us IS NULL OR n.occurred_us<r.valid_to_us)
    GROUP BY n.scope_id,n.record_id
)
SELECT ad.model AS device_model,ad.asset_tag AS asset_tag,product.source AS product_source,
       n.record_id,n.event_id,n.device_id,n.category_id,n.occurred_us,n.prior_record,n.prior_event,n.prior_us,
       e.transition_id,e.last_us,e.last_state,
       CASE
         WHEN n.event_id IS NULL OR n.identity_count<>1 THEN 'EVENT_IDENTITY_UNRESOLVED'
         WHEN n.device_id IS NULL THEN 'DEVICE_UNRESOLVED'
         WHEN n.category_id IS NULL THEN 'FAULT_UNMAPPED'
         WHEN COALESCE(n.history_complete,0)<>1 OR g.gaps IS NOT NULL THEN 'HISTORY_INCOMPLETE'
         WHEN n.simultaneous<>1 OR n.prev_count>1 THEN 'PREDECESSOR_AMBIGUOUS'
         WHEN n.prior_record IS NULL THEN 'NO_PREDECESSOR'
         WHEN n.prior_event IS NULL OR n.prior_identity_count<>1 THEN 'EVENT_IDENTITY_UNRESOLVED'
         WHEN COALESCE(n.lifecycle_complete,0)<>1 THEN 'LIFECYCLE_INCOMPLETE'
         WHEN e.missing_time=1 THEN 'CLOSE_TIME_MISSING'
         WHEN e.transitions>1 THEN 'LIFECYCLE_AMBIGUOUS'
         WHEN e.last_state IS NULL OR e.last_state IN ('open','reopened') THEN 'PREDECESSOR_OPEN'
         WHEN e.last_state<>'closed' OR e.last_us<n.prior_us THEN 'LIFECYCLE_INCOMPLETE'
         WHEN n.occurred_us-e.last_us<=0 OR n.occurred_us-e.last_us>2592000000000 THEN 'OUTSIDE_INTERVAL'
         ELSE 'MATCHED' END AS reason,
       o.relations,
       CASE WHEN o.relations=1 THEN o.customer_id END AS customer_id,
       CASE WHEN o.relations=1 THEN o.relation_id END AS relation_id,
       CASE WHEN o.relations=1 THEN o.valid_from_us END AS valid_from_us,
       CASE WHEN o.relations=1 THEN o.valid_to_us END AS valid_to_us
FROM pairs n LEFT JOIN effective e ON e.scope_id=n.scope_id AND e.record_id=n.record_id
LEFT JOIN possible_gaps g ON g.scope_id=n.scope_id AND g.record_id=n.record_id
LEFT JOIN ownership o ON o.scope_id=n.scope_id AND o.record_id=n.record_id
LEFT JOIN asset_detail ad ON ad.scope_id=n.scope_id AND ad.device_id=n.device_id
LEFT JOIN product ON product.model=ad.model
ORDER BY n.occurred_us,n.record_id
