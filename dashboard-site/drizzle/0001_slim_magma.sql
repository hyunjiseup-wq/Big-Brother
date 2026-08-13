CREATE TABLE `sanction_records` (
	`event_id` text PRIMARY KEY NOT NULL,
	`user_id` text NOT NULL,
	`user_display` text NOT NULL,
	`action_type` text NOT NULL,
	`reason` text NOT NULL,
	`source` text NOT NULL,
	`status` text NOT NULL,
	`issued_at` text NOT NULL,
	`issued_ts` integer NOT NULL,
	`expires_at` text,
	`expires_ts` integer,
	`released_at` text,
	`released_ts` integer,
	`issued_by_id` text,
	`issued_by_display` text,
	`released_by_id` text,
	`released_by_display` text,
	`release_reason` text,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_sanctions_issued_ts` ON `sanction_records` (`issued_ts`);--> statement-breakpoint
CREATE INDEX `idx_sanctions_status_issued` ON `sanction_records` (`status`,`issued_ts`);--> statement-breakpoint
CREATE INDEX `idx_sanctions_user_issued` ON `sanction_records` (`user_id`,`issued_ts`);--> statement-breakpoint
CREATE TABLE `staff_login_attempts` (
	`fingerprint` text PRIMARY KEY NOT NULL,
	`failures` integer NOT NULL,
	`locked_until` integer NOT NULL,
	`updated_at` integer NOT NULL
);
