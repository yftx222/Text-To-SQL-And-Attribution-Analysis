-- ORD-01
SELECT COUNT(DISTINCT order_id) AS orders
FROM dwd_trade_order_detail_di
WHERE order_create_time >= '2026-09-01' AND order_create_time < '2026-10-01';

-- CNV-01 (after clarification)
SELECT SUM(receivable_amount) AS receivable
FROM dwd_trade_order_detail_di
WHERE order_create_time >= '2026-09-01' AND order_create_time < '2026-10-01';

-- ATT-01 (second half less first half, yuan)
WITH by_channel AS (
    SELECT channel_sk,
           SUM(CASE WHEN order_create_time < '2026-09-16'
                    THEN -receivable_amount ELSE receivable_amount END) AS delta
    FROM dwd_trade_order_detail_di
    WHERE order_create_time >= '2026-09-01' AND order_create_time < '2026-10-01'
    GROUP BY channel_sk
), labeled AS (
    SELECT c.channel_code AS channel, SUM(b.delta) AS delta
    FROM by_channel b LEFT JOIN dim_channel_info c ON c.channel_sk = b.channel_sk
    GROUP BY c.channel_code
)
SELECT channel, delta FROM labeled
UNION ALL
SELECT 'ALL', SUM(delta) FROM labeled;
