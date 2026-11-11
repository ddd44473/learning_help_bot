CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    telegram_id INTEGER NOT NULL UNIQUE,

    tg_username TEXT,
    tg_full_name TEXT,

    bot_nickname TEXT,
    bot_nickname_normalized TEXT UNIQUE,

    birthday TEXT,

    level INTEGER NOT NULL DEFAULT 1
        CHECK (level >= 1),

    coins INTEGER NOT NULL DEFAULT 0
        CHECK (coins >= 0),

    balance INTEGER NOT NULL DEFAULT 0
        CHECK (balance >= 0),

    bonus_balance INTEGER NOT NULL DEFAULT 0
        CHECK (bonus_balance >= 0),

    referral_code TEXT,

    is_registered INTEGER NOT NULL DEFAULT 0
        CHECK (is_registered IN (0, 1)),

    rules_accepted INTEGER NOT NULL DEFAULT 0
        CHECK (rules_accepted IN (0, 1)),

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    last_active_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
);
"""


CREATE_USERS_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code
ON users(referral_code)
WHERE referral_code IS NOT NULL;
"""


CREATE_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,

    direction TEXT,

    subject TEXT NOT NULL,

    work_type TEXT NOT NULL,

    description TEXT NOT NULL,

    deadline TEXT,

    status TEXT NOT NULL DEFAULT 'new'
        CHECK (
            status IN (
                'new',
                'in_progress',
                'waiting_client',
                'completed',
                'cancelled',
                'closed'
            )
        ),

    assigned_master_telegram_id INTEGER,

    assignment_status TEXT NOT NULL DEFAULT 'assigned'
        CHECK (
            assignment_status IN (
                'assigned',
                'waiting_selection',
                'open',
                'cancelled'
            )
        ),

    agreed_price INTEGER
        CHECK (
            agreed_price IS NULL
            OR agreed_price > 0
        ),

    price_status TEXT NOT NULL DEFAULT 'waiting_master'
        CHECK (
            price_status IN (
                'waiting_master',
                'waiting_client',
                'waiting_master_response',
                'agreed',
                'rejected'
            )
        ),

    group_chat_id INTEGER,

    topic_id INTEGER,

    order_card_message_id INTEGER,

    cash_paid_amount INTEGER
        CHECK (
            cash_paid_amount IS NULL
            OR cash_paid_amount >= 0
        ),

    bonus_used INTEGER NOT NULL DEFAULT 0
        CHECK (bonus_used >= 0),

    referral_discount INTEGER NOT NULL DEFAULT 0
        CHECK (referral_discount >= 0),

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    assignment_generation INTEGER NOT NULL DEFAULT 0,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
);
"""


CREATE_ORDERS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_orders_user_id
ON orders(user_id);

CREATE INDEX IF NOT EXISTS idx_orders_status
ON orders(status);

CREATE INDEX IF NOT EXISTS idx_orders_topic_id
ON orders(topic_id);

CREATE INDEX IF NOT EXISTS idx_orders_assigned_master
ON orders(assigned_master_telegram_id);

CREATE INDEX IF NOT EXISTS idx_orders_assignment_status
ON orders(assignment_status);
"""


CREATE_ORDER_FILES_TABLE = """
CREATE TABLE IF NOT EXISTS order_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    telegram_file_id TEXT NOT NULL,

    file_type TEXT NOT NULL
        CHECK (
            file_type IN (
                'photo',
                'document'
            )
        ),

    file_name TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE
);
"""


CREATE_ORDER_FILES_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_order_files_order_id
ON order_files(order_id);
"""


CREATE_REVIEWS_TABLE = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL UNIQUE,

    client_user_id INTEGER NOT NULL,

    master_telegram_id INTEGER NOT NULL,

    rating INTEGER NOT NULL
        CHECK (
            rating BETWEEN 1 AND 5
        ),

    review_text TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE,

    FOREIGN KEY (client_user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);
"""


CREATE_MASTERS_TABLE = """
CREATE TABLE IF NOT EXISTS masters (
    telegram_id INTEGER PRIMARY KEY,

    username TEXT,

    full_name TEXT NOT NULL,

    university TEXT,

    specialty_code TEXT,

    specialty_name TEXT,

    average_response_minutes INTEGER
        CHECK (
            average_response_minutes IS NULL
            OR average_response_minutes >= 0
        ),

    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1)),

    deleted_at TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
);
"""

CREATE_REVIEWS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_reviews_client_user_id
ON reviews(client_user_id);

CREATE INDEX IF NOT EXISTS idx_reviews_master_telegram_id
ON reviews(master_telegram_id);

CREATE INDEX IF NOT EXISTS idx_reviews_rating
ON reviews(rating);
"""

CREATE_MASTERS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_masters_username
ON masters(username);

CREATE INDEX IF NOT EXISTS idx_masters_is_active
ON masters(is_active);
"""


CREATE_MASTER_DIRECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS master_directions (
    master_telegram_id INTEGER NOT NULL,

    direction TEXT NOT NULL,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    PRIMARY KEY (
        master_telegram_id,
        direction
    ),

    FOREIGN KEY (master_telegram_id)
        REFERENCES masters(telegram_id)
        ON DELETE CASCADE
);
"""


CREATE_MASTER_DIRECTIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_master_directions_master
ON master_directions(master_telegram_id);

CREATE INDEX IF NOT EXISTS idx_master_directions_direction
ON master_directions(direction);
"""


CREATE_BALANCE_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS balance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,

    amount INTEGER NOT NULL
        CHECK (amount != 0),

    balance_before INTEGER NOT NULL
        CHECK (balance_before >= 0),

    balance_after INTEGER NOT NULL
        CHECK (balance_after >= 0),

    transaction_type TEXT NOT NULL
        CHECK (
            transaction_type IN (
                'admin_deposit',
                'admin_withdrawal',
                'payment',
                'refund',
                'bonus'
            )
        ),

    admin_telegram_id INTEGER,

    order_id INTEGER,

    comment TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE RESTRICT,

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL
);
"""





CREATE_BALANCE_TRANSACTIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_balance_transactions_user_id
ON balance_transactions(user_id);

CREATE INDEX IF NOT EXISTS idx_balance_transactions_created_at
ON balance_transactions(created_at);

CREATE INDEX IF NOT EXISTS idx_balance_transactions_admin_id
ON balance_transactions(admin_telegram_id);

CREATE INDEX IF NOT EXISTS idx_balance_transactions_order_id
ON balance_transactions(order_id);
"""

CREATE_ORDER_PRICE_OFFERS_TABLE = """
CREATE TABLE IF NOT EXISTS order_price_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    offered_by TEXT NOT NULL
        CHECK (
            offered_by IN (
                'master',
                'client'
            )
        ),

    offered_by_telegram_id INTEGER NOT NULL,

    amount INTEGER NOT NULL
        CHECK (amount > 0),

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'accepted',
                'rejected',
                'superseded'
            )
        ),

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE
);
"""

CREATE_ORDER_PRICE_OFFERS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_order_price_offers_order_id
ON order_price_offers(order_id);

CREATE INDEX IF NOT EXISTS idx_order_price_offers_status
ON order_price_offers(status);

CREATE INDEX IF NOT EXISTS idx_order_price_offers_created_at
ON order_price_offers(created_at);
"""

CREATE_MASTER_RESPONSE_TIMES_TABLE = """
CREATE TABLE IF NOT EXISTS master_response_times (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    master_telegram_id INTEGER NOT NULL,

    client_message_at TEXT NOT NULL
        DEFAULT (datetime('now', '+3 hours')),

    master_response_at TEXT,

    response_seconds INTEGER
        CHECK (
            response_seconds IS NULL
            OR response_seconds >= 0
        ),

    created_at TEXT NOT NULL
        DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE,

    FOREIGN KEY (master_telegram_id)
        REFERENCES masters(telegram_id)
        ON DELETE CASCADE
)
"""


CREATE_MASTER_RESPONSE_TIMES_INDEXES = """
CREATE INDEX IF NOT EXISTS
idx_master_response_times_order_open
ON master_response_times (
    order_id,
    master_telegram_id,
    master_response_at
);

CREATE INDEX IF NOT EXISTS
idx_master_response_times_master
ON master_response_times (
    master_telegram_id
);
"""


CREATE_USER_ACTIVITY_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS user_activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER,

    telegram_id INTEGER NOT NULL,

    action TEXT NOT NULL,

    entity_type TEXT,

    entity_id INTEGER,

    details TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE SET NULL
);
"""


CREATE_USER_ACTIVITY_LOGS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_user_activity_logs_user_id
ON user_activity_logs(user_id);

CREATE INDEX IF NOT EXISTS idx_user_activity_logs_telegram_id
ON user_activity_logs(telegram_id);

CREATE INDEX IF NOT EXISTS idx_user_activity_logs_created_at
ON user_activity_logs(created_at);

CREATE INDEX IF NOT EXISTS idx_user_activity_logs_entity
ON user_activity_logs(entity_type, entity_id);
"""

CREATE_ORDER_MASTER_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS order_master_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    master_telegram_id INTEGER NOT NULL,

    outcome TEXT NOT NULL
        CHECK (
            outcome IN (
                'selected',
                'claimed',
                'declined',
                'negotiation_failed'
            )
        ),

    reason TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    UNIQUE (order_id, master_telegram_id),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE,

    FOREIGN KEY (master_telegram_id)
        REFERENCES masters(telegram_id)
        ON DELETE CASCADE
);
"""


CREATE_ORDER_MASTER_ATTEMPTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_order_master_attempts_order_id
ON order_master_attempts(order_id);

CREATE INDEX IF NOT EXISTS idx_order_master_attempts_master_id
ON order_master_attempts(master_telegram_id);

CREATE INDEX IF NOT EXISTS idx_order_master_attempts_outcome
ON order_master_attempts(outcome);
"""


CREATE_REFERRALS_TABLE = """
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    referrer_user_id INTEGER NOT NULL,
    referred_user_id INTEGER NOT NULL UNIQUE,

    first_discount_order_id INTEGER UNIQUE,
    first_order_discount_amount INTEGER NOT NULL DEFAULT 0
        CHECK (first_order_discount_amount >= 0),

    reward_order_id INTEGER UNIQUE,
    reward_amount INTEGER NOT NULL DEFAULT 0
        CHECK (reward_amount >= 0),
    reward_granted_at TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    CHECK (referrer_user_id != referred_user_id),

    FOREIGN KEY (referrer_user_id)
        REFERENCES users(id)
        ON DELETE RESTRICT,

    FOREIGN KEY (referred_user_id)
        REFERENCES users(id)
        ON DELETE CASCADE,

    FOREIGN KEY (first_discount_order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL,

    FOREIGN KEY (reward_order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL
);
"""

CREATE_REFERRALS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_referrals_referrer
ON referrals(referrer_user_id);

CREATE INDEX IF NOT EXISTS idx_referrals_referred
ON referrals(referred_user_id);

CREATE INDEX IF NOT EXISTS idx_referrals_reward_order
ON referrals(reward_order_id);
"""

CREATE_BONUS_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS bonus_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,

    amount INTEGER NOT NULL
        CHECK (amount != 0),

    bonus_before INTEGER NOT NULL
        CHECK (bonus_before >= 0),

    bonus_after INTEGER NOT NULL
        CHECK (bonus_after >= 0),

    transaction_type TEXT NOT NULL
        CHECK (
            transaction_type IN (
                'referral_reward',
                'payment',
                'refund'
            )
        ),

    order_id INTEGER,
    related_user_id INTEGER,
    comment TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE RESTRICT,

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL,

    FOREIGN KEY (related_user_id)
        REFERENCES users(id)
        ON DELETE SET NULL
);
"""

CREATE_BONUS_TRANSACTIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_bonus_transactions_user
ON bonus_transactions(user_id);

CREATE INDEX IF NOT EXISTS idx_bonus_transactions_order
ON bonus_transactions(order_id);

CREATE INDEX IF NOT EXISTS idx_bonus_transactions_created_at
ON bonus_transactions(created_at);
"""


CREATE_BONUS_GRANTS_TABLE = """
CREATE TABLE IF NOT EXISTS bonus_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,

    amount INTEGER NOT NULL
        CHECK (amount > 0),

    remaining_amount INTEGER NOT NULL
        CHECK (remaining_amount >= 0 AND remaining_amount <= amount),

    source_type TEXT NOT NULL
        CHECK (source_type IN ('referral_reward', 'legacy_balance')),

    source_order_id INTEGER,
    related_user_id INTEGER,

    granted_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    expires_at TEXT NOT NULL,
    reminder_sent_at TEXT,
    expired_at TEXT,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE RESTRICT,

    FOREIGN KEY (source_order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL,

    FOREIGN KEY (related_user_id)
        REFERENCES users(id)
        ON DELETE SET NULL
);
"""

CREATE_BONUS_GRANTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_bonus_grants_user
ON bonus_grants(user_id);

CREATE INDEX IF NOT EXISTS idx_bonus_grants_expiry
ON bonus_grants(expires_at);

CREATE INDEX IF NOT EXISTS idx_bonus_grants_source_order
ON bonus_grants(source_order_id);
"""


CREATE_BONUS_USAGE_ALLOCATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS bonus_usage_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,
    grant_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),

    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    restored_at TEXT,

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE,

    FOREIGN KEY (grant_id)
        REFERENCES bonus_grants(id)
        ON DELETE RESTRICT
);
"""

CREATE_BONUS_USAGE_ALLOCATIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_bonus_usage_order
ON bonus_usage_allocations(order_id);

CREATE INDEX IF NOT EXISTS idx_bonus_usage_grant
ON bonus_usage_allocations(grant_id);
"""

# Durable idempotency registry for operations that must run at most
# once per Telegram event (FIN-01 admin balance commands) and for
# order draft confirmation (ORD-01).
CREATE_OPERATION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS operation_log (
    op_key TEXT PRIMARY KEY,
    op_kind TEXT NOT NULL,
    content_hash TEXT,
    result_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
);
"""

CREATE_OPERATION_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_operation_log_created
ON operation_log(created_at);
"""

# REL-01: durable checkpoint for forum-topic publication lifecycle.
# Written immediately after a known topic ID is received, BEFORE the
# card/attachments/masters notifications are sent. `state` records the
# exact progress so retries can safely resume unfinished publication.
CREATE_ORDER_TOPIC_CHECKPOINTS_TABLE = """
CREATE TABLE IF NOT EXISTS order_topic_checkpoints (
    order_id INTEGER PRIMARY KEY,
    assignment_generation INTEGER NOT NULL DEFAULT -1,
    group_chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('created', 'publishing', 'published', 'failed')),
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),

    FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE
);
"""

# ARC-01: durable журнал задач скачивания вложений архива.
# Это планировщик/очередь (не второй источник истины архива): содержательная
# запись остаётся в chat_archive_attachments, а здесь отслеживается только
# прогресс дорогой операции скачивания/хеширования. UNIQUE(attachment_id)
# гарантирует, что на одно вложение приходится не более одной активной задачи.
CREATE_ARCHIVE_DOWNLOAD_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS chat_archive_download_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL
        REFERENCES chat_archive_attachments(id)
        ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'in_progress',
            'completed',
            'retryable_failure',
            'permanent_failure'
        )),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    claimed_at TEXT,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    reserved_bytes INTEGER NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    content_size INTEGER,
    relative_path TEXT,

    UNIQUE(attachment_id)
);
"""
CREATE_ARCHIVE_DOWNLOAD_JOBS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_download_jobs_status
ON chat_archive_download_jobs(status, created_at);
"""
