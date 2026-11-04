# Runtime implementation handoff

2026-10-22 recognition options: optional `unknown_no_retry` on eligible Canvas
nodes/Steps; main UNKNOWN blocks, Trigger ends its round, attached Guard skips.
Step `input_conditions` uses same-frame COCO/color/OCR rows, left-to-right;
user's final rule is UNKNOWN-dominant AND and known-value-dominant OR. Do not
replace it with Kleene logic or apply it to existing Guards. `roi_policy.py`
expands final COCO/OCR search ROI only for non-1080x2400 physical displays;
PROGRESS OCR is excluded and saved definitions stay untouched. See
`docs/rwtd/RECOGNITION_OPTIONS_20261022.md`. Checks are authored/registered,
not executed; leave the current OK process untouched until user-run validation.

目前節點與 CHAIN 行為的統一查閱入口：[NODE_LOGIC.md](NODE_LOGIC.md)（2026-10-22）。
以下保留歷史交接紀錄；日期較早的行為描述請對照該彙整與目前程式碼。

2026-10-22 addition: `watchdog.py` and `ok_tasks/RwtdWatchdogTask.py` provide a
native OK TriggerTask switch owning independent passive supervision. Farm session
telemetry hooks are independent of diagnostic logging settings. Default disabled;
no live watcher was started while editing. Watchdog never captures or dispatches
input and does not restart tasks. See `docs/rwtd/WATCHDOG.md`; user-run tests and
live delivery remain pending. Do not infer external process-hang detection from
this same-process supervisor.

Updated 2026-10-16. Source notes belong beside this implementation; user-facing
requirements and evidence live under `docs/rwtd/`.

- `canvas_scheduler.py`: shared node execution, Guard-before-budget, explicit
  BLOCK continuation, target-limit evidence, START KEY / END completion.
- `canvas_triggers.py`, `trigger_observers.py`: independent read-only observation;
  only the owning executor claims device work. Preserve generation/round checks
  and existing deduplication. No queue TTL is requested.
- `trigger_preemption.py`, `device_macro.py`: nonzero opt-in Trigger cancels all
  remaining operations/repeats of the current node at touch-up. Do not resume or
  replay that node. A missing device STOP receipt is an uncertain delivery, not
  successful cancellation. Normal Stop must remain distinct from preemption.
- `flow_key.py`, `solver_recovery.py`: origin START owns END behavior. Only actual
  END creates completion. Distinguish completed execution/cooldown from attempted
  Solver input/rank. Trigger participants may jointly change the recovery screen
  without consuming the Solver's own attempt rank.
- `canvas_chains.py`, `farm_runtime.py`: memory revisit is bounded and may follow
  any successful next-chain handoff; interrupted memory C1 must not disappear.
- `module_store.py`, `decision_graph.py`, `feature_catalog.py`: bounded metadata-
  checked caches. Never bypass revision validation or saved-file readback.

Resolved root cause: 03:47:34 C3 `Node26` / OCR `Node35` hit 10 attempts and 3/3
chain failures. Observations had already stopped after debugkit `Node58` at
03:46:26. A later target miss after a confirmed first input no longer sets the
shared `trigger_cancel` flag. True delivery uncertainty still stops execution.
Farm now explicitly reports unavailable observation and stops, rather than
waiting forever with only heartbeats. Do not clear unknown cancellation flags.

Tests use fake devices/frames and temporary definitions. The foreground entry is
`docs/rwtd/tools/Run-Phase3DashboardChecks.ps1`; keep one test process at a time.
Sandbox temp-directory permission failures must be separated from code failures.
Latest full run (08:44 local) passed 901 tests, zero failures/errors/skips:
`docs/rwtd/analysis/phase3_dashboard_checks_20261016T004430_7882880Z/report.json`.
JavaScript syntax checks passed for canvas_model, decision_editor and the offline
fixture. These do not establish unattended live behavior. Earlier sandbox-temp
failures and the intermediate 894-test failing run are retained as evidence.
The latest user explicitly authorized direct testing, starting `py main_debug.py`
and native Farm activation for this unattended run.

See `docs/rwtd/OPTIMIZATION_LIST.md`, `TRIGGER_PREEMPTION.md`, `START_KEY_SOLVER.md`,
`NODE_RESULT_POLICY.md`, `CANVAS_CONTROLS_AND_READ_CACHE.md` for behavior and limits.
User confirmed substantial UI/save improvement; unattended live Farm is
authorized after successful verification in this task. At 08:48 the saved Canvas
was `亡途第五關V1.2.3`, mode `Until stopped`; native UI activation produced actual
macro completion and further node/observer progress. No saved Canvas was edited.
User subsequently requested no polling after activation: leave Farm running,
do not add background monitors, and finish only remaining repository work.
