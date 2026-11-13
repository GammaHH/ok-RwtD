"""Native Farm activity ownership hooks, with no OK UI or device startup."""
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def load_task_class():
    dashboard_stub = SimpleNamespace(RwtdControlDashboardTask=type('DashboardStub', (), {}))
    path = Path(__file__).resolve().parents[2] / 'ok_tasks' / 'RwtdFarmTask.py'
    spec = importlib.util.spec_from_file_location('farm_activity_task_fixture', path)
    module = importlib.util.module_from_spec(spec)
    # patch.dict(sys.modules) restores the entire dictionary on exit, removing
    # NumPy/OpenCV imported by this fixture. Native extensions cannot then be
    # safely initialized again by the next test in the same process.
    key = 'ok_tasks.RwtdControlDashboardTask'
    missing = object()
    previous = sys.modules.get(key, missing)
    sys.modules[key] = dashboard_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is missing:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = previous
    return module.RwtdFarmTask


class FarmActivityTaskTests(unittest.TestCase):
    def prepare_ocr(self, boxes):
        import numpy as np
        self.main.runtime = SimpleNamespace(current_node=None, cursor='Node43')
        self.main.schedule = SimpleNamespace(current='Node43')
        self.task.failure_evidence = Mock()
        self.task.exit_is_set = lambda: False
        self.task.validate_native_frame = Mock()
        self.task.ocr = Mock(return_value=boxes)
        frame = np.zeros((2400,1080,3), dtype=np.uint8)
        backend = SimpleNamespace(get_frame=Mock(return_value=frame))
        device = {'selected':True, 'connected':True}
        self.task.manual_backend = Mock(return_value=(backend, device))
        return frame, backend, device

    def test_chain_unknown_remembers_its_actual_frame_and_logs_reason(self):
        frame, _, device = self.prepare_ocr([])
        rule = {'field':'progress_km', 'op':'lte', 'value':100}
        box_module = SimpleNamespace(Box=lambda *args: args)
        with patch.dict(sys.modules, {'ok.feature.Box':box_module}):
            self.assertEqual(self.task._observe(rule), {})
        self.task.failure_evidence.forget.assert_called_once_with('Node43')
        args, kwargs = self.task.failure_evidence.remember.call_args
        self.assertEqual(args[0], 'Node43')
        self.assertIs(args[1], frame)
        self.assertEqual(kwargs['device'], device)
        detail = self.main.chain_reading
        self.assertEqual(detail['roi'], [330,370,156,55])
        self.assertEqual(detail['roi_mean_pixel'], 0)
        self.assertEqual(detail['reason'], 'no_numeric_token')
        self.assertIn('RWTD_CHAIN_OCR ', self.task.log_info.call_args.args[0])

    def test_chain_valid_and_ambiguous_ocr_keep_distinct_results(self):
        self.prepare_ocr([SimpleNamespace(name='1,234 km', confidence=.95)])
        rule = {'field':'progress_km', 'op':'lte', 'value':100}
        with patch.dict(sys.modules, {'ok.feature.Box':SimpleNamespace(Box=lambda *args:args)}):
            self.assertEqual(self.task._observe(rule), {'progress_km':1234})
            self.task.ocr.return_value = [SimpleNamespace(name='100 200', confidence=.9)]
            self.assertEqual(self.task._observe(rule), {})
        self.assertEqual(self.main.chain_reading['number_count'], 2)
        self.assertEqual(self.main.chain_reading['result'], 'UNKNOWN')

    def test_chain_capture_error_discards_old_evidence_and_reports_exception(self):
        _, backend, _ = self.prepare_ocr([])
        backend.get_frame.side_effect = RuntimeError('capture unavailable')
        with patch.dict(sys.modules, {'ok.feature.Box':SimpleNamespace(Box=lambda *args:args)}):
            self.assertEqual(self.task._observe({'field':'progress_km'}), {})
        self.task.failure_evidence.forget.assert_called_once_with('Node43')
        self.task.failure_evidence.remember.assert_not_called()
        self.assertIn('capture unavailable', self.main.chain_reading['reason'])

    def setUp(self):
        self.task = object.__new__(load_task_class())
        # Match TriggerTaskScheduler's recovery contract without starting workers.
        self.main = SimpleNamespace(runtime=object(),
            watchers=SimpleNamespace(recovery=SimpleNamespace(event=None)), time_guards=object())
        self.event = SimpleNamespace(runtime=object(),
            watchers=SimpleNamespace(recovery=SimpleNamespace(event=None)), time_guards=object())
        self.main.active_runtime = self.main
        self.main.activity_snapshot = Mock(return_value=[])
        self.task.farm = self.main
        self.task._activity_owner = self.main
        self.task._activity_contexts = {}
        self.task.module_progress = {'main-module': 3}
        self.task.farm_action_progress = {'main-action': 2}
        self.task.uncertain_inputs = {'main-input': 'sent'}
        self.task.manual = SimpleNamespace(time_guards=self.main.time_guards,
                                          decision_store=SimpleNamespace())
        self.task.controller = SimpleNamespace(
            lock=threading.RLock(), time_guards=self.main.time_guards,
            trigger_observers=None, trigger_work=None, activity_inline_observers=False,
            trigger_cancel=threading.Event(), device_stop=threading.Event(),
            capture_suppressed=threading.Event(), _invalidate_trigger_frames=Mock(),
            _invalidate_trigger_rounds=Mock())
        self.task.trigger_frames = {'old-frame': object()}
        self.task.root = Path('unused-test-root')
        self.task.monitor = SimpleNamespace(notifier=object())
        self.task._failure_notifier = Mock(return_value=self.task.monitor.notifier)
        self.task.log_info = Mock()

    def test_activity_switch_rebinds_owner_and_restores_main_progress(self):
        main_caches = (self.task.module_progress, self.task.farm_action_progress, self.task.uncertain_inputs)
        self.main.active_runtime = self.event
        bindings = []
        self.task.controller.trigger_observers = SimpleNamespace(sync=lambda: bindings.append(
            (self.task.controller.trigger_scheduler,
             self.task.controller.activity_inline_observers)))
        with patch('src.rwtd.android_core.failure_evidence.FailureEvidence') as evidence:
            event_evidence, resumed_evidence = object(), object()
            evidence.side_effect = [event_evidence, resumed_evidence]
            self.task._activity_switch(self.event)
            self.assertIs(self.task._owner, self.event)
            self.assertIs(self.task.controller.canvas_runtime, self.event.runtime)
            self.assertIs(self.task.controller.trigger_scheduler, self.event.watchers)
            self.assertIs(self.task.controller.continuation_owner, self.event)
            self.assertIs(self.task.manual.time_guards, self.event.time_guards)
            self.assertIs(self.task.controller.time_guards, self.event.time_guards)
            self.assertFalse(self.task.controller.activity_inline_observers)
            self.assertEqual(self.task.module_progress, {})
            self.assertEqual(self.task.farm_action_progress, {})
            self.assertEqual(self.task.uncertain_inputs, {})
            self.assertEqual(self.task.trigger_frames, {})
            self.assertIs(self.task.failure_evidence, event_evidence)
            self.assertTrue(self.task.manual.decision_store.execution_locked)
            self.assertTrue(self.task.manual.decision_store.input_locked)
            self.task.module_progress['event-only'] = 8
            self.task.uncertain_inputs['event-only'] = 'sent'
            self.task.trigger_frames['event-frame'] = object()
            self.main.active_runtime = self.main
            self.task._activity_switch(self.main)
            self.assertIs(self.task.controller.canvas_runtime, self.main.runtime)
            self.assertIs(self.task.controller.trigger_scheduler, self.main.watchers)
            self.assertIs(self.task.controller.continuation_owner, self.main)
            self.assertIs(self.task.manual.time_guards, self.main.time_guards)
            self.assertIs(self.task.controller.time_guards, self.main.time_guards)
            self.assertFalse(self.task.controller.activity_inline_observers)
            for actual, original in zip((self.task.module_progress, self.task.farm_action_progress,
                                         self.task.uncertain_inputs), main_caches):
                self.assertIs(actual, original)
            self.assertNotIn('event-only', self.task.uncertain_inputs)
            self.assertEqual(self.task.trigger_frames, {})
            self.assertIs(self.task.failure_evidence, resumed_evidence)
            self.assertNotIn(id(self.event), self.task._activity_contexts)
            self.assertEqual(self.task.controller._invalidate_trigger_frames.call_count, 2)
            self.assertEqual(bindings, [(self.event.watchers, False), (self.main.watchers, False)])

    def test_sync_uses_active_owner_recovery_and_publishes_restart_once(self):
        self.main.watchers.recovery.event = dict(id='main-cycle', cycle_restarted=True,
                                                problem='main-node', candidates=['main-solver'])
        self.event.watchers.recovery.event = dict(id='event-cycle', cycle_restarted=True,
                                                 problem='event-node', candidates=['event-solver'])
        self.main.active_runtime = self.event
        self.task._sync_farm()
        self.task._sync_farm()
        self.task.log_info.assert_called_once()
        message = self.task.log_info.call_args.args[0]
        self.assertIn('RWTD_SOLVER_CYCLE_RESTART ', message)
        self.assertIn('event-cycle', message)
        self.assertNotIn('main-cycle', message)
        self.assertIs(self.task.controller.trigger_scheduler, self.event.watchers)

    def test_activity_boundary_dispatches_current_canvas_trigger_inputs(self):
        self.main.activity_pending = True
        self.main.activity_active = True
        self.task.controller._trigger_boundary = Mock(return_value=True)
        self.assertFalse(self.task._farm_tick_boundary())
        self.task.controller._trigger_boundary.assert_called_once_with()
        self.task.controller._trigger_boundary.return_value = False
        self.assertTrue(self.task._farm_tick_boundary())

    def test_pending_switch_does_not_dispatch_old_canvas_trigger_inputs(self):
        self.main.activity_pending = True
        self.main.activity_active = False
        self.task.controller._trigger_boundary = Mock()
        self.assertTrue(self.task._farm_tick_boundary())
        self.task.controller._trigger_boundary.assert_not_called()

    def test_ready_drains_observers_without_latching_user_stop(self):
        thread = Mock()
        thread.is_alive.return_value = True
        observers = SimpleNamespace(slots={'watch': {'thread': thread}}, sync=Mock())
        self.task.controller.trigger_observers = observers
        self.assertFalse(self.task._activity_ready())
        self.task.controller._invalidate_trigger_rounds.assert_not_called()
        self.assertTrue(self.task.controller.activity_inline_observers)
        observers.sync.assert_called_once()
        for flag in ('device_stop', 'trigger_cancel', 'capture_suppressed'):
            self.assertFalse(getattr(self.task.controller, flag).is_set())
        thread.is_alive.return_value = False
        self.task.controller.trigger_work = object()
        self.assertFalse(self.task._activity_ready())
        self.task.controller.trigger_work = None
        self.assertTrue(self.task._activity_ready())
        self.task.controller._invalidate_trigger_rounds.assert_called_once()
        for flag in ('device_stop', 'trigger_cancel', 'capture_suppressed'):
            self.assertFalse(getattr(self.task.controller, flag).is_set())

    def test_ready_does_not_clear_existing_user_cancellation(self):
        self.task.controller.device_stop.set()
        self.task.controller.trigger_cancel.set()
        self.task.controller.capture_suppressed.set()
        self.task._activity_ready()
        self.assertTrue(self.task.controller.device_stop.is_set())
        self.assertTrue(self.task.controller.trigger_cancel.is_set())
        self.assertTrue(self.task.controller.capture_suppressed.is_set())
