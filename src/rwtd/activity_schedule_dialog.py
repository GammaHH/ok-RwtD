"""Native OK activity editor; persistence retains the scheduler's JSON contract."""
import json
import uuid

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from src.rwtd.android_core.activity_settings import (
    ENABLED_KEY, SCHEDULES_KEY, resolve_activity_schedules,
)


class ActivityScheduleDialog(QDialog):
    def __init__(self, config, catalog, main_canvas_id, parent=None):
        super().__init__(parent)
        self.config, self.catalog, self.main_canvas_id = config, catalog, main_canvas_id
        raw = config.get(SCHEDULES_KEY, '[]')
        self.original = raw
        rows = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('原活動設定不是活動清單，未覆寫原資料')
        self.rows = []
        self.setWindowTitle('活動排程設定')
        self.resize(880, 650)
        layout = QVBoxLayout(self)
        hint = QLabel('FARM 正常整輪結束後，依列表順序執行到期活動。\n'
                      '首次次數設 0：已刷完，先等滿間隔；設 3：首次連跑三次。\n'
                      '可選首次回復倒數：首次次數先跑，倒數決定額外一場何時回復，之後依原間隔累積。\n'
                      '倒數從排程啟用時計算，重啟保留；修改倒數設定會重新計時。儲存後於下次啟動 FARM 生效。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.row_layout = QVBoxLayout(container)
        self.row_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)
        add = QPushButton('＋ 新增活動')
        add.clicked.connect(lambda: self.add_row({}))
        layout.addWidget(add)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('儲存')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for row in rows:
            self.add_row(row)

    @staticmethod
    def spin(value, maximum):
        widget = QSpinBox()
        widget.setRange(0, maximum)
        widget.setValue(value if type(value) is int and value >= 0 else 0)
        return widget

    def add_row(self, saved):
        box = QGroupBox('活動')
        layout = QVBoxLayout(box)
        header = QHBoxLayout()
        enabled = QCheckBox('啟用')
        enabled.setChecked(saved.get('enabled', True) is True)
        name = QLineEdit(str(saved.get('id', 'event_'+uuid.uuid4().hex[:8])))
        header.addWidget(enabled)
        header.addWidget(QLabel('識別名稱'))
        header.addWidget(name)
        up, down, remove = (QPushButton(text) for text in ('↑', '↓', '移除'))
        for button in (up, down, remove):
            header.addWidget(button)
        layout.addLayout(header)
        form = QFormLayout()
        canvas = QComboBox()
        canvas.addItem('請選擇活動 Canvas', '')
        for item in self.catalog:
            if item['id'] != self.main_canvas_id:
                canvas.addItem(item['name']+' · '+item['id'], item['id'])
        selected = str(saved.get('canvas', '')).split(' · ', 1)[0].strip()
        index = canvas.findData(selected)
        if selected and index < 0:
            canvas.addItem('無法使用：'+selected, selected)
            index = canvas.count()-1
        canvas.setCurrentIndex(max(index, 0))
        form.addRow('活動 Canvas', canvas)
        initial = self.spin(saved.get('initial_runs', 1), 1000)
        form.addRow('首次連跑次數（0＝先等待）', initial)
        delay_keys = ('first_delay_days', 'first_delay_hours', 'first_delay_minutes')
        use_delay = QCheckBox('自訂首次回復倒數（可選）')
        use_delay.setChecked(any(key in saved for key in delay_keys))
        form.addRow(use_delay)
        delay_box = QWidget()
        delay_layout = QHBoxLayout(delay_box)
        delay_units = []
        for key, label, maximum in zip(delay_keys, ('天', '小時', '分鐘'), (365, 8760, 525600)):
            field = self.spin(saved.get(key, 0), maximum)
            delay_layout.addWidget(field)
            delay_layout.addWidget(QLabel(label))
            delay_units.append(field)
        delay_box.setVisible(use_delay.isChecked())
        use_delay.toggled.connect(delay_box.setVisible)
        form.addRow('首次倒數', delay_box)
        interval = QHBoxLayout()
        units = []
        for key, label, maximum in (('interval_days','天',365),
                                    ('interval_hours','小時',8760),
                                    ('interval_minutes','分鐘',525600)):
            value = saved.get(key, 0)
            # Disabled drafts may have invalid old values; show editable zero
            # instead of crashing the editor. Nothing is persisted until Save.
            field = self.spin(value if type(value) is int and value >= 0 else 0, maximum)
            interval.addWidget(field)
            interval.addWidget(QLabel(label))
            units.append(field)
        form.addRow('排程間隔（每次到期累積一場）', interval)
        max_pending = self.spin(saved.get('max_pending_runs', 0), 1000)
        form.addRow('定期排程最多累積場數（0＝不限）', max_pending)
        layout.addLayout(form)
        row = dict(box=box, enabled=enabled, name=name, canvas=canvas,
                   initial=initial, units=units, max_pending=max_pending, extra=dict(saved),
                   use_delay=use_delay, delay_units=delay_units)
        self.rows.append(row)
        self.row_layout.insertWidget(len(self.rows)-1, box)
        remove.clicked.connect(lambda: self.remove_row(row))
        up.clicked.connect(lambda: self.move_row(row, -1))
        down.clicked.connect(lambda: self.move_row(row, 1))

    def remove_row(self, row):
        self.rows.remove(row)
        self.row_layout.removeWidget(row['box'])
        row['box'].deleteLater()

    def move_row(self, row, delta):
        index = self.rows.index(row)
        target = index+delta
        if not 0 <= target < len(self.rows):
            return
        self.rows.insert(target, self.rows.pop(index))
        self.row_layout.removeWidget(row['box'])
        self.row_layout.insertWidget(target, row['box'])

    def save(self):
        rows = []
        for row in self.rows:
            days, hours, minutes = (widget.value() for widget in row['units'])
            rows.append(dict(row['extra'], id=row['name'].text().strip(),
                enabled=row['enabled'].isChecked(), canvas=row['canvas'].currentData(),
                initial_runs=row['initial'].value(), interval_days=days,
                interval_hours=hours, interval_minutes=minutes, max_pending_runs=row['max_pending'].value()))
            for key, widget in zip(('first_delay_days', 'first_delay_hours', 'first_delay_minutes'), row['delay_units']):
                rows[-1].pop(key, None)
                if row['use_delay'].isChecked():
                    rows[-1][key] = widget.value()
        try:
            resolve_activity_schedules({ENABLED_KEY: True, SCHEDULES_KEY: rows},
                                      self.catalog, self.main_canvas_id)
            if self.config.get(SCHEDULES_KEY, '[]') != self.original:
                raise ValueError('活動設定已由其他入口變更，請取消並重新開啟表單')
            self.config[SCHEDULES_KEY] = json.dumps(rows, ensure_ascii=False, indent=2)
        except ValueError as exc:
            QMessageBox.warning(self, '無法儲存活動', str(exc))
            return
        self.accept()
