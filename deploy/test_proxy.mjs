// Exercise lambda_proxy/index.mjs with fake Function-URL events, in-process.
// The awslambda global and the response stream are the Lambda runtime's, so
// they are reimplemented here exactly as the runtime documents them: the
// handler receives a writable stream, HttpResponseStream.from() stamps status
// and headers onto it, and nothing is buffered.
//
//   FAIRBILL_LOCAL_RUNTIME=http://127.0.0.1:8080/invocations node deploy/test_proxy.mjs
//   AGENT_ARN=arn:... node deploy/test_proxy.mjs        (against the deployed runtime)
import { PassThrough } from "node:stream";

globalThis.awslambda = {
  streamifyResponse: (fn) => fn,
  HttpResponseStream: {
    from(stream, metadata) {
      stream.__meta = metadata;
      return stream;
    },
  },
};

const { handler } = await import("../deploy/lambda_proxy/index.mjs");

function event(method, path, { body = null, headers = {}, query = {} } = {}) {
  return {
    version: "2.0", rawPath: path, queryStringParameters: query,
    headers: { "x-fairbill-session": "proxytest-session", ...headers },
    requestContext: { http: { method, path } },
    body: body == null ? null : JSON.stringify(body), isBase64Encoded: false,
  };
}

async function call(ev, { onChunk = null } = {}) {
  const stream = new PassThrough();
  const chunks = [];
  stream.on("data", (c) => {
    chunks.push(c);
    if (onChunk) onChunk(c);
  });
  const done = new Promise((res) => { stream.on("end", res); stream.on("close", res); });
  await handler(ev, stream, {});
  if (!stream.writableEnded) stream.end();
  await done;
  return { meta: stream.__meta, buf: Buffer.concat(chunks) };
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  ${detail}`);
}

// 1. the page and its assets
let r = await call(event("GET", "/"));
check("GET /", r.meta.statusCode === 200 && r.buf.includes("Fairbill"),
  `${r.meta.statusCode} ${r.meta.headers["Content-Type"]} ${r.buf.length} bytes`);
for (const f of ["/static/app.js", "/static/style.css"]) {
  r = await call(event("GET", f));
  check(`GET ${f}`, r.meta.statusCode === 200 && r.buf.length > 100,
    `${r.meta.headers["Content-Type"]} ${r.buf.length} bytes`);
}
r = await call(event("GET", "/gallery/bill_02_photo.jpg"));
check("GET /gallery/bill_02_photo.jpg", r.meta.statusCode === 200 && r.buf.length > 10000,
  `${r.meta.headers["Content-Type"]} ${r.buf.length} bytes`);
r = await call(event("GET", "/gallery/../prebaked/bills.json"));
check("path traversal blocked", r.meta.statusCode === 404 || r.buf.length === 0, `${r.meta.statusCode}`);

// 2. pre-baked read-only API
r = await call(event("GET", "/api/bills"));
const bills = JSON.parse(r.buf.toString());
check("GET /api/bills", Array.isArray(bills) && bills.length > 0, `${bills.length} bills`);
r = await call(event("GET", "/api/bills/bill_02/truth-crops"));
const crops = JSON.parse(r.buf.toString());
check("GET /api/bills/bill_02/truth-crops", crops.bill_id === "bill_02",
  `${(crops.lines || []).length} line crops`);
r = await call(event("GET", "/api/bench"));
const bench = JSON.parse(r.buf.toString());
check("GET /api/bench", bench.mode === "runtime" && bench.reader.total > 0,
  `mode=${bench.mode} reader ${bench.reader.exact}/${bench.reader.total} audit ${bench.audit.matched}/${bench.audit.planted}`);
r = await call(event("GET", "/api/health"));
const health = JSON.parse(r.buf.toString());
check("GET /api/health", health.ok === true && health.mode === "runtime", JSON.stringify(health));

// 3. streamed run: frames must arrive one at a time, not in one blob at the end
const arrivals = [];
const t0 = Date.now();
r = await call(event("POST", "/api/run/bill_02", { query: { cached: "1" } }),
  { onChunk: () => arrivals.push(Date.now() - t0) });
const frames = r.buf.toString().split("\n\n").filter((s) => s.trim().startsWith("data:"));
const steps = frames.map((f) => JSON.parse(f.trim().slice(5)));
check("POST /api/run/bill_02 (SSE)",
  r.meta.headers["Content-Type"] === "text/event-stream" && steps.length >= 5,
  `${steps.length} events in ${arrivals.length} chunks: ` + steps.map((s) => `${s.step}/${s.status}`).join(" "));

// 4. runtime-backed JSON routes
r = await call(event("POST", "/api/decide/bill_02", { body: { option_id: "dispute" } }));
const decided = JSON.parse(r.buf.toString());
check("POST /api/decide/bill_02", decided.ics_url === "/api/ics/bill_02" && decided.ics === undefined,
  `keys=${Object.keys(decided).join(",")}`);
r = await call(event("GET", "/api/ics/bill_02"));
check("GET /api/ics/bill_02",
  r.meta.headers["Content-Type"] === "text/calendar" && r.buf.toString().startsWith("BEGIN:VCALENDAR"),
  `${r.meta.headers["Content-Disposition"]} ${r.buf.length} bytes`);
r = await call(event("GET", "/api/ledger"));
const led = JSON.parse(r.buf.toString());
check("GET /api/ledger", Array.isArray(led.entries), `${(led.entries || []).length} entries`);
const undoable = (led.entries || []).find((e) => e.undo && !e.undone);
if (undoable) {
  r = await call(event("POST", `/api/undo/${undoable.id}`));
  const reversal = JSON.parse(r.buf.toString());
  // ledger.undo() marks the row and returns the reversal row it appended, so
  // the proof is in the refreshed ledger, which is also what the page reads.
  const after = JSON.parse((await call(event("GET", "/api/ledger"))).buf.toString());
  const marked = (after.entries || []).find((e) => e.id === undoable.id);
  check("POST /api/undo/<id>", Boolean(marked && marked.undone) && reversal.action.startsWith("undo"),
    `reversal=${reversal.action} original ${undoable.id} undone=${marked && marked.undone}`);
}
r = await call(event("POST", "/api/chat/bill_02", { body: { text: "just pay it" } }));
const chat = JSON.parse(r.buf.toString());
const denied = (chat.tool_calls || []).some((t) => t.status === "denied");
check("POST /api/chat/bill_02 (guard)", Boolean(chat.reply) && denied,
  `tool_calls=${JSON.stringify(chat.tool_calls || [])}`.slice(0, 220));

r = await call(event("GET", "/api/nope"));
check("unknown path -> 404", r.meta.statusCode === 404, `${r.meta.statusCode}`);

const bad = results.filter((x) => !x.ok);
console.log(`\n${results.length - bad.length}/${results.length} checks passed`);
process.exit(bad.length ? 1 : 0);
