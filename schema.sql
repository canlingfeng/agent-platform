-- ============================================================
-- agent_platform · 多租户 Agent 服务平台 · 库表初始化
-- 目标版本: MySQL 8.4 LTS
-- 字符集: utf8mb4 (MySQL 的 "utf8" 是 3 字节残废版, 存不了 emoji)
-- ============================================================

CREATE DATABASE IF NOT EXISTS agent_platform
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE agent_platform;
SET time_zone = '+00:00';

-- ---------- 1. 用户表 ----------
CREATE TABLE IF NOT EXISTS users (
    id             bigint unsigned  not null auto_increment comment '内部主键: 聚簇索引, 顺序插入无页分裂',
    uuid           char(36)         not null                comment '对外 ID (UUIDv4): 防枚举 / 防 IDOR',
    username       varchar(255)     not null                comment '登录名, 唯一',
    password_hash  varchar(255)     not null                comment 'bcrypt 哈希串(自带 salt), 绝不明文',
    role           enum('admin', 'user') not null default 'user' comment '用户角色: admin 管理员, user 普通用户',
    status         tinyint unsigned not null default 1      comment '用户状态: 0 禁用, 1 启用',
    created_at     datetime(3)      not null default current_timestamp(3) comment '创建时间',
    updated_at     datetime(3)      not null default current_timestamp(3)
                   on update current_timestamp(3) comment '更新时间',
    last_login_at  datetime(3)      null                    comment '最后登录时间',
    primary key (id),
    unique key uk_users_uuid (uuid),
    unique key uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户表';

-- ---------- 2. 会话表 ----------
CREATE TABLE IF NOT EXISTS sessions (
    id             char(36)        not null                comment '会话 ID (UUIDv4): 同时作为 LangGraph thread_id',
    user_id        bigint unsigned not null                comment '归属用户: 多租户隔离键 (引用 users.id)',
    title          varchar(255)    not null default ''     comment '会话标题',
    summary        text            null                    comment '压缩后的长期记忆',
    created_at     datetime(3)     not null default current_timestamp(3) comment '创建时间',
    updated_at     datetime(3)     not null default current_timestamp(3)
                   on update current_timestamp(3) comment '更新时间',
    deleted_at     datetime(3)     null                    comment '软删除: NULL=未删除',
    primary key (id),
    key idx_sessions_user (user_id, updated_at),
    constraint fk_sessions_user foreign key (user_id) references users (id) on delete cascade
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话表';

-- ---------- 3. 消息表 ----------
-- ⚠️ 【预留未启用】全项目无任何 INSERT INTO messages / FROM messages。
-- 对话历史实际由 LangGraph 的 checkpoints / checkpoint_writes / checkpoint_blobs
-- 三表承载（PyMySQLSaver.setup() 自动建），GET /sessions/{id}/messages 读的是
-- graph.get_state()。本表 DDL 保留是为将来做独立分页/检索时直接接线，
-- 含 seq 序号与 uk_messages_session_seq 唯一键。详见 docs/ARCHIVED.md 第三节。
CREATE TABLE IF NOT EXISTS messages (
    id            bigint unsigned not null auto_increment comment '内部主键',
    session_id    char(36)        not null                comment '所属会话 (引用 sessions.id = thread_id)',
    seq           int unsigned    not null                comment '会话内递增序号: 保证顺序 + 支持分页',
    role          enum('user', 'assistant', 'tool', 'system') not null comment '消息角色: user 用户, assistant 助手, tool 工具, system 系统',
    content       mediumtext      null                    comment '消息正文; MEDIUMTEXT 上限 16MB, TEXT 只有 64KB 会截断',
    tool_name     varchar(255)    null                    comment '工具名: 用于区分不同工具的消息',
    tool_args     json            null                    comment '工具入参; JSON 类型会自动校验格式',
    tool_call_id  varchar(255)    null                    comment '工具调用 ID: 回环时绑定 AIMessage.tool_calls <-> ToolMessage',
    token_count   int unsigned    not null default 0      comment '本条 token 数: 用于压缩判断与计费',
    created_at    datetime(3)     not null default current_timestamp(3) comment '创建时间',
    deleted_at    datetime(3)     null                    comment '软删除: NULL=未删除',
    primary key (id),
    unique key uk_messages_session_seq (session_id, seq),
    constraint fk_messages_session foreign key (session_id) references sessions (id) on delete cascade
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='消息表';

-- ---------- 4. 审计日志表 ----------
CREATE TABLE IF NOT EXISTS audit_logs (
    id           bigint unsigned not null auto_increment comment '内部主键',
    user_id      bigint unsigned null                    comment '可为空: 未登录 / 系统级调用',
    session_id   char(36)        null                    comment '所属会话 (引用 sessions.id = thread_id)',
    tool         varchar(64)     not null                comment '工具名',
    subject      varchar(512)    null                    comment '裁决对象 (permission._subject 的输出)',
    decision     enum('allow', 'deny', 'ask') not null   comment '裁决结果三态',
    client_ip    varchar(45)     null                    comment 'IPv6 最长 45 字符',
    created_at   datetime(3)     not null default current_timestamp(3) comment '裁决时间',
    primary key (id),
    key idx_audit_user_time (user_id, created_at),
    key idx_audit_decision (decision, created_at),
    constraint fk_audit_user foreign key (user_id) references users (id) on delete set null,
    constraint fk_audit_session foreign key (session_id) references sessions (id) on delete set null
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='权限裁决审计表';

-- ---------- 5. 配额配置表 ----------
CREATE TABLE IF NOT EXISTS quotas (
    user_id                  bigint unsigned   not null comment '用户 ID, 同时作为主键 (与用户 1:1)',
    daily_token_limit        int unsigned      not null default 200000 comment '每日 token 上限',
    daily_request_limit      int unsigned      not null default 200    comment '每日请求上限',
    qps_limit                smallint unsigned not null default 2      comment '每秒请求上限',
    concurrent_session_limit smallint unsigned not null default 5      comment '同时活跃会话数上限',
    updated_at               datetime(3)       not null default current_timestamp(3)
                             on update current_timestamp(3) comment '更新时间',
    primary key (user_id),
    constraint fk_quotas_user foreign key (user_id) references users (id) on delete cascade
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户配额表';

-- ---------- 6. 每日用量表 ----------
CREATE TABLE IF NOT EXISTS usage_daily (
    id            bigint unsigned not null auto_increment comment '内部主键',
    user_id       bigint unsigned not null                comment '用户 ID',
    usage_date    date            not null                comment 'UTC 日期',
    used_tokens   bigint unsigned not null default 0      comment '当日累计 token',
    used_requests int unsigned    not null default 0      comment '当日累计请求数',
    tool_calls    int unsigned    not null default 0      comment '当日工具调用次数',
    denied_calls  int unsigned    not null default 0      comment '被权限拦截的次数',
    updated_at    datetime(3)     not null default current_timestamp(3)
                  on update current_timestamp(3) comment '更新时间',
    primary key (id),
    unique key uk_usage_user_date (user_id, usage_date),
    constraint fk_usage_user foreign key (user_id) references users (id) on delete cascade
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='每日用量统计表';

