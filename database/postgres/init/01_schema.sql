-- PG LAB schema v1：与 MySQL LAB 保持同构的客户/订单最小业务模型。
-- 仅用于隔离测试环境；不包含真实数据。
CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id),
    status TEXT NOT NULL,
    amount_cents BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

INSERT INTO customers (customer_id, name, email, created_at)
SELECT g,
       'customer_' || g,
       'customer_' || g || '@example.test',
       now() - ((g % 40) || ' days')::interval
FROM generate_series(1, 40) AS g
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO orders (order_id, customer_id, status, amount_cents, created_at, updated_at)
SELECT 2000 + g,
       1 + (g % 40),
       (ARRAY['pending','paid','shipped','cancelled'])[1 + (g % 4)],
       1000 + (g * 37) % 90000,
       now() - ((g % 60) || ' days')::interval,
       now() - ((g % 30) || ' days')::interval
FROM generate_series(1, 200) AS g
ON CONFLICT (order_id) DO NOTHING;

GRANT CONNECT ON DATABASE dg_lab_pg TO dg_ro_pg;
GRANT USAGE ON SCHEMA public TO dg_ro_pg;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dg_ro_pg;
ALTER DEFAULT PRIVILEGES FOR ROLE dg_rw_pg IN SCHEMA public
    GRANT SELECT ON TABLES TO dg_ro_pg;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- PG 12 无内置只读事务开关；只读由账号权限（仅 SELECT）与应用层只读校验共同保证。
