// "Under the hood": the AWS request path of every call this page makes, drawn live.
//
// Each request lights its real route through the deployed services. Every number shown is
// measured: the scoring Lambda reports its own stages in the Server-Timing header (read, score,
// write, publish, workflow), and the rest of the round trip is API Gateway plus the network. Only
// the pacing of the animation is illustrative.

const SVG = "http://www.w3.org/2000/svg";

// AWS architecture-icon category colours, so the map reads like an AWS diagram.
const CATEGORY = {
  client: "#5b6474",
  network: "#8c4fff",
  security: "#dd344c",
  compute: "#ed7100",
  database: "#c925d1",
  integration: "#e7157b",
  storage: "#7aa116",
  analytics: "#8c4fff",
};

const NODES = {
  browser: { x: 180, y: 34, label: "Your browser", note: "capture.mjs", kind: "client", glyph: "WEB" },
  cloudfront: { x: 62, y: 108, label: "CloudFront", note: "S3 static site", kind: "network", glyph: "CF" },
  cognito: { x: 298, y: 108, label: "Cognito", note: "passkeys, JWT", kind: "security", glyph: "COG" },
  apigw: { x: 180, y: 176, label: "API Gateway", note: "JWT authorizer", kind: "network", glyph: "API" },
  scoring: { x: 62, y: 250, label: "λ scoring", note: "fraudcore", kind: "compute", glyph: "λ" },
  transfers: { x: 298, y: 250, label: "λ transfers", note: "step-up", kind: "compute", glyph: "λ" },
  dynamodb: { x: 180, y: 318, label: "DynamoDB", note: "single table", kind: "database", glyph: "DDB" },
  eventbridge: { x: 62, y: 392, label: "EventBridge", note: "decision bus", kind: "integration", glyph: "EB" },
  sfn: { x: 298, y: 392, label: "Step Functions", note: "transfer workflow", kind: "integration", glyph: "SF" },
  lake: { x: 62, y: 466, label: "S3 audit lake", note: "λ archive", kind: "storage", glyph: "S3" },
  adaptation: { x: 180, y: 466, label: "λ adaptation", note: "trust-gated", kind: "compute", glyph: "λ" },
  ledger: { x: 298, y: 466, label: "λ ledger", note: "debits on release", kind: "compute", glyph: "λ" },
  athena: { x: 62, y: 540, label: "Athena", note: "nightly batch", kind: "analytics", glyph: "ATH" },
  sns: { x: 298, y: 540, label: "SNS", note: "fraud alerts", kind: "integration", glyph: "SNS" },
};

const EDGES = [
  ["browser", "cloudfront"],
  ["browser", "cognito"],
  ["browser", "apigw"],
  ["apigw", "scoring"],
  ["apigw", "transfers"],
  ["scoring", "dynamodb"],
  ["transfers", "dynamodb"],
  ["transfers", "cognito"],
  ["scoring", "eventbridge"],
  ["scoring", "sfn"],
  ["transfers", "sfn"],
  ["sfn", "ledger"],
  ["ledger", "dynamodb"],
  ["eventbridge", "lake"],
  ["eventbridge", "adaptation"],
  ["adaptation", "dynamodb"],
  ["sfn", "sns"],
  ["sfn", "eventbridge"],
  ["lake", "athena"],
];

const W = 104;
const H = 44;

// Latency stages, each with a fixed categorical slot: colour follows the stage, never its rank.
export const STAGES = [
  { key: "network", label: "API Gateway + network", slot: 1 },
  { key: "read", label: "DynamoDB read", slot: 2 },
  { key: "score", label: "fraudcore scoring", slot: 3 },
  { key: "write", label: "DynamoDB write", slot: 4 },
  { key: "publish", label: "EventBridge publish", slot: 5 },
  { key: "workflow", label: "Step Functions start", slot: 6 },
  { key: "other", label: "Lambda other", slot: 7 },
];

export function parseServerTiming(header) {
  const stages = {};
  for (const part of (header ?? "").split(",")) {
    const match = /^\s*([\w-]+);dur=([\d.]+)/.exec(part);
    if (match) stages[match[1]] = Number(match[2]);
  }
  return stages;
}

function el(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (parent) parent.appendChild(node);
  return node;
}

function html(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const edgeId = (a, b) => [a, b].sort().join("--");
const ms = (value) => (value >= 100 ? `${Math.round(value)} ms` : `${value.toFixed(1)} ms`);

export function createHood(root) {
  const map = root.querySelector("[data-hood=map]");
  const svg = el("svg", { viewBox: "0 0 360 572", role: "img", "aria-label": "AWS request path" }, map);
  const edgeLayer = el("g", { class: "edges" }, svg);
  const nodeLayer = el("g", { class: "nodes" }, svg);
  const edges = new Map();
  const nodes = new Map();

  for (const [a, b] of EDGES) {
    const line = el("line", {
      x1: NODES[a].x, y1: NODES[a].y, x2: NODES[b].x, y2: NODES[b].y, class: "edge",
    }, edgeLayer);
    edges.set(edgeId(a, b), line);
  }
  for (const [key, node] of Object.entries(NODES)) {
    const group = el("g", { class: "node", transform: `translate(${node.x - W / 2} ${node.y - H / 2})` }, nodeLayer);
    el("rect", { width: W, height: H, rx: 8, class: "node-box" }, group);
    el("rect", { x: 6, y: 8, width: 28, height: 28, rx: 5, fill: CATEGORY[node.kind] }, group);
    const glyph = el("text", { x: 20, y: 26, class: "node-glyph", "text-anchor": "middle" }, group);
    glyph.textContent = node.glyph;
    const label = el("text", { x: 40, y: 19, class: "node-label" }, group);
    label.textContent = node.label;
    const note = el("text", { x: 40, y: 33, class: "node-note" }, group);
    note.textContent = node.note;
    const title = el("title", {}, group);
    title.textContent = `${node.label}: ${node.note}`;
    nodes.set(key, { group, note, base: node.note });
  }

  let run = 0;

  function reset() {
    for (const line of edges.values()) line.classList.remove("hot", "async");
    for (const { group, note, base } of nodes.values()) {
      group.classList.remove("hot", "async");
      note.textContent = base;
    }
  }

  // steps: [{ node, from?, note?, async? }]; each lights its node and the edge it came along.
  async function trace(steps) {
    const mine = ++run;
    reset();
    for (const step of steps) {
      if (mine !== run) return;
      const target = nodes.get(step.node);
      if (step.from) edges.get(edgeId(step.from, step.node))?.classList.add(step.async ? "async" : "hot");
      // A service already used on the synchronous path keeps that marking and its measured note.
      const busy = step.async && target.group.classList.contains("hot");
      if (!busy) {
        target.group.classList.add(step.async ? "async" : "hot");
        if (step.note) target.note.textContent = step.note;
      }
      await sleep(step.async ? 260 : 170);
    }
  }

  // ---- Latency waterfall ------------------------------------------------------------------------

  const waterfall = root.querySelector("[data-hood=waterfall]");

  function latency(roundTrip, serverTiming) {
    const stages = parseServerTiming(serverTiming);
    const handler = stages.handler ?? 0;
    const values = {
      network: Math.max(0, roundTrip - handler),
      read: stages.read ?? 0,
      score: stages.score ?? 0,
      write: stages.write ?? 0,
      publish: stages.publish ?? 0,
      workflow: stages.workflow ?? 0,
    };
    const inside = values.read + values.score + values.write + values.publish + values.workflow;
    values.other = Math.max(0, handler - inside);

    const bar = html("div", "wf-bar");
    const legend = html("ul", "wf-legend");
    for (const stage of STAGES) {
      const value = values[stage.key];
      if (!(value > 0)) continue;
      const segment = html("span", `wf-seg series-${stage.slot}`);
      segment.style.flexGrow = String(value);
      segment.dataset.tip = `${stage.label}: ${ms(value)}`;
      bar.appendChild(segment);
      const row = html("li");
      row.append(html("i", `swatch series-${stage.slot}`), html("span", "", stage.label), html("b", "", ms(value)));
      legend.appendChild(row);
    }
    const total = html("p", "wf-total");
    total.append(
      html("b", "", ms(roundTrip)),
      document.createTextNode(handler ? ` round trip, ${ms(handler)} inside Lambda` : " round trip"),
    );
    waterfall.replaceChildren(total, bar, legend);
    return values;
  }

  function latencyOnly(roundTrip, label) {
    const total = html("p", "wf-total");
    total.append(html("b", "", ms(roundTrip)), document.createTextNode(` round trip, ${label}`));
    waterfall.replaceChildren(total, html("p", "muted small", "Only the scoring Lambda reports per-stage timings."));
  }

  // ---- Request log ------------------------------------------------------------------------------

  const log = root.querySelector("[data-hood=log]");

  function record(method, path, status, roundTrip, detail = "") {
    const row = html("li", status >= 400 ? "bad" : "");
    const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    row.append(
      html("span", "log-time", when),
      html("code", "log-route", `${method} ${path}`),
      html("span", "log-detail", detail),
      html("span", "log-ms", roundTrip === null ? String(status) : `${status} · ${Math.round(roundTrip)} ms`),
    );
    log.prepend(row);
    while (log.children.length > 12) log.lastChild.remove();
  }

  // ---- Payload inspector ------------------------------------------------------------------------

  const payload = root.querySelector("[data-hood=payload]");

  function abbreviate(value) {
    if (Array.isArray(value)) {
      if (value.length > 6 && value.every((item) => typeof item === "number")) {
        return [...value.slice(0, 6), `… ${value.length - 6} more timings`];
      }
      return value.map(abbreviate);
    }
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, abbreviate(v)]));
    }
    return value;
  }

  function showPayload(body) {
    payload.textContent = JSON.stringify(abbreviate(body), null, 2);
  }

  return { trace, latency, latencyOnly, record, showPayload, reset, stepsFor };
}

// The route each kind of request takes through the deployed stack.
export function stepsFor(kind, { values = {}, action = null } = {}) {
  const note = (value, text) => (value > 0 ? `${text} ${ms(value)}` : undefined);
  switch (kind) {
    case "page":
      return [{ node: "browser" }, { node: "cloudfront", from: "browser", note: "served this page" }];
    case "signin":
      return [{ node: "browser" }, { node: "cognito", from: "browser", note: "issued your JWT" }];
    case "account":
      return [
        { node: "browser" },
        { node: "apigw", from: "browser", note: note(values.network, "net+gw") },
        { node: "transfers", from: "apigw", note: "GET /account" },
        { node: "dynamodb", from: "transfers", note: "ledger read" },
      ];
    case "score": {
      const steps = [
        { node: "browser" },
        { node: "apigw", from: "browser", note: note(values.network, "net+gw") },
        { node: "scoring", from: "apigw", note: note(values.score, "score") },
        { node: "dynamodb", from: "scoring", note: note(values.read + values.write, "r+w") },
        { node: "eventbridge", from: "scoring", note: note(values.publish, "publish") },
      ];
      if (values.workflow > 0) {
        steps.push({ node: "sfn", from: "scoring", note: note(values.workflow, "start") });
        steps.push({ node: "ledger", from: "sfn", async: true, note: "holds or debits" });
        steps.push({ node: "dynamodb", from: "ledger", async: true });
        if (action === "restrict" || action === "block") {
          steps.push({ node: "sns", from: "sfn", async: true, note: "analyst alerted" });
        }
      }
      steps.push({ node: "lake", from: "eventbridge", async: true, note: "decision archived" });
      return steps;
    }
    case "stepup-start":
      return [
        { node: "browser" },
        { node: "apigw", from: "browser" },
        { node: "transfers", from: "apigw", note: "challenge" },
        { node: "cognito", from: "transfers", note: "WebAuthn options" },
      ];
    case "stepup-verify":
      return [
        { node: "browser" },
        { node: "apigw", from: "browser" },
        { node: "transfers", from: "apigw", note: "verify" },
        { node: "cognito", from: "transfers", note: "passkey accepted" },
        { node: "sfn", from: "transfers", note: "task token sent" },
        { node: "ledger", from: "sfn", async: true, note: "released" },
        { node: "dynamodb", from: "ledger", async: true },
        { node: "eventbridge", from: "sfn", async: true, note: "stepup.verified" },
        { node: "adaptation", from: "eventbridge", async: true, note: "profile learns" },
      ];
    default:
      return [{ node: "browser" }];
  }
}
