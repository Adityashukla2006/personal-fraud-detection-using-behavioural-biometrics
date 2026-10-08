// Tells the customer when a transfer they were waiting on settles elsewhere: an analyst releases or
// denies it, or its hold times out. Pure, so the page decides only where the notices go.

// Statuses a transfer leaves without the customer acting on this page.
export const OPEN = new Set(["pending", "processing", "awaiting_step_up", "under_review"]);

// `seen` maps transfer ids to the status last shown, or is null on a first look, which notifies
// nothing: the statement already shows how old transfers ended. `describe(transfer)` returns
// "₹500.00 to Maa" or similar.
export function settledNotices(seen, transfers, describe) {
  const notices = [];
  const next = {};
  for (const transfer of transfers) {
    next[transfer.transfer_id] = transfer.status;
    const before = seen?.[transfer.transfer_id];
    if (OPEN.has(before) && !OPEN.has(transfer.status)) {
      notices.push({ transferId: transfer.transfer_id, ...message(before, transfer, describe(transfer)) });
    }
  }
  return { notices, seen: next };
}

function message(before, transfer, what) {
  const reviewed = before === "under_review";
  if (transfer.status === "released") {
    return reviewed
      ? { level: "good", title: "Transfer approved", text: `Our fraud team reviewed your transfer of ${what} and sent it.` }
      : { level: "good", title: "Transfer completed", text: `Your transfer of ${what} has gone through.` };
  }
  if (transfer.reason === "denied") {
    return { level: "critical", title: "Transfer declined", text: `Our fraud team declined your transfer of ${what}. Nothing was debited.` };
  }
  if (transfer.reason === "timeout") {
    const why = reviewed ? "It wasn't reviewed in time" : "It wasn't verified in time";
    return { level: "serious", title: "Transfer cancelled", text: `${why}, so your transfer of ${what} was cancelled. Nothing was debited.` };
  }
  return { level: "critical", title: "Transfer stopped", text: `Your transfer of ${what} was stopped. Nothing was debited.` };
}
