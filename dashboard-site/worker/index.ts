import {
  DEFAULT_DEVICE_SIZES,
  DEFAULT_IMAGE_SIZES,
  handleImageOptimization,
} from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  KPI_INGEST_TOKEN?: string;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

type JsonRecord = Record<string, unknown>;

const SCHEMA_STATEMENTS = [
  `CREATE TABLE IF NOT EXISTS moderation_events (
    event_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL, detected_ts INTEGER NOT NULL,
    reviewed_at TEXT, reviewed_ts INTEGER, verdict TEXT NOT NULL, level TEXT NOT NULL,
    category TEXT NOT NULL, provider TEXT NOT NULL, source TEXT NOT NULL,
    language TEXT NOT NULL, card_delivered INTEGER NOT NULL, channel TEXT NOT NULL,
    channel_group TEXT NOT NULL, updated_at INTEGER NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS idx_events_detected_ts ON moderation_events(detected_ts)",
  "CREATE INDEX IF NOT EXISTS idx_events_verdict_period ON moderation_events(verdict, detected_ts)",
  "CREATE INDEX IF NOT EXISTS idx_events_category_period ON moderation_events(category, detected_ts)",
  "CREATE INDEX IF NOT EXISTS idx_events_channel_period ON moderation_events(channel_group, detected_ts)",
  "CREATE INDEX IF NOT EXISTS idx_events_provider_period ON moderation_events(provider, detected_ts)",
  `CREATE TABLE IF NOT EXISTS audit_runs (
    event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, created_ts INTEGER NOT NULL,
    backend TEXT NOT NULL, reviewed_messages INTEGER NOT NULL, flagged_messages INTEGER NOT NULL,
    failed_channels INTEGER NOT NULL, target_channels INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS idx_audit_runs_created_ts ON audit_runs(created_ts)",
  `CREATE TABLE IF NOT EXISTS operation_snapshots (
    scope TEXT PRIMARY KEY, captured_at TEXT NOT NULL, captured_ts INTEGER NOT NULL,
    pending_over_24h INTEGER NOT NULL, pending_over_72h INTEGER NOT NULL,
    active_learning_rules INTEGER NOT NULL, ai_retry_queue INTEGER NOT NULL,
    kpi_sync_pending INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
] as const;

let schemaReady = false;

async function ensureSchema(db: D1Database) {
  if (schemaReady) return;
  await db.batch(SCHEMA_STATEMENTS.map((sql) => db.prepare(sql)));
  schemaReady = true;
}

function json(data: unknown, status = 200) {
  return Response.json(data, {
    status,
    headers: {
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

function safeString(value: unknown, maxLength = 120) {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function safeCount(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
}

function timestamp(value: unknown) {
  const parsed = Date.parse(safeString(value, 64));
  return Number.isFinite(parsed) ? parsed : 0;
}

function list(value: unknown, limit: number): JsonRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is JsonRecord => Boolean(item) && typeof item === "object").slice(0, limit)
    : [];
}

async function ingest(request: Request, env: Env) {
  if (!env.KPI_INGEST_TOKEN) return json({ error: "ingest_not_configured" }, 503);
  const authorization = request.headers.get("authorization") ?? "";
  if (authorization !== `Bearer ${env.KPI_INGEST_TOKEN}`) {
    return json({ error: "unauthorized" }, 401);
  }

  let payload: JsonRecord;
  try {
    payload = (await request.json()) as JsonRecord;
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  if (payload.schema_version !== 1) return json({ error: "unsupported_schema" }, 400);

  await ensureSchema(env.DB);
  const now = Date.now();
  const statements: D1PreparedStatement[] = [];

  for (const event of list(payload.events, 250)) {
    const eventId = safeString(event.event_id, 96);
    const detectedAt = safeString(event.detected_at, 64);
    const detectedTs = timestamp(detectedAt);
    if (!eventId || !detectedTs) continue;
    const reviewedAt = safeString(event.reviewed_at, 64) || null;
    statements.push(env.DB.prepare(
      `INSERT INTO moderation_events (
        event_id, detected_at, detected_ts, reviewed_at, reviewed_ts, verdict, level,
        category, provider, source, language, card_delivered, channel, channel_group, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET
        detected_at=excluded.detected_at, detected_ts=excluded.detected_ts,
        reviewed_at=excluded.reviewed_at, reviewed_ts=excluded.reviewed_ts,
        verdict=excluded.verdict, level=excluded.level, category=excluded.category,
        provider=excluded.provider, source=excluded.source, language=excluded.language,
        card_delivered=excluded.card_delivered, channel=excluded.channel,
        channel_group=excluded.channel_group, updated_at=excluded.updated_at`,
    ).bind(
      eventId, detectedAt, detectedTs, reviewedAt, reviewedAt ? timestamp(reviewedAt) : null,
      safeString(event.verdict, 32) || "pending", safeString(event.level, 32) || "UNKNOWN",
      safeString(event.category, 64) || "other_complex", safeString(event.provider, 64) || "unknown",
      safeString(event.source, 32) || "unknown", safeString(event.language, 32) || "und",
      event.card_delivered ? 1 : 0, safeString(event.channel, 120) || "이름 미확인 채널",
      safeString(event.channel_group, 120) || "이름 미확인 채널", now,
    ));
  }

  for (const run of list(payload.audit_runs, 100)) {
    const eventId = safeString(run.event_id, 96);
    const createdAt = safeString(run.created_at, 64);
    const createdTs = timestamp(createdAt);
    if (!eventId || !createdTs) continue;
    statements.push(env.DB.prepare(
      `INSERT INTO audit_runs (
        event_id, created_at, created_ts, backend, reviewed_messages, flagged_messages,
        failed_channels, target_channels, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET
        created_at=excluded.created_at, created_ts=excluded.created_ts, backend=excluded.backend,
        reviewed_messages=excluded.reviewed_messages, flagged_messages=excluded.flagged_messages,
        failed_channels=excluded.failed_channels, target_channels=excluded.target_channels,
        updated_at=excluded.updated_at`,
    ).bind(
      eventId, createdAt, createdTs, safeString(run.backend, 64) || "unknown",
      safeCount(run.reviewed_messages), safeCount(run.flagged_messages),
      safeCount(run.failed_channels), safeCount(run.target_channels), now,
    ));
  }

  for (const state of list(payload.operations, 10)) {
    const scope = safeString(state.scope, 64);
    const capturedAt = safeString(state.captured_at, 64);
    const capturedTs = timestamp(capturedAt);
    if (!scope || !capturedTs) continue;
    statements.push(env.DB.prepare(
      `INSERT INTO operation_snapshots (
        scope, captured_at, captured_ts, pending_over_24h, pending_over_72h,
        active_learning_rules, ai_retry_queue, kpi_sync_pending, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(scope) DO UPDATE SET
        captured_at=excluded.captured_at, captured_ts=excluded.captured_ts,
        pending_over_24h=excluded.pending_over_24h, pending_over_72h=excluded.pending_over_72h,
        active_learning_rules=excluded.active_learning_rules, ai_retry_queue=excluded.ai_retry_queue,
        kpi_sync_pending=excluded.kpi_sync_pending, updated_at=excluded.updated_at`,
    ).bind(
      scope, capturedAt, capturedTs, safeCount(state.pending_over_24h),
      safeCount(state.pending_over_72h), safeCount(state.active_learning_rules),
      safeCount(state.ai_retry_queue), safeCount(state.kpi_sync_pending), now,
    ));
  }

  if (statements.length) await env.DB.batch(statements);
  return json({ accepted: statements.length });
}

function rangeBounds(range: string) {
  const kstOffsetMs = 9 * 60 * 60 * 1000;
  const nowInKst = new Date(Date.now() + kstOffsetMs);
  const year = nowInKst.getUTCFullYear();
  const month = nowInKst.getUTCMonth();
  const kstBoundary = (boundaryYear: number, boundaryMonth: number) =>
    new Date(Date.UTC(boundaryYear, boundaryMonth, 1) - kstOffsetMs);
  let start = kstBoundary(year, month);
  let end = kstBoundary(year, month + 1);
  if (range === "previous-month") {
    start = kstBoundary(year, month - 1);
    end = kstBoundary(year, month);
  } else if (range === "current-quarter") {
    start = kstBoundary(year, Math.floor(month / 3) * 3);
    end = kstBoundary(year, Math.floor(month / 3) * 3 + 3);
  } else if (range === "current-year") {
    start = kstBoundary(year, 0);
    end = kstBoundary(year + 1, 0);
  } else if (range === "all") {
    start = new Date(0);
    end = kstBoundary(year + 1, 0);
  }
  return { start: start.getTime(), end: end.getTime() };
}

async function dashboard(request: Request, env: Env) {
  await ensureSchema(env.DB);
  const range = new URL(request.url).searchParams.get("range") ?? "current-month";
  const supported = new Set(["current-month", "previous-month", "current-quarter", "current-year", "all"]);
  const selected = supported.has(range) ? range : "current-month";
  const { start, end } = rangeBounds(selected);
  const periodArgs = [start, end] as const;

  const results = await env.DB.batch([
    env.DB.prepare(`SELECT COUNT(*) detected,
      SUM(verdict='confirmed') confirmed, SUM(verdict='false_positive') false_positive,
      SUM(verdict IN ('pending','processing')) pending, SUM(card_delivered=0) delivery_failed,
      AVG(CASE WHEN reviewed_ts IS NOT NULL THEN (reviewed_ts-detected_ts)/3600000.0 END) avg_review_hours
      FROM moderation_events WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded'`).bind(...periodArgs),
    env.DB.prepare(`SELECT category label, COUNT(*) count,
      SUM(verdict='confirmed') confirmed, SUM(verdict='false_positive') false_positive,
      SUM(verdict IN ('pending','processing')) pending
      FROM moderation_events WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded'
      GROUP BY category ORDER BY count DESC LIMIT 12`).bind(...periodArgs),
    env.DB.prepare(`SELECT channel_group label, COUNT(*) count,
      SUM(verdict='confirmed') confirmed, SUM(verdict='false_positive') false_positive,
      SUM(verdict IN ('pending','processing')) pending
      FROM moderation_events WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded'
      GROUP BY channel_group ORDER BY count DESC LIMIT 15`).bind(...periodArgs),
    env.DB.prepare(`SELECT provider label, COUNT(*) count,
      SUM(verdict='confirmed') confirmed, SUM(verdict='false_positive') false_positive,
      SUM(verdict IN ('pending','processing')) pending
      FROM moderation_events WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded'
      GROUP BY provider ORDER BY count DESC`).bind(...periodArgs),
    env.DB.prepare(`SELECT level label, COUNT(*) count FROM moderation_events
      WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded' GROUP BY level ORDER BY count DESC`).bind(...periodArgs),
    env.DB.prepare(`SELECT language label, COUNT(*) count FROM moderation_events
      WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded' GROUP BY language ORDER BY count DESC`).bind(...periodArgs),
    env.DB.prepare(`SELECT strftime('%Y-%m', detected_ts/1000, 'unixepoch', '+9 hours') label,
      COUNT(*) count, SUM(verdict='confirmed') confirmed,
      SUM(verdict='false_positive') false_positive,
      SUM(verdict IN ('pending','processing')) pending
      FROM moderation_events
      WHERE detected_ts>=? AND verdict<>'superseded' GROUP BY label ORDER BY label DESC LIMIT 12`).bind(Date.now() - 370 * 86400000),
    env.DB.prepare(`SELECT COUNT(*) runs, COALESCE(SUM(reviewed_messages),0) reviewed_messages,
      COALESCE(SUM(flagged_messages),0) flagged_messages,
      COALESCE(SUM(failed_channels),0) failed_channels,
      COALESCE(SUM(target_channels),0) target_channels
      FROM audit_runs WHERE created_ts>=? AND created_ts<?`).bind(...periodArgs),
    env.DB.prepare(`SELECT captured_at, captured_ts, pending_over_24h, pending_over_72h,
      active_learning_rules, ai_retry_queue, kpi_sync_pending
      FROM operation_snapshots ORDER BY captured_ts DESC LIMIT 1`),
    env.DB.prepare(`SELECT MAX(updated_at) updated_at FROM (
      SELECT updated_at FROM moderation_events UNION ALL
      SELECT updated_at FROM audit_runs UNION ALL SELECT updated_at FROM operation_snapshots
    )`),
  ]);

  const rows = results.map((result) => result.results ?? []);
  const total = (rows[0][0] ?? {}) as JsonRecord;
  const detected = safeCount(total.detected);
  const confirmed = safeCount(total.confirmed);
  const falsePositive = safeCount(total.false_positive);
  const resolved = confirmed + falsePositive;
  const audit = (rows[7][0] ?? {}) as JsonRecord;
  const auditReviewed = safeCount(audit.reviewed_messages);
  const auditFlagged = safeCount(audit.flagged_messages);
  const targetChannels = safeCount(audit.target_channels);
  const failedChannels = safeCount(audit.failed_channels);
  const operations = (rows[8][0] ?? null) as JsonRecord | null;
  const lastSyncTs = operations ? safeCount(operations.captured_ts) : 0;
  const ageSeconds = lastSyncTs
    ? Math.max(0, Math.floor((Date.now() - lastSyncTs) / 1000))
    : null;
  const freshnessStatus = ageSeconds == null
    ? "no_data"
    : ageSeconds <= 12 * 60
      ? "live"
      : ageSeconds <= 30 * 60
        ? "delayed"
        : "stored";

  return json({
    range: selected,
    period: { start, end },
    cards: {
      detected,
      confirmed,
      false_positive: falsePositive,
      pending: safeCount(total.pending),
      delivery_failed: safeCount(total.delivery_failed),
      resolved,
      precision_percent: resolved ? Math.round((confirmed / resolved) * 1000) / 10 : null,
      false_positive_percent: resolved ? Math.round((falsePositive / resolved) * 1000) / 10 : null,
      resolution_percent: detected ? Math.round((resolved / detected) * 1000) / 10 : null,
      avg_review_hours: total.avg_review_hours == null ? null : Math.round(Number(total.avg_review_hours) * 10) / 10,
    },
    categories: rows[1],
    channels: rows[2],
    providers: rows[3],
    levels: rows[4],
    languages: rows[5],
    monthly: rows[6].reverse(),
    audit: {
      runs: safeCount(audit.runs),
      reviewed_messages: auditReviewed,
      flagged_messages: auditFlagged,
      flag_rate_percent: auditReviewed ? Math.round((auditFlagged / auditReviewed) * 1000) / 10 : null,
      failed_channels: failedChannels,
      target_channels: targetChannels,
      successful_channel_percent: targetChannels
        ? Math.round((Math.max(0, targetChannels - failedChannels) / targetChannels) * 1000) / 10
        : null,
    },
    operations,
    freshness: {
      status: freshnessStatus,
      last_sync_at: operations?.captured_at ?? null,
      last_sync_ts: lastSyncTs || null,
      age_seconds: ageSeconds,
      expected_interval_seconds: 300,
    },
    updated_at: (rows[9][0] as JsonRecord | undefined)?.updated_at ?? null,
  });
}

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/api/ingest" && request.method === "POST") {
      return ingest(request, env);
    }
    if (url.pathname === "/api/dashboard" && request.method === "GET") {
      return dashboard(request, env);
    }
    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }
    return handler.fetch(request, env, ctx);
  },
};

export default worker;
