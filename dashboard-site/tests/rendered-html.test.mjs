import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

function dashboardDatabase() {
  const state = { attempts: null };
  return {
    prepare(sql) {
      return {
        sql,
        args: [],
        bind(...args) {
          this.args = args;
          return this;
        },
        async first() {
          if (sql.includes("SELECT failures")) return state.attempts;
          return null;
        },
        async run() {
          if (sql.startsWith("DELETE FROM staff_login_attempts")) state.attempts = null;
          if (sql.includes("INSERT INTO staff_login_attempts")) {
            state.attempts = { failures: this.args[1], locked_until: this.args[2] };
          }
          return { success: true };
        },
      };
    },
    async batch(statements) {
      if (statements.length !== 11) return statements.map(() => ({ results: [] }));
      return [
        { results: [{ detected: 0, confirmed: 0, false_positive: 0, pending: 0, delivery_failed: 0 }] },
        { results: [] },
        { results: [] },
        { results: [] },
        { results: [] },
        { results: [] },
        { results: [] },
        { results: [{ runs: 0, reviewed_messages: 0, flagged_messages: 0, failed_channels: 0, target_channels: 0 }] },
        { results: [] },
        { results: [{ updated_at: null }] },
        { results: [{ label: "2025-02" }, { label: "2024-12" }] },
      ];
    },
  };
}

test("server-renders the private Korean dashboard entry gate", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html lang="ko"/i);
  assert.match(html, /<title>BB봇 운영 인사이트<\/title>/i);
  assert.match(html, /ADMINISTRATORS ONLY/);
  assert.match(html, /관리자 비밀번호/);
  assert.match(html, /세션 확인 중/);
  assert.doesNotMatch(html, /감지의 양보다|판단의 질|조회 단위/);
  assert.doesNotMatch(html, /Your site is taking shape|codex-preview|react-loading-skeleton/i);
});

test("removes starter assets and keeps the ingest contract private", async () => {
  const [worker, dashboard, packageJson, hosting] = await Promise.all([
    readFile(new URL("../worker/index.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../.openai/hosting.json", import.meta.url), "utf8"),
  ]);

  assert.match(worker, /KPI_INGEST_TOKEN/);
  assert.match(worker, /authorization/);
  assert.match(worker, /\/api\/ingest/);
  assert.match(worker, /\/api\/dashboard/);
  assert.doesNotMatch(worker, /message_content|message_id/);
  assert.match(worker, /validStaffSession/);
  assert.match(worker, /STAFF_DASHBOARD_PASSWORD/);
  assert.match(worker, /staffSession/);
  assert.match(dashboard, /감지 카드/);
  assert.match(dashboard, /정탐 최다 채널/);
  assert.match(dashboard, /오탐 최다 채널/);
  assert.match(dashboard, /배치 감사/);
  assert.match(dashboard, /실시간 동기화/);
  assert.match(dashboard, /동기화 지연/);
  assert.match(dashboard, /저장 데이터 표시/);
  assert.match(dashboard, /미전송 KPI/);
  assert.match(worker, /expected_interval_seconds/);
  assert.match(worker, /captured_ts/);
  assert.match(worker, /available_periods/);
  assert.match(worker, /requestedView/);
  assert.match(dashboard, /selectionQuery/);
  assert.match(dashboard, /selectionFromSearch/);
  assert.match(dashboard, /자료 없음/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.match(hosting, /"d1": "DB"/);

  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
  await assert.rejects(access(new URL("../public/favicon.svg", root)));
});

test("dashboard API applies a selected KST month and exposes available periods", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("api-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const env = { DB: dashboardDatabase(), STAFF_DASHBOARD_PASSWORD: "correct-handoff-password" };
  const context = { waitUntil() {}, passThroughOnException() {} };
  const anonymous = await worker.fetch(
    new Request("http://localhost/api/dashboard?view=month&year=2025&month=2"),
    env,
    context,
  );
  assert.equal(anonymous.status, 401);

  const login = await worker.fetch(new Request("http://localhost/api/staff/login", {
    method: "POST",
    headers: { "content-type": "application/json", "cf-connecting-ip": "127.0.0.9" },
    body: JSON.stringify({ password: "correct-handoff-password" }),
  }), env, context);
  const cookie = (login.headers.get("set-cookie") ?? "").split(";")[0];
  const response = await worker.fetch(
    new Request("http://localhost/api/dashboard?view=month&year=2025&month=2", {
      headers: { cookie },
    }),
    env,
    context,
  );

  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.deepEqual(payload.period, {
    view: "month",
    year: 2025,
    month: 2,
    quarter: null,
    start: Date.UTC(2025, 1, 1) - 9 * 60 * 60 * 1000,
    end: Date.UTC(2025, 2, 1) - 9 * 60 * 60 * 1000,
    label: "2025년 2월",
  });
  assert.deepEqual(payload.available_periods, {
    years: [2025, 2024],
    months: ["2025-02", "2024-12"],
    quarters: ["2025-Q1", "2024-Q4"],
  });
  assert.doesNotMatch(JSON.stringify(payload), /user_id|user_display|reason/);
});

function staffDatabase() {
  const state = { attempts: null };
  return {
    prepare(sql) {
      return {
        sql,
        args: [],
        bind(...args) { this.args = args; return this; },
        async first() {
          if (sql.includes("SELECT failures")) return state.attempts;
          if (sql.includes("SELECT COUNT(*) total")) {
            return { total: 1, active: 1, warnings: 0, timeouts: 1 };
          }
          return null;
        },
        async all() {
          if (!sql.includes("FROM sanction_records")) return { results: [] };
          return { results: [{
            event_id: "sanction-1", user_id: "123", user_display: "테스트 관리자",
            action_type: "TIMEOUT", reason: "인수인계 테스트", source: "manual_command",
            status: "active", issued_at: "2026-08-13T00:00:00Z", expires_at: null,
            released_at: null, issued_by_display: "운영진", released_by_display: null,
            release_reason: null,
          }] };
        },
        async run() {
          if (sql.startsWith("DELETE FROM staff_login_attempts")) state.attempts = null;
          if (sql.includes("INSERT INTO staff_login_attempts")) {
            state.attempts = { failures: this.args[1], locked_until: this.args[2] };
          }
          return { success: true };
        },
      };
    },
    async batch(statements) { return statements.map(() => ({ results: [] })); },
  };
}

test("staff ledger rejects anonymous access and grants an 8-hour secure session", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("staff-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const env = { DB: staffDatabase(), STAFF_DASHBOARD_PASSWORD: "correct-handoff-password" };
  const context = { waitUntil() {}, passThroughOnException() {} };

  const anonymous = await worker.fetch(new Request("http://localhost/api/staff/sanctions"), env, context);
  assert.equal(anonymous.status, 401);

  const login = await worker.fetch(new Request("http://localhost/api/staff/login", {
    method: "POST",
    headers: { "content-type": "application/json", "cf-connecting-ip": "127.0.0.1" },
    body: JSON.stringify({ password: "correct-handoff-password" }),
  }), env, context);
  assert.equal(login.status, 200);
  const setCookie = login.headers.get("set-cookie") ?? "";
  assert.match(setCookie, /bb_staff_session=/);
  assert.match(setCookie, /HttpOnly/i);
  assert.match(setCookie, /Secure/i);
  assert.match(setCookie, /SameSite=Strict/i);
  assert.match(setCookie, /Max-Age=28800/i);

  const cookie = setCookie.split(";")[0];
  const ledger = await worker.fetch(new Request("http://localhost/api/staff/sanctions", {
    headers: { cookie },
  }), env, context);
  assert.equal(ledger.status, 200);
  const payload = await ledger.json();
  assert.equal(payload.records[0].reason, "인수인계 테스트");
  assert.equal(payload.summary.active, 1);
});

test("staff login locks the source for 15 minutes after five failures", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("lock-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const env = { DB: staffDatabase(), STAFF_DASHBOARD_PASSWORD: "correct-handoff-password" };
  const context = { waitUntil() {}, passThroughOnException() {} };
  let response;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    response = await worker.fetch(new Request("http://localhost/api/staff/login", {
      method: "POST",
      headers: { "content-type": "application/json", "cf-connecting-ip": "127.0.0.2" },
      body: JSON.stringify({ password: "wrong-password" }),
    }), env, context);
  }
  assert.equal(response.status, 429);
  const payload = await response.json();
  assert.equal(payload.error, "temporarily_locked");
});
