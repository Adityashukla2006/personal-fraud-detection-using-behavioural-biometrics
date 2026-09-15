// Keystroke timing capture.
//
// Key identity is used only in memory, to pair each key-down with its key-up and to count edits.
// It is never stored in a stroke record and never leaves the page: the payload built here holds
// timing deltas and three counts, nothing else (CLAUDE.md section 2).

export const FIELD_KEYS = ["hold", "down_down", "up_down", "backspaces", "corrections", "pastes"];
export const MIN_KEYSTROKES = 3;
export const MAX_KEYSTROKES = 256;
export const MAX_FIELDS = 16;
export const MAX_HOLD_MS = 5000;
export const MAX_INTERVAL_MS = 60000;

const IGNORED_KEYS = new Set([
  "Shift", "Control", "Alt", "Meta", "CapsLock", "Tab", "Enter", "Escape",
  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
]);
const EDIT_KEYS = new Set(["Backspace", "Delete"]);

export function createRecorder() {
  return { pending: new Map(), strokes: [], backspaces: 0, corrections: 0, pastes: 0, editing: false };
}

export function resetRecorder(recorder) {
  Object.assign(recorder, createRecorder());
}

export function keyDown(recorder, code, key, time, repeat) {
  if (repeat || IGNORED_KEYS.has(key)) return;
  if (EDIT_KEYS.has(key)) {
    recorder.backspaces += 1;
    recorder.editing = true;
    return;
  }
  // Typing again after a run of deletions is one correction, however many keys were deleted.
  if (recorder.editing) {
    recorder.corrections += 1;
    recorder.editing = false;
  }
  if (!recorder.pending.has(code)) recorder.pending.set(code, time);
}

export function keyUp(recorder, code, time) {
  const down = recorder.pending.get(code);
  if (down === undefined) return;
  recorder.pending.delete(code);
  if (time - down <= MAX_HOLD_MS) recorder.strokes.push([down, time]);
}

export function paste(recorder) {
  recorder.pastes += 1;
}

// Milliseconds to seconds, at 0.1 ms resolution.
const seconds = (ms) => Math.round(ms * 10) / 10000;

function toField(strokes, counts) {
  const kept = strokes.slice(-MAX_KEYSTROKES);
  const hold = kept.map(([down, up]) => seconds(up - down));
  const down_down = [];
  const up_down = [];
  for (let i = 0; i < kept.length - 1; i += 1) {
    down_down.push(seconds(kept[i + 1][0] - kept[i][0]));
    up_down.push(seconds(kept[i + 1][0] - kept[i][1]));
  }
  return { hold, down_down, up_down, ...counts };
}

// One recorder can yield several fields: an entry is split wherever the user left it for longer
// than MAX_INTERVAL_MS. Edit counts are attached to the last segment.
export function buildFields(recorder) {
  const strokes = [...recorder.strokes].sort((a, b) => a[0] - b[0]);
  const segments = [];
  let current = [];
  for (const stroke of strokes) {
    if (current.length && stroke[0] - current[current.length - 1][0] > MAX_INTERVAL_MS) {
      segments.push(current);
      current = [];
    }
    current.push(stroke);
  }
  if (current.length) segments.push(current);

  const usable = segments.filter((segment) => segment.length >= MIN_KEYSTROKES);
  const clamp = (count) => Math.min(count, MAX_KEYSTROKES);
  return usable.map((segment, index) =>
    toField(
      segment,
      index === usable.length - 1
        ? { backspaces: clamp(recorder.backspaces), corrections: clamp(recorder.corrections), pastes: clamp(recorder.pastes) }
        : { backspaces: 0, corrections: 0, pastes: 0 },
    ),
  );
}

export function buildBehaviour(recorders) {
  return { fields: recorders.flatMap(buildFields).slice(-MAX_FIELDS) };
}

// Coarse device descriptor. device_id is a random per-browser identifier, not a fingerprint.
export function deviceDescriptor(deviceId, env = globalThis) {
  const screen = env.screen ?? { width: 0, height: 0 };
  return {
    device_id: deviceId,
    max_touch_points: Math.min(32, Math.max(0, env.navigator?.maxTouchPoints ?? 0)),
    coarse_pointer: Boolean(env.matchMedia?.("(pointer: coarse)")?.matches),
    short_side_px: Math.min(10000, Math.max(0, Math.round(Math.min(screen.width, screen.height)))),
  };
}
