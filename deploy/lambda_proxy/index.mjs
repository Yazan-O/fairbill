// Fairbill public entrypoint: one Lambda Function URL serves the page, its
// assets, the pre-baked read-only API, and proxies everything else to the
// AgentCore Runtime. /api/run is streamed with awslambda.streamifyResponse so
// the browser sees each pipeline event as it happens (the progress bar during
// the live 39 MB price-file download is the demo's whole point); the Function
// URL must therefore be created with InvokeMode=RESPONSE_STREAM.
import { readFile } from "node:fs/promises";
import { createReadStream, existsSync, statSync } from "node:fs";
import { pipeline } from "node:stream/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from "@aws-sdk/client-bedrock-agentcore";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const AGENT_ARN = process.env.AGENT_ARN;
// Set to a local http://host:port/invocations to exercise this handler against
// a runtime running from the extracted artifact, before any AWS deploy.
const LOCAL_RUNTIME = process.env.FAIRBILL_LOCAL_RUNTIME;
const client = LOCAL_RUNTIME ? null : new BedrockAgentCoreClient({ region: process.env.AWS_REGION || "us-east-1" });

const TYPES = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json", ".png": "image/png",
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf", ".svg": "image/svg+xml" };

const cache = new Map();
async function prebaked(name) {
  if (!cache.has(name)) cache.set(name, JSON.parse(await readFile(path.join(HERE, "prebaked", name), "utf8")));
  return cache.get(name);
}

// InvokeAgentRuntime rejects a runtimeSessionId under 33 characters; the same
// padding as src/fairbill/app.py so a session keeps its ledger across callers.
const sid = (h) => (((h && h.trim()) || "anon") + "-" + "0".repeat(33)).slice(0, 40);

async function invoke(payload, sessionId) {
  if (LOCAL_RUNTIME) {
    const r = await fetch(LOCAL_RUNTIME, { method: "POST", body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json", "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId } });
    return { contentType: r.headers.get("content-type") || "", body: r.body };
  }
  const out = await client.send(new InvokeAgentRuntimeCommand({
    agentRuntimeArn: AGENT_ARN, runtimeSessionId: sessionId,
    contentType: "application/json", accept: "application/json",
    payload: new TextEncoder().encode(JSON.stringify(payload)),
  }));
  return { contentType: out.contentType || "", body: out.response };
}

async function invokeJson(payload, sessionId) {
  const { body } = await invoke(payload, sessionId);
  const chunks = [];
  for await (const c of body) chunks.push(Buffer.from(c));
  const text = Buffer.concat(chunks).toString("utf8");
  try { return JSON.parse(text); } catch { return { error: "runtime returned non-JSON", raw: text.slice(0, 500) }; }
}

const meta = (statusCode, headers) => ({ statusCode, headers });

function sendJson(stream, obj, status = 200) {
  const out = awslambda.HttpResponseStream.from(stream, meta(status, { "Content-Type": "application/json" }));
  out.write(JSON.stringify(obj));
  out.end();
}

async function sendFile(stream, file, extraHeaders = {}) {
  const type = TYPES[path.extname(file).toLowerCase()] || "application/octet-stream";
  const out = awslambda.HttpResponseStream.from(stream,
    meta(200, { "Content-Type": type, "Cache-Control": "public, max-age=300", ...extraHeaders }));
  await pipeline(createReadStream(file), out);
}

// Resolve inside base only, and only to a file that exists: a miss must be a
// 404, not a stream that dies mid-flight after the headers are already sent.
function safeJoin(base, rel) {
  const root = path.resolve(base);
  const p = path.resolve(root, "." + path.posix.normalize("/" + rel));
  if (!p.startsWith(root + path.sep) || !existsSync(p) || !statSync(p).isFile()) return null;
  return p;
}

export const handler = awslambda.streamifyResponse(async (event, stream, _context) => {
  const http = event.requestContext?.http || {};
  const method = (http.method || "GET").toUpperCase();
  const rawPath = decodeURIComponent(http.path || event.rawPath || "/");
  const headers = event.headers || {};
  const session = sid(headers["x-fairbill-session"] || headers["X-Fairbill-Session"]);
  const bodyText = event.body
    ? (event.isBase64Encoded ? Buffer.from(event.body, "base64").toString("utf8") : event.body)
    : "";
  const body = bodyText ? JSON.parse(bodyText) : {};
  const fail = (status, msg) => sendJson(stream, { detail: msg }, status);

  try {
    if (method === "GET" && (rawPath === "/" || rawPath === "/index.html"))
      return await sendFile(stream, path.join(HERE, "static", "index.html"), { "Cache-Control": "no-cache" });

    if (method === "GET" && rawPath.startsWith("/static/")) {
      const f = safeJoin(path.join(HERE, "static"), rawPath.slice("/static".length));
      return f ? await sendFile(stream, f) : fail(404, "no such file");
    }
    if (method === "GET" && rawPath.startsWith("/gallery/")) {
      const f = safeJoin(path.join(HERE, "gallery"), rawPath.slice("/gallery".length));
      return f ? await sendFile(stream, f) : fail(404, "no such file");
    }

    if (method === "GET" && rawPath === "/api/bills") return sendJson(stream, await prebaked("bills.json"));
    if (method === "GET" && rawPath === "/api/bench") return sendJson(stream, await prebaked("bench.json"));
    if (method === "GET" && rawPath === "/api/health") {
      // app.py's /api/health carries no `mode` field; the deploy brief asks the
      // public health check to name the tier it serves, so it is added here.
      const h = await prebaked("health.json");
      return sendJson(stream, { ...h, ts: Date.now() / 1000, mode: "runtime", runtime: Boolean(AGENT_ARN || LOCAL_RUNTIME) });
    }
    let m = rawPath.match(/^\/api\/bills\/([^/]+)\/truth-crops$/);
    if (method === "GET" && m) {
      const all = await prebaked("truth_crops.json");
      return all[m[1]] ? sendJson(stream, all[m[1]]) : fail(404, "no such bill");
    }

    m = rawPath.match(/^\/api\/run\/([^/]+)$/);
    if (method === "POST" && m) {
      const cached = (event.queryStringParameters || {}).cached === "1";
      const { body: up } = await invoke({ action: "run", bill_id: m[1], cached }, session);
      const out = awslambda.HttpResponseStream.from(stream, meta(200, {
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no" }));
      for await (const chunk of up) out.write(Buffer.from(chunk));  // byte-for-byte SSE passthrough
      out.end();
      return;
    }

    m = rawPath.match(/^\/api\/decide\/([^/]+)$/);
    if (method === "POST" && m) {
      const out = await invokeJson(
        { action: "decide", bill_id: m[1], option_id: body.option_id || "dispute" }, session);
      // web/app.js links the calendar file rather than inlining it, so the ics
      // text is dropped here and served from GET /api/ics/<id>.
      delete out.ics;
      out.ics_url = "/api/ics/" + m[1];
      return sendJson(stream, out);
    }

    m = rawPath.match(/^\/api\/chat\/([^/]+)$/);
    if (method === "POST" && m)
      return sendJson(stream, await invokeJson({ action: "chat", bill_id: m[1], text: body.text || "" }, session));

    m = rawPath.match(/^\/api\/undo\/([^/]+)$/);
    if (method === "POST" && m) {
      const out = await invokeJson({ action: "undo", entry_id: m[1] }, session);
      if (out.error) return fail(404, out.error);
      return sendJson(stream, out.undone || out);
    }

    if (method === "GET" && rawPath === "/api/ledger")
      return sendJson(stream, await invokeJson({ action: "ledger" }, session));

    m = rawPath.match(/^\/api\/ics\/([^/]+)$/);
    if (method === "GET" && m) {
      const out = await invokeJson({ action: "ics", bill_id: m[1] }, session);
      if (!out.ics) return fail(404, out.error || "no calendar entry");
      const s = awslambda.HttpResponseStream.from(stream, meta(200, {
        "Content-Type": "text/calendar",
        "Content-Disposition": 'attachment; filename="' + m[1] + '.ics"' }));
      s.write(out.ics);
      s.end();
      return;
    }

    return fail(404, "not found");
  } catch (err) {
    return fail(502, err.name + ": " + err.message);
  }
});
