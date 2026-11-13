"""Real Qt form round-trip checks; no app executor or phone is created."""
import json
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from src.rwtd.activity_schedule_dialog import ActivityScheduleDialog
from src.rwtd.android_core.activity_settings import SCHEDULES_KEY


class CountdownEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self, **extra):
        row = dict(id='event', canvas='activity', initial_runs=2, interval_hours=3, **extra)
        config = {SCHEDULES_KEY: json.dumps([row])}
        dialog = ActivityScheduleDialog(config, [{'id': 'farm', 'name': 'Farm'},
                                                {'id': 'activity', 'name': 'Activity'}], 'farm')
        self.addCleanup(dialog.deleteLater)
        return config, dialog

    def test_old_schedule_stays_without_countdown(self):
        config, dialog = self.make_dialog()
        self.assertFalse(dialog.rows[0]['use_delay'].isChecked())
        dialog.save()
        row = json.loads(config[SCHEDULES_KEY])[0]
        self.assertFalse(any(key.startswith('first_delay_') for key in row))
        self.assertEqual((row['initial_runs'], row['interval_hours']), (2, 3))

    def test_optional_countdown_saved_and_reopened(self):
        config, dialog = self.make_dialog()
        dialog.rows[0]['use_delay'].setChecked(True)
        dialog.rows[0]['delay_units'][2].setValue(30)
        dialog.save()
        row = json.loads(config[SCHEDULES_KEY])[0]
        self.assertEqual(row['first_delay_minutes'], 30)
        _, reopened = self.make_dialog(first_delay_minutes=row['first_delay_minutes'])
        self.assertTrue(reopened.rows[0]['use_delay'].isChecked())
        self.assertEqual(reopened.rows[0]['delay_units'][2].value(), 30)

    def test_unchecking_removes_saved_countdown(self):
        config, dialog = self.make_dialog(first_delay_days=0, first_delay_hours=1, first_delay_minutes=15)
        dialog.rows[0]['use_delay'].setChecked(False)
        dialog.save()
        self.assertFalse(any(key.startswith('first_delay_') for key in json.loads(config[SCHEDULES_KEY])[0]))
