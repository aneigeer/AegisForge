-- DBGuardian 沙箱基础库与种子数据（MySQL 8）
-- 注意：MySQL 中 schema 等价于 database，因此不创建嵌套 schema，表直接位于 dg_lab。
-- 只包含表结构和演示数据；账号与授权见 00_roles.sh / scripts/bootstrap_mysql.py。

CREATE DATABASE IF NOT EXISTS dg_lab
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE dg_lab;

CREATE TABLE IF NOT EXISTS seed_customers (
    customer_id   INT UNSIGNED NOT NULL PRIMARY KEY,
    customer_name VARCHAR(64) NOT NULL,
    region        VARCHAR(32) NOT NULL,
    created_at    DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS seed_orders (
    order_id      INT UNSIGNED NOT NULL PRIMARY KEY,
    customer_id   INT UNSIGNED NOT NULL,
    status        VARCHAR(16) NOT NULL,
    amount_cents  BIGINT NOT NULL,
    created_at    DATETIME NOT NULL,
    note          VARCHAR(128) NULL,
    KEY idx_seed_orders_customer (customer_id),
    KEY idx_seed_orders_created_at (created_at),
    CONSTRAINT fk_seed_orders_customer
        FOREIGN KEY (customer_id) REFERENCES seed_customers (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO seed_customers (customer_id, customer_name, region, created_at)
SELECT
    1000 + n,
    CONCAT('customer_', LPAD(n, 3, '0')),
    ELT(1 + (n % 5), 'north', 'south', 'east', 'west', 'central'),
    TIMESTAMP('2025-01-01 00:00:00') + INTERVAL n DAY
FROM (
    SELECT a.n + 10 * b.n AS n
    FROM (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
          UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) a
    CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
          UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) b
    WHERE a.n + 10 * b.n BETWEEN 1 AND 40
) seq;

INSERT IGNORE INTO seed_orders (order_id, customer_id, status, amount_cents, created_at, note)
SELECT
    2000 + n,
    1000 + 1 + ((n - 1) % 40),
    ELT(1 + (n % 4), 'paid', 'pending', 'cancelled', 'refunded'),
    500 + (n * 37) % 10000,
    TIMESTAMP('2025-02-01 00:00:00') + INTERVAL n HOUR,
    IF(n % 11 = 0, 'sandbox-seed', NULL)
FROM (
    SELECT a.n + 10 * b.n + 100 * c.n AS n
    FROM (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
          UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) a
    CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
          UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) b
    CROSS JOIN
    (SELECT 0 n UNION ALL SELECT 1 UNION ALL SELECT 2) c
    WHERE a.n + 10 * b.n + 100 * c.n BETWEEN 1 AND 200
) seq;
