"""User-run multi-activity round-boundary tests; no device operations."""
import tempfile
import unittest

from src.rwtd.android_core.activity_schedule_state import ActivityScheduleState
from src.rwtd.android_core.farm_runtime import FarmRuntime


def graph(prefix):
    return {'schema_version':2, 'entry':prefix+'Start', 'nodes':[
        {'id':prefix+'Start', 'kind':'start', 'chain':{'order':1,'failure_limit':1}},
        {'id':prefix+'First', 'kind':'module', 'module':'Work'},
        {'id':prefix+'Second', 'kind':'module', 'module':'Work'},
        {'id':prefix+'End', 'kind':'end'}], 'edges':[
        {'id':prefix+'A', 'source':prefix+'Start', 'port':'NEXT', 'target':prefix+'First'},
        {'id':prefix+'B', 'source':prefix+'First', 'port':'NEXT', 'target':prefix+'Second'},
        {'id':prefix+'C', 'source':prefix+'Second', 'port':'NEXT', 'target':prefix+'End'}],
        # These interval tests jump 190 minutes while a main Chain is active;
        # timeout behavior has its own dedicated regression suite.
        'settings':{'max_cycles':100, 'round_wait_seconds':.1, 'node_handoff_seconds':0,
                    'chain_timeout_minutes':10080}}


class FarmActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls, self.switched = [], []
        self.fail_prefix = None
        def effect(context):
            node = context['node']['id']
            self.calls.append(node)
            if self.fail_prefix and node.startswith(self.fail_prefix):
                return {'status':'FAILED', 'action_completed':False}
            return {'status':'INPUT_SENT_UNVERIFIED', 'action_completed':True}
        self.farm = FarmRuntime(graph('Farm'), {'Work':{'id':'Work','steps':[]}},
                                effect, lambda request:None, lambda event:None,
                                lambda:self.now, source='android_manual')

    def entries(self, delayed=False):
        return [{'id':prefix,'name':prefix,'graph':graph(prefix),'interval_seconds':interval,
                 **({'first_due':interval} if delayed else {})}
                for prefix,interval in [('A',10),('B',20),('C',30)]]

    def test_remote_activity_waits_for_normal_round_and_runs_requested_count(self):
        entries=self.entries(delayed=True)[:1]
        entries[0]['initial_runs']=0
        self.farm.configure_activities(entries)
        self.farm.queue_activity('A',2)
        self.assertFalse(self.farm.activity_pending)
        self.assertEqual(self.calls,[])
        self.round_boundary()
        self.assertTrue(self.farm.activity_pending)
        self.finish_batch()
        self.assertEqual(self.calls,['FarmFirst','FarmSecond']+['AFirst','ASecond']*2)

    def test_remote_stop_after_round_wins_over_due_activity(self):
        self.configure()
        self.farm.remote_stop_after_round=True
        self.round_boundary()
        self.assertEqual(self.farm.status,'STOPPED')
        self.assertFalse(self.farm.activity_pending)
        self.assertEqual(self.calls,['FarmFirst','FarmSecond'])

    def test_remote_activity_cannot_mutate_active_batch(self):
        self.configure()
        self.round_boundary()
        with self.assertRaises(ValueError):self.farm.queue_activity('A',2)

    def configure(self, delayed=False, **kwargs):
        self.farm.configure_activities(self.entries(delayed), activity_switch=self.switched.append, **kwargs)

    def round_boundary(self):
        previous = self.farm.schedule.rounds
        for _ in range(10):
            self.farm.tick(.2)
            if self.farm.schedule.rounds != previous:
                return
        self.fail('Farm did not reach its round boundary')

    def finish_batch(self):
        for _ in range(70):
            self.farm.tick(.2)
            if not self.farm.activity_pending:
                return
        self.fail('Activity batch did not finish')

    def tick_until(self, predicate, limit=10):
        # START has its own scheduler delay. Observe the relevant event rather
        # than assume a module has already run after a fixed number of ticks.
        for _ in range(limit):
            if predicate():
                return
            self.farm.tick(.2)
        self.assertTrue(predicate(), self.farm.snapshot())

    def test_activity_inherits_external_observers_and_pending_read_does_not_block(self):
        from test_canvas_farm import graph as trigger_graph
        from unittest.mock import Mock
        data = trigger_graph(trigger=True)
        next(n for n in data['nodes'] if n['id']=='T')['trigger_interval'] = 60
        invalidate = Mock()
        self.farm.external_watchers = True
        self.farm.invalidate_observers = invalidate
        self.farm.configure_activities([{'id':'A','graph':data,'interval_seconds':10}])
        self.round_boundary()
        self.farm.tick(.2)
        child = self.farm.active_runtime
        self.assertIsNot(child, self.farm)
        self.assertTrue(child.external_watchers)
        self.assertIs(child.invalidate_observers, invalidate)
        child.watchers.poll(task_id='T')
        self.assertIsNotNone(child.watchers.poll(task_id='T'))
        for _ in range(3):
            self.farm.tick(.2)
        self.assertIn('W1', self.calls)
        self.assertNotIn('Check', self.calls)
        self.assertIsNotNone(child.watchers.tasks['T']['pending'])

    def test_due_mid_round_waits_for_successful_round_completion(self):
        self.configure(delayed=True)
        self.farm.tick(.2)
        self.now = 20
        self.tick_until(lambda: bool(self.calls))
        self.assertFalse(self.farm.activity_pending)
        self.assertEqual(self.calls, ['FarmFirst'])
        self.round_boundary()
        self.assertEqual(self.calls, ['FarmFirst','FarmSecond'])
        self.assertTrue(self.farm.activity_pending)
        self.assertFalse(self.farm.activity_active)
        self.assertEqual(self.farm.schedule.rounds, 1)

    def test_due_activities_run_in_setting_order_before_next_farm_round(self):
        self.configure(delayed=True)
        self.now = 20
        self.round_boundary()
        schedule = self.farm.schedule
        self.finish_batch()
        self.assertIs(self.farm.schedule, schedule)
        self.assertEqual(self.calls, ['FarmFirst','FarmSecond'] + ['AFirst','ASecond'] * 2
                         + ['BFirst','BSecond'])
        self.assertEqual(schedule.total_runs, {'FarmStart':1})
        self.assertEqual(schedule.rounds, 1)
        self.assertEqual(self.farm.runtime.cursor, 'FarmStart')
        self.assertEqual([entry['id'] for entry in self.farm.activity_history
                          if entry['status']=='COMPLETED'], ['A','A','B'])
        self.assertEqual([entry['next_due'] for entry in self.farm.activities[:2]], [30, 40])
        self.assertIs(self.switched[-1], self.farm)
        self.assertIsNot(self.switched[0].time_guards, self.farm.time_guards)

    def test_initial_three_runs_happen_first_normal_round_without_waiting_interval(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=3, interval_seconds=190*60)
        self.farm.configure_activities(entries, activity_switch=self.switched.append)
        self.round_boundary()
        self.farm.tick(.2)
        self.assertEqual(self.farm.activities[0]['next_due'], 190*60)
        self.now = 60
        self.finish_batch()
        self.assertEqual(self.calls, ['FarmFirst','FarmSecond']+['AFirst','ASecond']*3)
        self.assertEqual([event['repetition'] for event in self.farm.activity_history
                          if event['status']=='COMPLETED'], [1,2,3])
        self.assertTrue(self.farm.activities[0]['initial_completed'])
        self.assertEqual(self.farm.activities[0]['next_due'], 190*60)
        self.assertEqual(self.switched.count(self.farm), 1)
        # First batch complete: the next round does not replay its initial runs.
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 190*60
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 4)

    def test_due_batch_is_snapshot_not_growing_during_activity_execution(self):
        self.configure(delayed=True)
        self.now = 10
        self.round_boundary()
        self.farm.tick(.2)
        self.now = 100
        self.finish_batch()
        self.assertEqual([node for node in self.calls if not node.startswith('Farm')], ['AFirst','ASecond'])
        self.assertEqual(self.farm.activities[0]['next_due'], 20)
        self.assertEqual(self.farm.activities[1]['next_due'], 20)
        self.assertEqual(self.farm.activities[2]['next_due'], 30)
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 9)

    def test_missed_intervals_accumulate_until_a_safe_farm_round(self):
        entries = self.entries()[:1]
        entries[0]['initial_runs'] = 0
        self.farm.configure_activities(entries)
        self.now = 15
        self.fail_prefix = 'Farm'
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 35
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 3)
        self.fail_prefix = None
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 3)
        self.assertEqual(self.farm.activities[0]['next_due'], 40)
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 0)

    def test_schedule_cap_discards_extra_slots_without_shifting_deadline(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=3, max_pending_runs=2)
        self.farm.configure_activities(entries)
        self.now = 55
        self.farm.queue_activity('A', 1)
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 5)
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 6)
        self.assertEqual(self.farm.activities[0]['next_due'], 60)
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)

    def restart_with_activity_state(self, root, entries):
        previous = self.farm
        self.farm = FarmRuntime(graph('Farm'), previous.modules, previous.effect,
                                previous.queued, previous.failure_notice,
                                lambda: 0, source='android_manual')
        self.farm.configure_activities(entries, activity_state=ActivityScheduleState(root),
                                       activity_clock=lambda: self.now)

    def test_activity_deadline_accumulates_while_farm_is_stopped(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=0, interval_seconds=10800, max_pending_runs=2)
        with tempfile.TemporaryDirectory() as root:
            self.restart_with_activity_state(root, entries)
            self.now = 7200
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 10800)
            self.now = 21600
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 2)
            self.round_boundary()
            self.finish_batch()
            self.assertEqual(self.calls.count('AFirst'), 2)
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 32400)
            self.round_boundary()
            self.assertFalse(self.farm.activity_pending)

    def test_restart_keeps_unstarted_batch_runs_and_manual_requests(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=2, interval_seconds=10800)
        with tempfile.TemporaryDirectory() as root:
            self.restart_with_activity_state(root, entries)
            self.farm.queue_activity('A', 1)
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['manual_runs'], 1)
            self.round_boundary()
            self.farm.tick(.2)
            self.assertTrue(self.farm.activity_active)
            self.farm.stop()
            self.restart_with_activity_state(root, entries)
            entry = self.farm.activities[0]
            self.assertEqual((entry['deferred_runs'], entry['initial_runs_left']), (2, 1))
            self.round_boundary()
            self.finish_batch()
            self.assertEqual(self.calls.count('AFirst'), 2)
            self.restart_with_activity_state(root, entries)
            entry = self.farm.activities[0]
            self.assertEqual((entry['deferred_runs'], entry['initial_runs_left']), (0, 0))

    def test_zero_initial_runs_waits_full_interval_then_runs_once(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=0, interval_seconds=190*60)
        self.farm.configure_activities(entries)
        self.assertTrue(self.farm.activities[0]['initial_completed'])
        self.assertEqual(self.farm.activities[0]['next_due'], 190*60)
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 190*60-1
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 190*60
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 1)
        self.assertEqual(self.farm.activities[0]['next_due'], 2*190*60)

    def test_first_countdown_runs_two_now_then_one_early_then_normal_interval(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=2, interval_seconds=10800, first_delay_seconds=1800)
        self.farm.configure_activities(entries)
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 2)
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 2)
        self.assertEqual(self.farm.activities[0]['next_due'], 1800)
        self.now = 1799
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 1800
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 3)
        self.assertEqual(self.farm.activities[0]['next_due'], 12600)
        self.now = 12599
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 12600
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 4)

    def test_countdown_accumulates_if_initial_batch_cannot_run_yet_and_obeys_cap(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=2, interval_seconds=100, first_delay_seconds=20, max_pending_runs=2)
        self.farm.configure_activities(entries)
        self.now = 320
        self.assertEqual(self.farm.activity_snapshot()['entries'][0]['scheduled_pending_runs'], 4)
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 4)
        self.assertEqual(self.farm.activities[0]['next_due'], 420)

    def test_countdown_restart_keeps_early_deadline_and_consumed_initial_runs(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=2, interval_seconds=10800, first_delay_seconds=1800)
        with tempfile.TemporaryDirectory() as root:
            self.restart_with_activity_state(root, entries)
            self.round_boundary()
            self.finish_batch()
            self.now = 900
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 1800)
            self.assertEqual(self.farm.activities[0]['initial_runs_left'], 0)
            self.round_boundary()
            self.assertFalse(self.farm.activity_pending)
            self.now = 1800
            self.round_boundary()
            self.finish_batch()
            self.assertEqual(self.calls.count('AFirst'), 3)
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 12600)

    def test_zero_initial_custom_countdown_and_zero_delay(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=0, first_delay_seconds=20)
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.now = 20
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 1)
        entries[0].update(initial_runs=2, first_delay_seconds=0)
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 4)

    def test_custom_countdown_change_resets_but_legacy_state_remains_compatible(self):
        entries = self.entries()[:1]
        entries[0].update(initial_runs=0, interval_seconds=10800)
        with tempfile.TemporaryDirectory() as root:
            self.restart_with_activity_state(root, entries)
            self.now = 900
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 10800)
            entries[0]['first_delay_seconds'] = 1800
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 2700)
            self.now = 1000
            self.restart_with_activity_state(root, entries)
            self.assertEqual(self.farm.activities[0]['next_due'], 2700)

    def test_custom_countdown_invalid_values_are_rejected(self):
        for value in (-1, True, 1.5, float('nan'), '30', 365*86400+1):
            entries = self.entries()[:1]
            entries[0]['first_delay_seconds'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.farm.configure_activities(entries)

    def test_external_workers_drain_while_parent_holds_round_boundary(self):
        ready = [False]
        self.configure(activity_ready=lambda:ready[0])
        self.round_boundary()
        previous_calls = list(self.calls)
        self.farm.tick(.2)
        self.assertTrue(self.farm.activity_pending)
        self.assertFalse(self.farm.activity_active)
        self.assertEqual(self.calls, previous_calls)
        ready[0] = True
        self.farm.tick(.2)
        self.assertTrue(self.farm.activity_active)

    def test_failed_round_never_launches_due_activities(self):
        self.configure()
        self.fail_prefix = 'Farm'
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.assertFalse(self.farm.activity_active)
        self.assertFalse(self.farm.activities[0]['initial_completed'])
        self.assertEqual(self.farm.activity_history[-1]['status'], 'INSERTION_DEFERRED')

    def test_skipping_terminal_chain_is_not_a_successful_round_for_insertion(self):
        self.configure()
        self.farm.schedule.by_id['FarmStart']['continue_condition'] = {
            'field':'progress_km','op':'lte','value':100}
        self.farm.observe = lambda rule:{'progress_km':200}
        self.round_boundary()
        self.assertEqual(self.farm.schedule.total_runs, {})
        self.assertFalse(self.farm.activity_pending)

    def test_earlier_legal_skip_allows_insertion_after_last_chain_end(self):
        data = graph('Farm')
        data['nodes'][0]['chain']['continue_condition'] = {
            'field':'progress_km','op':'lte','value':100}
        final = graph('Final')
        final['nodes'][0]['chain']['order'] = 2
        data['nodes'].extend(final['nodes'])
        data['edges'].extend(final['edges'])
        self.farm = FarmRuntime(data, self.farm.modules, self.farm.effect,
            lambda request:None, lambda event:None, lambda:self.now,
            source='android_manual', observe=lambda rule:{'progress_km':200})
        self.configure()
        self.round_boundary()
        self.assertTrue(self.farm.activity_pending)
        self.assertEqual(self.farm.schedule.total_runs, {'FinalStart':1})

    def test_failed_memory_exit_prevents_insertion_even_after_successful_return(self):
        data = graph('Farm')
        data['nodes'][0]['chain'].update(type='memory', failure_limit=3, memory_retries=1)
        final = graph('Final')
        final['nodes'][0]['chain']['order'] = 2
        data['nodes'].extend(final['nodes'])
        data['edges'].extend(final['edges'])
        calls = []
        def effect(context):
            name = context['node']['id']
            calls.append(name)
            if name == 'FarmFirst' and calls.count(name) == 1:
                return {'status':'FAILED', 'action_completed':False}
            return {'status':'INPUT_SENT_UNVERIFIED', 'action_completed':True}
        self.farm = FarmRuntime(data, self.farm.modules, effect, lambda request:None,
                                lambda event:None, lambda:self.now, source='android_manual')
        self.configure()
        for _ in range(30):
            self.farm.tick(.2)
            if self.farm.schedule.rounds:
                break
        self.assertEqual(self.farm.schedule.rounds, 1)
        self.assertEqual(calls.count('FarmFirst'), 2)
        self.assertFalse(self.farm.activity_pending)
        self.assertEqual(self.farm.failed_chain_exits, 1)
        self.assertEqual(self.farm.activity_history[-1]['status'], 'INSERTION_DEFERRED')

    def test_canvas_skip_completion_is_respected_without_outer_end_requirement(self):
        entries = self.entries()[:1]
        entries[0]['graph']['nodes'][0]['chain']['continue_condition'] = {
            'field':'progress_km','op':'lte','value':100}
        self.farm.observe = lambda rule:{'progress_km':200}
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.farm.status, 'RUNNING')
        self.assertTrue(self.farm.activities[0]['initial_completed'])
        self.assertIn('SKIPPED', [e['status'] for e in self.farm.activity_history])
        self.assertNotIn('AFirst', self.calls)

    def test_old_parent_queued_work_is_discarded_at_activity_ownership_switch(self):
        self.configure()
        self.round_boundary()
        self.farm.watchers.queue.append({'task_id':'stale'})
        self.farm.watchers.handoff_requests.append({'target':'stale'})
        self.farm.next_routes.append({'target':'stale'})
        self.farm.tick(.2)
        self.assertEqual(self.farm.watchers.queue, [])
        self.assertEqual(self.farm.watchers.handoff_requests, [])
        self.assertEqual(self.farm.next_routes, [])

    def test_failed_chain_finishes_but_next_activity_waits_for_safe_farm_round(self):
        self.configure()
        self.round_boundary()
        self.fail_prefix = 'A'
        self.tick_until(lambda: 'AFirst' in self.calls)
        self.assertEqual(self.farm.status, 'RUNNING')
        self.assertEqual(self.calls.count('AFirst'), 1)
        self.assertNotIn('ASecond', self.calls)
        self.assertEqual(self.farm.schedule.total_failures, {})
        self.finish_batch()
        self.assertEqual(self.farm.status, 'RUNNING')
        self.assertNotIn('BFirst', self.calls)
        self.assertEqual(next(e for e in self.farm.activity_history
                         if e['status']=='COMPLETED_WITH_FAILURES')['chain_failures'], {'AStart':1})
        self.round_boundary()
        self.finish_batch()
        self.assertIn('BFirst', self.calls)

    def test_failed_activity_chain_hands_off_to_successors_and_settlement(self):
        entries = self.entries()[:1]
        data = entries[0]['graph']
        for order, prefix in ((2, 'C3'), (3, 'Settlement')):
            successor = graph(prefix)
            successor['nodes'][0]['chain']['order'] = order
            data['nodes'].extend(successor['nodes'])
            data['edges'].extend(successor['edges'])
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.fail_prefix = 'A'
        self.tick_until(lambda: 'AFirst' in self.calls)
        self.assertEqual(self.farm.status, 'RUNNING')
        self.assertTrue(self.farm.activity_pending)
        self.finish_batch()
        self.assertIn('C3First', self.calls)
        self.assertIn('SettlementSecond', self.calls)
        self.assertEqual(self.farm.status, 'RUNNING')
        event = next(e for e in self.farm.activity_history if e['status']=='COMPLETED_WITH_FAILURES')
        self.assertEqual(event['chain_failures'], {'AStart':1})
        self.assertEqual(event['chain_runs'], {'C3Start':1, 'SettlementStart':1})
        self.assertEqual(self.farm.schedule.total_failures, {})

    def test_terminal_chain_failure_uses_canvas_round_exit_and_restores_parent(self):
        entries = self.entries()[:1]
        cleanup = graph('Settlement')
        cleanup['nodes'][0]['chain']['order'] = 2
        entries[0]['graph']['nodes'].extend(cleanup['nodes'])
        entries[0]['graph']['edges'].extend(cleanup['edges'])
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.fail_prefix = 'Settlement'
        self.tick_until(lambda: 'SettlementFirst' in self.calls, limit=20)
        self.assertEqual(self.farm.status, 'RUNNING')
        self.finish_batch()
        self.assertEqual(self.farm.status, 'RUNNING')
        event = next(e for e in self.farm.activity_history if e['status']=='COMPLETED_WITH_FAILURES')
        self.assertEqual(event['status'], 'COMPLETED_WITH_FAILURES')
        self.assertEqual(event['chain_failures'], {'SettlementStart':1})

    def test_memory_chain_return_and_failure_handoff_remain_canvas_owned(self):
        entries = self.entries()[:1]
        data = entries[0]['graph']
        data['nodes'][0]['chain'].update(type='memory', failure_limit=2, memory_retries=1)
        for order, prefix in ((2, 'C3'), (3, 'Settlement')):
            successor = graph(prefix)
            successor['nodes'][0]['chain']['order'] = order
            data['nodes'].extend(successor['nodes'])
            data['edges'].extend(successor['edges'])
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.fail_prefix = 'A'
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 2)
        self.assertEqual(self.calls.count('C3First'), 1)
        self.assertEqual(self.calls.count('SettlementFirst'), 1)
        self.assertEqual(self.farm.status, 'RUNNING')

    def test_unsafe_activity_defers_initial_repeats_without_resetting_their_count(self):
        entries = self.entries()[:1]
        entries[0]['initial_runs'] = 3
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.fail_prefix = 'A'
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 1)
        self.assertEqual(self.farm.activities[0]['initial_runs_left'], 2)
        self.assertFalse(self.farm.activities[0]['initial_completed'])
        self.fail_prefix = None
        self.now = 5
        self.round_boundary()
        self.finish_batch()
        self.assertEqual(self.calls.count('AFirst'), 3)
        self.assertEqual(self.farm.activities[0]['initial_runs_left'], 0)
        self.assertTrue(self.farm.activities[0]['initial_completed'])
        self.assertEqual(self.farm.activities[0]['next_due'], 10)

    def test_user_stop_during_child_effect_is_not_reclassified_or_resumed(self):
        self.farm.configure_activities(self.entries()[:1])
        self.round_boundary()
        def stop_effect(context):
            self.farm.stop()
            return {'status':'BLOCKED', 'action_completed':False}
        self.farm.effect = stop_effect
        self.tick_until(lambda: self.farm.status != 'RUNNING')
        self.assertEqual(self.farm.status, 'STOPPED')
        self.assertFalse(self.farm.activity_pending)
        self.assertFalse(any(e['status'].startswith('COMPLETED') for e in self.farm.activity_history))

    def test_unknown_chain_reading_is_attached_to_exhaustion_evidence(self):
        evidence = []
        self.farm.exhaustion_callback = evidence.append
        self.farm.schedule.by_id['FarmStart']['continue_condition'] = {
            'field':'progress_km', 'op':'lte', 'value':100}
        detail = {'result':'UNKNOWN', 'roi':[330,370,156,55], 'tokens':[],
                  'reason':'no_numeric_token'}
        def observe(rule):
            self.farm.chain_reading = dict(detail)
            return {}
        self.farm.observe = observe
        self.farm.tick(.2)
        self.assertEqual(evidence[0]['chain_reading'], detail)
        detail['reason'] = 'changed_after_reporting'
        self.assertEqual(evidence[0]['chain_reading']['reason'], 'no_numeric_token')

    def test_final_requested_round_runs_due_batch_before_stopping(self):
        self.configure()
        self.farm.schedule.round_limit = 1
        self.round_boundary()
        self.assertEqual(self.farm.status, 'RUNNING')
        self.assertFalse(self.farm.schedule.active)
        self.finish_batch()
        self.assertEqual(self.farm.status, 'STOPPED')
        self.assertEqual(self.calls[-2:], ['CFirst','CSecond'])

    def test_stop_cancels_remaining_batch_without_resuming(self):
        self.configure()
        self.round_boundary()
        self.farm.tick(.2)
        child = self.farm.active_runtime
        self.farm.stop()
        self.assertEqual(child.status, 'STOPPED')
        self.assertEqual(self.farm.status, 'STOPPED')
        self.assertFalse(self.farm.activity_pending)
        self.assertNotIn(self.farm, self.switched)

    def test_no_due_activities_continue_next_farm_round_normally(self):
        self.configure(delayed=True)
        self.round_boundary()
        self.assertFalse(self.farm.activity_pending)
        self.assertEqual(self.farm.activity_history, [])
        self.assertEqual(self.farm.status, 'RUNNING')

    def test_independent_time_guards_can_be_supplied_per_activity(self):
        from src.rwtd.android_core.time_guard import TimeGuards
        guards = TimeGuards()
        entries = self.entries()
        entries[0]['time_guards'] = guards
        self.farm.configure_activities(entries)
        self.round_boundary()
        self.farm.tick(.2)
        self.assertIs(self.farm.active_runtime.time_guards, guards)

    def test_invalid_intervals_and_duplicate_ids_rejected_atomically(self):
        for interval in (0, -1, float('inf'), float('nan'), True, '190'):
            with self.assertRaises(ValueError):
                self.farm.configure_activity(graph('A'), interval)
        entries = self.entries()
        entries[1]['id'] = 'A'
        with self.assertRaises(ValueError):
            self.farm.configure_activities(entries)
        self.assertEqual(self.farm.activities, [])


if __name__ == '__main__':
    unittest.main()
