import copy
import json
import unittest

from src.rwtd.android_core.activity_settings import (
    CANVAS_KEY, DAYS_KEY, DEFAULTS, ENABLED_KEY, HOURS_KEY, MAX_INTERVAL_SECONDS,
    MINUTES_KEY, SCHEDULES_KEY, resolve_activity_config, resolve_activity_schedules,
)


class ActivitySettingsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [{'id': 'farm', 'name': 'Main'}, {'id': 'event', 'name': 'Activity'}]
        self.config = {**DEFAULTS, ENABLED_KEY: True, CANVAS_KEY: 'event', MINUTES_KEY: 190}

    def resolve(self, config=None):
        return resolve_activity_config(self.config if config is None else config, self.catalog, 'farm')

    def test_disabled_has_no_schedule_and_ignores_stale_selection(self):
        self.assertIsNone(self.resolve({}))
        self.assertIsNone(self.resolve({ENABLED_KEY: False, CANVAS_KEY: 'deleted', MINUTES_KEY: -1}))

    def test_minutes_are_customizable_not_a_fixed_190(self):
        for minutes in (1, 190, 241):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.resolve({**self.config, MINUTES_KEY: minutes}),
                                 {'canvas_id': 'event', 'interval_seconds': minutes * 60})

    def test_units_sum_and_display_label_uses_id(self):
        self.config.update({CANVAS_KEY: 'event · Old activity name', DAYS_KEY: 1,
                            HOURS_KEY: 2, MINUTES_KEY: 3})
        before = copy.deepcopy(self.config)
        self.assertEqual(self.resolve(), {'canvas_id': 'event', 'interval_seconds': 93780})
        self.assertEqual(self.config, before)

    def test_missing_deleted_or_main_canvas_is_rejected(self):
        for selection in ('', 'missing', 'farm', 'farm · Main', None):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.resolve({**self.config, CANVAS_KEY: selection})

    def test_invalid_interval_values_are_rejected(self):
        for key in (DAYS_KEY, HOURS_KEY, MINUTES_KEY):
            for value in (-1, True, False, 1.5, '3', None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.resolve({**self.config, key: value})

    def test_zero_and_excessive_total_are_rejected(self):
        for values in ({MINUTES_KEY: 0}, {DAYS_KEY: 366},
                       {DAYS_KEY: 365, MINUTES_KEY: 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.resolve({**self.config, MINUTES_KEY: 0, **values})
        self.assertEqual(self.resolve({**self.config, MINUTES_KEY: 0, DAYS_KEY: 365})['interval_seconds'],
                         MAX_INTERVAL_SECONDS)

    def test_enabled_requires_bool(self):
        for value in (0, 1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve({**self.config, ENABLED_KEY: value})


class MultipleActivitySettingsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [{'id': 'farm'}, {'id': 'event-a'}, {'id': 'event-b'}]
        self.row = {'id': 'daily', 'canvas': 'event-a', 'enabled': True,
                    'interval_days': 0, 'interval_hours': 3, 'interval_minutes': 10}

    def resolve(self, rows):
        return resolve_activity_schedules(
            {ENABLED_KEY: True, SCHEDULES_KEY: json.dumps(rows)}, self.catalog, 'farm')

    def test_empty_default_is_disabled(self):
        self.assertEqual(resolve_activity_schedules(DEFAULTS, self.catalog, 'farm'), [])
        self.assertEqual(self.resolve([]), [])

    def test_multiple_intervals_and_order(self):
        second = {**self.row, 'id': 'weekly', 'canvas': 'event-b · Renamed',
                  'interval_days': 2, 'interval_hours': 0, 'interval_minutes': 7}
        self.assertEqual(self.resolve([second, self.row]), [
            {'id': 'weekly', 'initial_runs': 1, 'canvas_id': 'event-b', 'interval_seconds': 173220},
            {'id': 'daily', 'initial_runs': 1, 'canvas_id': 'event-a', 'interval_seconds': 11400},
        ])

    def test_disabled_row_may_keep_stale_interval_and_canvas(self):
        self.assertEqual(self.resolve([{'id': 'draft', 'enabled': False,
                                       'canvas': 'deleted', 'interval_minutes': -1}, self.row]),
                         [{'id': 'daily', 'initial_runs': 1, 'canvas_id': 'event-a', 'interval_seconds': 11400}])

    def test_duplicate_id_is_rejected_even_when_disabled(self):
        with self.assertRaises(ValueError):
            self.resolve([self.row, {'id': 'daily', 'enabled': False}])

    def test_duplicate_enabled_canvas_is_rejected(self):
        with self.assertRaises(ValueError):
            self.resolve([self.row, {**self.row, 'id': 'other', 'canvas': 'event-a · Name'}])

    def test_missing_ids_bad_values_and_unknown_fields_are_rejected(self):
        for row in ([], {}, {**self.row, 'id': ' '}, {**self.row, 'id': True},
                    {**self.row, 'id': 'x' * 81}, {**self.row, 'enabled': 'yes'},
                    {**self.row, 'interval_hours': True}, {**self.row, 'extra': 1},
                    {**self.row, 'canvas': 'farm'}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.resolve([row])

    def test_bad_top_level_json_is_rejected(self):
        for raw in ('{', '{}', 'null', '4', 4):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                resolve_activity_schedules({ENABLED_KEY: True, SCHEDULES_KEY: raw}, self.catalog, 'farm')

    def test_legacy_saved_config_remains_readable(self):
        config = {ENABLED_KEY: True, CANVAS_KEY: 'event-a', MINUTES_KEY: 42}
        self.assertEqual(resolve_activity_schedules(config, self.catalog, 'farm'),
                         [{'id': 'legacy', 'initial_runs': 1, 'canvas_id': 'event-a', 'interval_seconds': 2520}])

    def test_initial_batch_count_is_customizable(self):
        row = {'id': 'event1', 'canvas': 'event-a', 'initial_runs': 3, 'interval_minutes': 190}
        self.assertEqual(self.resolve([row]), [
            {'id': 'event1', 'canvas_id': 'event-a', 'initial_runs': 3, 'interval_seconds': 11400}])
        self.assertEqual(self.resolve([{**row, 'initial_runs': 0}])[0]['initial_runs'], 0)
        for value in (-1, True, 1.5, '3', 1001):
            with self.subTest(initial_runs=value), self.assertRaises(ValueError):
                self.resolve([{**row, 'initial_runs': value}])

    def test_structured_list_is_accepted_without_mutation(self):
        rows = [copy.deepcopy(self.row)]
        config = {ENABLED_KEY: True, SCHEDULES_KEY: rows}
        before = copy.deepcopy(config)
        self.assertEqual(resolve_activity_schedules(config, self.catalog, 'farm')[0]['id'], 'daily')
        self.assertEqual(config, before)

    def test_max_pending_runs_is_optional_and_validated(self):
        self.assertEqual(self.resolve([{**self.row, 'max_pending_runs': 3}])[0]['max_pending_runs'], 3)
        self.assertNotIn('max_pending_runs', self.resolve([{**self.row, 'max_pending_runs': 0}])[0])
        for value in (-1, True, 1.5, '3', 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve([{**self.row, 'max_pending_runs': value}])

    def test_optional_first_recovery_countdown(self):
        self.assertNotIn('first_delay_seconds', self.resolve([self.row])[0])
        row = {**self.row, 'initial_runs': 2, 'first_delay_days': 1,
               'first_delay_hours': 2, 'first_delay_minutes': 3}
        resolved = self.resolve([row])[0]
        self.assertEqual(resolved['initial_runs'], 2)
        self.assertEqual(resolved['first_delay_seconds'], 93780)
        self.assertEqual(self.resolve([{**self.row, 'first_delay_minutes': 0}])[0]['first_delay_seconds'], 0)
        for value in (-1, True, 1.5, '3', None, 525601):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve([{**self.row, 'first_delay_minutes': value}])


if __name__ == '__main__':
    unittest.main()
