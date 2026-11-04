# Dashboard handoff

2026-10-22: Canvas inspector has `nodeUnknownNoRetry`; Step editor dynamically
adds optional COCO/color/OCR gate rows and `stepUnknownNoRetry`. Conditions are
separate from touch targets and preserve module-local TAGs. Python and browser
fixtures cover the requested nonstandard UNKNOWN combination rules. No tests or
live browser/device checks were executed in this update; see
`docs/rwtd/RECOGNITION_OPTIONS_20261022.md` and `STEP_INPUT_CONDITIONS.md`.

2026-10-16: user confirmed substantial interaction and save-speed improvement.
Python integration suite: 901 passed. `canvas_model.js`, `decision_editor.js` and
the offline fixture passed Node syntax checks. Browser gesture/long-run behavior
still needs live validation; do not claim Python tests execute every HTML case.

- `decision_editor.js`: right-button box selection; Space/middle-button pan;
  selection Ctrl+C/Ctrl+V; one undo point for a pasted/moved group; preserve locks.
- `canvas_model.js`: atomic group copy/paste, new IDs, internal edges, OCR/Guard
  references. Cloned Trigger starts are disabled; chain membership is not cloned.
- Draft writes are coalesced after 180 ms; explicit save/switch/unload still
  persists current drafts. Never replace durable save receipt with optimistic UI.
- `triggerPreempt`: nonzero Trigger only, default off. Cancels the current node's
  remaining operations/repeats at gesture-up; no resume. Same class stays FIFO.
- `entryGuardRetry`: optional 1..1000 UNKNOWN attempts, overrides global maximum.
- Backend read caches belong to `src/rwtd/android_core/`; graph save receipts
  force a fresh disk read. Do not remove revision checks or atomic writes.
- User explicitly excluded Trigger queue expiry/TTL from this delivery.

See `docs/rwtd/CANVAS_CONTROLS_AND_READ_CACHE.md`, `TRIGGER_PREEMPTION.md`,
`START_KEY_SOLVER.md`, and `OPTIMIZATION_LIST.md` for detailed rules and limits.
Next agents should inspect actual saved Canvas IDs and fresh logs; display names
and old screenshots are not proof of the currently executing graph.
