import { index, integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const moderationEvents = sqliteTable(
  "moderation_events",
  {
    eventId: text("event_id").primaryKey(),
    detectedAt: text("detected_at").notNull(),
    detectedTs: integer("detected_ts").notNull(),
    reviewedAt: text("reviewed_at"),
    reviewedTs: integer("reviewed_ts"),
    verdict: text("verdict").notNull(),
    level: text("level").notNull(),
    category: text("category").notNull(),
    provider: text("provider").notNull(),
    source: text("source").notNull(),
    language: text("language").notNull(),
    cardDelivered: integer("card_delivered").notNull(),
    channel: text("channel").notNull(),
    channelGroup: text("channel_group").notNull(),
    updatedAt: integer("updated_at").notNull(),
  },
  (table) => [
    index("idx_events_detected_ts").on(table.detectedTs),
    index("idx_events_verdict_period").on(table.verdict, table.detectedTs),
    index("idx_events_category_period").on(table.category, table.detectedTs),
    index("idx_events_channel_period").on(table.channelGroup, table.detectedTs),
    index("idx_events_provider_period").on(table.provider, table.detectedTs),
  ],
);

export const auditRuns = sqliteTable(
  "audit_runs",
  {
    eventId: text("event_id").primaryKey(),
    createdAt: text("created_at").notNull(),
    createdTs: integer("created_ts").notNull(),
    backend: text("backend").notNull(),
    reviewedMessages: integer("reviewed_messages").notNull(),
    flaggedMessages: integer("flagged_messages").notNull(),
    failedChannels: integer("failed_channels").notNull(),
    targetChannels: integer("target_channels").notNull(),
    updatedAt: integer("updated_at").notNull(),
  },
  (table) => [index("idx_audit_runs_created_ts").on(table.createdTs)],
);

export const operationSnapshots = sqliteTable("operation_snapshots", {
  scope: text("scope").primaryKey(),
  capturedAt: text("captured_at").notNull(),
  capturedTs: integer("captured_ts").notNull(),
  pendingOver24h: integer("pending_over_24h").notNull(),
  pendingOver72h: integer("pending_over_72h").notNull(),
  activeLearningRules: integer("active_learning_rules").notNull(),
  aiRetryQueue: integer("ai_retry_queue").notNull(),
  kpiSyncPending: integer("kpi_sync_pending").notNull(),
  updatedAt: integer("updated_at").notNull(),
});
