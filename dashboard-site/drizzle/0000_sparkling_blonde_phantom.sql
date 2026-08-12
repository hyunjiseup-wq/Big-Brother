CREATE TABLE `audit_runs` (
	`event_id` text PRIMARY KEY NOT NULL,
	`created_at` text NOT NULL,
	`created_ts` integer NOT NULL,
	`backend` text NOT NULL,
	`reviewed_messages` integer NOT NULL,
	`flagged_messages` integer NOT NULL,
	`failed_channels` integer NOT NULL,
	`target_channels` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_audit_runs_created_ts` ON `audit_runs` (`created_ts`);--> statement-breakpoint
CREATE TABLE `moderation_events` (
	`event_id` text PRIMARY KEY NOT NULL,
	`detected_at` text NOT NULL,
	`detected_ts` integer NOT NULL,
	`reviewed_at` text,
	`reviewed_ts` integer,
	`verdict` text NOT NULL,
	`level` text NOT NULL,
	`category` text NOT NULL,
	`provider` text NOT NULL,
	`source` text NOT NULL,
	`language` text NOT NULL,
	`card_delivered` integer NOT NULL,
	`channel` text NOT NULL,
	`channel_group` text NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_events_detected_ts` ON `moderation_events` (`detected_ts`);--> statement-breakpoint
CREATE INDEX `idx_events_verdict_period` ON `moderation_events` (`verdict`,`detected_ts`);--> statement-breakpoint
CREATE INDEX `idx_events_category_period` ON `moderation_events` (`category`,`detected_ts`);--> statement-breakpoint
CREATE INDEX `idx_events_channel_period` ON `moderation_events` (`channel_group`,`detected_ts`);--> statement-breakpoint
CREATE INDEX `idx_events_provider_period` ON `moderation_events` (`provider`,`detected_ts`);--> statement-breakpoint
CREATE TABLE `operation_snapshots` (
	`scope` text PRIMARY KEY NOT NULL,
	`captured_at` text NOT NULL,
	`captured_ts` integer NOT NULL,
	`pending_over_24h` integer NOT NULL,
	`pending_over_72h` integer NOT NULL,
	`active_learning_rules` integer NOT NULL,
	`ai_retry_queue` integer NOT NULL,
	`kpi_sync_pending` integer NOT NULL,
	`updated_at` integer NOT NULL
);
