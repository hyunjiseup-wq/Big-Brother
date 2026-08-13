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
  STAFF_DASHBOARD_PASSWORD?: string;
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
  `CREATE TABLE IF NOT EXISTS sanction_records (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, user_display TEXT NOT NULL,
    action_type TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL, issued_at TEXT NOT NULL, issued_ts INTEGER NOT NULL,
    expires_at TEXT, expires_ts INTEGER, released_at TEXT, released_ts INTEGER,
    issued_by_id TEXT, issued_by_display TEXT, released_by_id TEXT,
    released_by_display TEXT, release_reason TEXT, updated_at INTEGER NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS idx_sanctions_issued_ts ON sanction_records(issued_ts)",
  "CREATE INDEX IF NOT EXISTS idx_sanctions_status_issued ON sanction_records(status, issued_ts)",
  "CREATE INDEX IF NOT EXISTS idx_sanctions_user_issued ON sanction_records(user_id, issued_ts)",
  `CREATE TABLE IF NOT EXISTS staff_login_attempts (
    fingerprint TEXT PRIMARY KEY, failures INTEGER NOT NULL,
    locked_until INTEGER NOT NULL, updated_at INTEGER NOT NULL
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

const STAFF_COOKIE = "bb_staff_session";
const STAFF_SESSION_SECONDS = 8 * 60 * 60;
const LOGIN_LOCK_SECONDS = 15 * 60;
const textEncoder = new TextEncoder();

function base64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

async function hmac(secret: string, value: string) {
  const key = await crypto.subtle.importKey(
    "raw", textEncoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, textEncoder.encode(value)));
}

async function safePasswordEqual(left: string, right: string) {
  const [leftHash, rightHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", textEncoder.encode(left)),
    crypto.subtle.digest("SHA-256", textEncoder.encode(right)),
  ]);
  const a = new Uint8Array(leftHash);
  const b = new Uint8Array(rightHash);
  let difference = a.length ^ b.length;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

async function createStaffSession(secret: string) {
  const payload = base64Url(textEncoder.encode(JSON.stringify({
    exp: Math.floor(Date.now() / 1000) + STAFF_SESSION_SECONDS,
  })));
  return `${payload}.${base64Url(await hmac(secret, `session:${payload}`))}`;
}

async function validStaffSession(request: Request, secret: string) {
  const cookie = request.headers.get("cookie") ?? "";
  const raw = cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${STAFF_COOKIE}=`));
  const token = raw?.slice(STAFF_COOKIE.length + 1) ?? "";
  const [payload, signature] = token.split(".");
  if (!payload || !signature) return false;
  const expected = base64Url(await hmac(secret, `session:${payload}`));
  if (!(await safePasswordEqual(signature, expected))) return false;
  try {
    const normalized = payload.replaceAll("-", "+").replaceAll("_", "/");
    const parsed = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="))) as JsonRecord;
    return safeCount(parsed.exp) > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

async function staffFingerprint(request: Request, secret: string) {
  const address = request.headers.get("cf-connecting-ip") ?? "unknown";
  return base64Url(await hmac(secret, `login:${address}`));
}

async function staffLogin(request: Request, env: Env) {
  if (!env.STAFF_DASHBOARD_PASSWORD || env.STAFF_DASHBOARD_PASSWORD.length < 12) {
    return json({ error: "staff_login_not_configured" }, 503);
  }
  await ensureSchema(env.DB);
  const fingerprint = await staffFingerprint(request, env.STAFF_DASHBOARD_PASSWORD);
  const now = Date.now();
  const attempt = await env.DB.prepare(
    "SELECT failures, locked_until FROM staff_login_attempts WHERE fingerprint = ?",
  ).bind(fingerprint).first<JsonRecord>();
  if (safeCount(attempt?.locked_until) > now) {
    return json({ error: "temporarily_locked", retry_after_seconds: Math.ceil((safeCount(attempt?.locked_until) - now) / 1000) }, 429);
  }

  let body: JsonRecord;
  try {
    body = (await request.json()) as JsonRecord;
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const password = safeString(body.password, 256);
  if (!password || !(await safePasswordEqual(password, env.STAFF_DASHBOARD_PASSWORD))) {
    const failures = safeCount(attempt?.failures) + 1;
    const lockedUntil = failures >= 5 ? now + LOGIN_LOCK_SECONDS * 1000 : 0;
    await env.DB.prepare(
      `INSERT INTO staff_login_attempts (fingerprint, failures, locked_until, updated_at)
       VALUES (?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET
       failures=excluded.failures, locked_until=excluded.locked_until, updated_at=excluded.updated_at`,
    ).bind(fingerprint, failures, lockedUntil, now).run();
    return json({ error: failures >= 5 ? "temporarily_locked" : "invalid_password", attempts_remaining: Math.max(0, 5 - failures) }, failures >= 5 ? 429 : 401);
  }

  await env.DB.prepare("DELETE FROM staff_login_attempts WHERE fingerprint = ?").bind(fingerprint).run();
  const session = await createStaffSession(env.STAFF_DASHBOARD_PASSWORD);
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "set-cookie": `${STAFF_COOKIE}=${session}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${STAFF_SESSION_SECONDS}`,
    },
  });
}

function staffLogout() {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "set-cookie": `${STAFF_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`,
    },
  });
}

async function staffSession(request: Request, env: Env) {
  if (!env.STAFF_DASHBOARD_PASSWORD || env.STAFF_DASHBOARD_PASSWORD.length < 12) {
    return json({ error: "staff_login_not_configured" }, 503);
  }
  if (!(await validStaffSession(request, env.STAFF_DASHBOARD_PASSWORD))) {
    return json({ error: "authentication_required" }, 401);
  }
  return json({ authenticated: true });
}

async function staffSanctions(request: Request, env: Env) {
  if (!env.STAFF_DASHBOARD_PASSWORD || env.STAFF_DASHBOARD_PASSWORD.length < 12) {
    return json({ error: "staff_login_not_configured" }, 503);
  }
  if (!(await validStaffSession(request, env.STAFF_DASHBOARD_PASSWORD))) {
    return json({ error: "authentication_required" }, 401);
  }
  await ensureSchema(env.DB);
  const url = new URL(request.url);
  const status = safeString(url.searchParams.get("status"), 16);
  const action = safeString(url.searchParams.get("action"), 16);
  const query = safeString(url.searchParams.get("q"), 100);
  const limit = Math.max(1, Math.min(200, safeCount(url.searchParams.get("limit")) || 100));
  const clauses: string[] = [];
  const bindings: unknown[] = [];
  if (["active", "released", "expired"].includes(status)) {
    clauses.push("status = ?"); bindings.push(status);
  }
  if (["WARNING", "DELETE", "TIMEOUT", "KICK", "BAN"].includes(action)) {
    clauses.push("action_type = ?"); bindings.push(action);
  }
  if (query) {
    clauses.push("(user_display LIKE ? OR user_id LIKE ? OR reason LIKE ?)");
    const pattern = `%${query}%`;
    bindings.push(pattern, pattern, pattern);
  }
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  const results = await env.DB.prepare(
    `SELECT event_id, user_id, user_display, action_type, reason, source, status,
            issued_at, expires_at, released_at, issued_by_display,
            released_by_display, release_reason
     FROM sanction_records ${where}
     ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, issued_ts DESC LIMIT ?`,
  ).bind(...bindings, limit).all();
  const summary = await env.DB.prepare(
    `SELECT COUNT(*) total, SUM(status='active') active,
            SUM(action_type='WARNING') warnings, SUM(action_type='TIMEOUT') timeouts
     FROM sanction_records`,
  ).first<JsonRecord>();
  return json({ records: results.results ?? [], summary: summary ?? {}, limit });
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
  if (![1, 2].includes(Number(payload.schema_version))) {
    return json({ error: "unsupported_schema" }, 400);
  }

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

  for (const sanction of list(payload.sanctions, 250)) {
    const eventId = safeString(sanction.event_id, 96);
    const issuedAt = safeString(sanction.issued_at, 64);
    const issuedTs = timestamp(issuedAt);
    if (!eventId || !issuedTs) continue;
    const expiresAt = safeString(sanction.expires_at, 64) || null;
    const releasedAt = safeString(sanction.released_at, 64) || null;
    statements.push(env.DB.prepare(
      `INSERT INTO sanction_records (
        event_id, user_id, user_display, action_type, reason, source, status,
        issued_at, issued_ts, expires_at, expires_ts, released_at, released_ts,
        issued_by_id, issued_by_display, released_by_id, released_by_display,
        release_reason, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET
        user_id=excluded.user_id, user_display=excluded.user_display,
        action_type=excluded.action_type, reason=excluded.reason, source=excluded.source,
        status=excluded.status, issued_at=excluded.issued_at, issued_ts=excluded.issued_ts,
        expires_at=excluded.expires_at, expires_ts=excluded.expires_ts,
        released_at=excluded.released_at, released_ts=excluded.released_ts,
        issued_by_id=excluded.issued_by_id, issued_by_display=excluded.issued_by_display,
        released_by_id=excluded.released_by_id,
        released_by_display=excluded.released_by_display,
        release_reason=excluded.release_reason, updated_at=excluded.updated_at`,
    ).bind(
      eventId, safeString(sanction.user_id, 32),
      safeString(sanction.user_display, 120) || "이름 미확인 사용자",
      safeString(sanction.action_type, 32) || "UNKNOWN",
      safeString(sanction.reason, 1000) || "사유 미입력",
      safeString(sanction.source, 32) || "unknown",
      safeString(sanction.status, 16) || "active",
      issuedAt, issuedTs, expiresAt, expiresAt ? timestamp(expiresAt) : null,
      releasedAt, releasedAt ? timestamp(releasedAt) : null,
      safeString(sanction.issued_by_id, 32) || null,
      safeString(sanction.issued_by_display, 120) || null,
      safeString(sanction.released_by_id, 32) || null,
      safeString(sanction.released_by_display, 120) || null,
      safeString(sanction.release_reason, 1000) || null, now,
    ));
  }

  if (statements.length) await env.DB.batch(statements);
  return json({ accepted: statements.length });
}

type PeriodView = "month" | "quarter" | "year" | "all";

type PeriodSelection = {
  view: PeriodView;
  year: number | null;
  month: number | null;
  quarter: number | null;
};

const KST_OFFSET_MS = 9 * 60 * 60 * 1000;

function kstBoundary(year: number, month: number) {
  return Date.UTC(year, month, 1) - KST_OFFSET_MS;
}

function currentKstPeriod() {
  const nowInKst = new Date(Date.now() + KST_OFFSET_MS);
  const year = nowInKst.getUTCFullYear();
  const month = nowInKst.getUTCMonth() + 1;
  return { year, month, quarter: Math.floor((month - 1) / 3) + 1 };
}

function validInteger(value: string | null, minimum: number, maximum: number, fallback: number) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function legacySelection(range: string | null, current: ReturnType<typeof currentKstPeriod>): PeriodSelection | null {
  if (!range) return null;
  if (range === "all") return { view: "all", year: null, month: null, quarter: null };
  if (range === "current-year") return { view: "year", year: current.year, month: null, quarter: null };
  if (range === "current-quarter") {
    return { view: "quarter", year: current.year, month: null, quarter: current.quarter };
  }
  if (range === "previous-month") {
    const previous = new Date(Date.UTC(current.year, current.month - 2, 1));
    return {
      view: "month",
      year: previous.getUTCFullYear(),
      month: previous.getUTCMonth() + 1,
      quarter: null,
    };
  }
  if (range === "current-month") {
    return { view: "month", year: current.year, month: current.month, quarter: null };
  }
  return null;
}

function periodBounds(url: URL) {
  const current = currentKstPeriod();
  const requestedView = url.searchParams.get("view");
  const supportedViews = new Set<PeriodView>(["month", "quarter", "year", "all"]);
  const legacy = legacySelection(url.searchParams.get("range"), current);
  const view = supportedViews.has(requestedView as PeriodView)
    ? requestedView as PeriodView
    : legacy?.view ?? "month";
  const maximumYear = current.year + 1;
  const year = view === "all"
    ? null
    : validInteger(url.searchParams.get("year"), 2000, maximumYear, legacy?.year ?? current.year);
  const month = view === "month"
    ? validInteger(url.searchParams.get("month"), 1, 12, legacy?.month ?? current.month)
    : null;
  const quarter = view === "quarter"
    ? validInteger(url.searchParams.get("quarter"), 1, 4, legacy?.quarter ?? current.quarter)
    : null;

  let start = 0;
  let end = kstBoundary(current.year + 1, 0);
  let label = "전체 기간";
  if (view === "month" && year && month) {
    start = kstBoundary(year, month - 1);
    end = kstBoundary(year, month);
    label = `${year}년 ${month}월`;
  } else if (view === "quarter" && year && quarter) {
    start = kstBoundary(year, (quarter - 1) * 3);
    end = kstBoundary(year, quarter * 3);
    label = `${year}년 ${quarter}분기`;
  } else if (view === "year" && year) {
    start = kstBoundary(year, 0);
    end = kstBoundary(year + 1, 0);
    label = `${year}년`;
  }

  return {
    selection: { view, year, month, quarter } satisfies PeriodSelection,
    start,
    end,
    label,
  };
}

async function dashboard(request: Request, env: Env) {
  await ensureSchema(env.DB);
  const { selection, start, end, label } = periodBounds(new URL(request.url));
  const periodArgs = [start, end] as const;
  const trendStart = Math.max(0, end - 370 * 86400000);

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
      WHERE detected_ts>=? AND detected_ts<? AND verdict<>'superseded'
      GROUP BY label ORDER BY label DESC LIMIT 12`).bind(trendStart, end),
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
    env.DB.prepare(`SELECT label FROM (
      SELECT DISTINCT strftime('%Y-%m', detected_ts/1000, 'unixepoch', '+9 hours') label
      FROM moderation_events WHERE verdict<>'superseded'
      UNION
      SELECT DISTINCT strftime('%Y-%m', created_ts/1000, 'unixepoch', '+9 hours') label
      FROM audit_runs
    ) WHERE label IS NOT NULL ORDER BY label DESC`),
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
  const availableMonths = rows[10]
    .map((row) => String((row as JsonRecord).label ?? ""))
    .filter((value) => /^\d{4}-\d{2}$/.test(value));
  const availableYears = [...new Set(availableMonths.map((value) => Number(value.slice(0, 4))))];
  const availableQuarters = [...new Set(availableMonths.map((value) => {
    const month = Number(value.slice(5, 7));
    return `${value.slice(0, 4)}-Q${Math.floor((month - 1) / 3) + 1}`;
  }))];

  return json({
    range: selection.view,
    period: { ...selection, start, end, label },
    available_periods: {
      years: availableYears,
      months: availableMonths,
      quarters: availableQuarters,
    },
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
    if (url.pathname === "/api/staff/login" && request.method === "POST") {
      return staffLogin(request, env);
    }
    if (url.pathname === "/api/staff/logout" && request.method === "POST") {
      return staffLogout();
    }
    if (url.pathname === "/api/staff/session" && request.method === "GET") {
      return staffSession(request, env);
    }
    if (url.pathname === "/api/staff/sanctions" && request.method === "GET") {
      return staffSanctions(request, env);
    }
    if (url.pathname === "/api/dashboard" && request.method === "GET") {
      const session = await staffSession(request, env);
      if (!session.ok) return session;
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
