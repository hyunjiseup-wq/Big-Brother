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
  return {
    prepare(sql) {
      return {
        sql,
        args: [],
        bind(...args) {
          this.args = args;
          return this;
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

test("server-renders the finished Korean KPI dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html lang="ko"/i);
  assert.match(html, /<title>BB봇 운영 인사이트<\/title>/i);
  assert.match(html, /감지의 양보다/);
  assert.match(html, /판단의 질/);
  assert.match(html, /조회 단위/);
  assert.match(html, /월별/);
  assert.match(html, /분기별/);
  assert.match(html, /연간/);
  assert.match(html, /선택 기간/);
  assert.match(html, /운영 지표를 정리하고 있습니다/);
  assert.match(html, /정탐·오탐/);
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
  assert.doesNotMatch(worker, /user_id|message_content|message_id/);
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
  const response = await worker.fetch(
    new Request("http://localhost/api/dashboard?view=month&year=2025&month=2"),
    { DB: dashboardDatabase() },
    { waitUntil() {}, passThroughOnException() {} },
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
});
