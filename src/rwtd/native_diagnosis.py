"""Official diagnosis UI with RwtD failure screenshots included in its history."""
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialog, QGridLayout, QListWidgetItem, QScrollArea, QVBoxLayout, QWidget

from ok import og
from ok.ui.qt.debug.diagnosis_page import DiagnosisPage, MAX_SHOTS, Shot, list_shots
from ok.ui.qt.kit.icons import icon
from ok.ui.qt.kit.buttons import Button
from ok.ui.qt.kit.labels import Label
from ok.ui.qt.kit.theme import theme
from ok.ui.qt.shell import diagnostics

from .android_core.module_store import safe_path
from .diagnosis_history import recent_reports, report_details


class RwtdShot(Shot):
    @property
    def title(self):
        return getattr(self, 'rwtd_title', self.stem)


class RwtdDiagnosisPage(DiagnosisPage):
    name = 'RwtD 失敗診斷'
    add_after_default_tabs = True

    def __init__(self):
        self.root = Path(__file__).resolve().parents[2]
        super().__init__(exit_event=og.exit_event, debug=og.config.get('debug', False))
        self.setObjectName('rwtdDiagnosisPage')
        self.icon = icon('pulse')
        self.details_button = Button('詳細資訊', 'file')
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(self.show_details)
        self.layout().itemAt(0).layout().addWidget(self.details_button)
        self.ocr_summary_label = Label('', 'caption', self, word_wrap=True)
        self.ocr_summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.ocr_summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout().insertWidget(1, self.ocr_summary_label)
        self._show_selection()

    def refresh(self):
        shots = list_shots(diagnostics.screenshot_folder())
        try:
            reports = recent_reports(self.root)
        except (OSError, ValueError):
            reports = []
        for record_id, report in reports:
            try:
                path = safe_path(self.root, f'docs/rwtd/exceptions/{record_id}/analyzed_frame.png')
                if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                shot = RwtdShot(record_id, path.stat().st_mtime, {'boxed': str(path)})
                shot.rwtd_report = report
                shot.rwtd_title = str(report.get('task_name') or 'RwtD') + ' · ' + str(
                    report.get('exception_category') or '診斷')
                shots.append(shot)
            except (OSError, ValueError):
                continue
        shots = sorted(shots, key=lambda shot: shot.mtime, reverse=True)[:MAX_SHOTS]
        signature = [(shot.stem, shot.mtime, tuple(shot.files.items())) for shot in shots]
        if signature == self._signature:
            return
        current = self.current_shot()
        follow = current is None or self.shot_list.currentRow() == 0
        self._signature = signature
        self.shots = shots
        self.shot_list.blockSignals(True)
        try:
            self.shot_list.clear()
            for shot in shots:
                item = QListWidgetItem(f'{shot.title}\n{shot.time_text}')
                item.setToolTip(shot.path())
                item.setSizeHint(QSize(0, 52))
                self.shot_list.addItem(item)
            row = 0 if follow else next(
                (index for index, shot in enumerate(shots) if shot.stem == current.stem), 0)
            if shots:
                self.shot_list.setCurrentRow(row)
        finally:
            self.shot_list.blockSignals(False)
        self.count_label.setText(str(len(shots)) if shots else '')
        self._show_selection()

    def _show_selection(self):
        super()._show_selection()
        if hasattr(self, 'details_button'):
            shot = self.current_shot()
            self.details_button.setEnabled(hasattr(shot, 'rwtd_report'))
        if hasattr(self, 'ocr_summary_label'):
            report = getattr(self.current_shot(), 'rwtd_report', None)
            labels = {'當次畫面最高信心 OCR', '當次畫面最高信心數字候選',
                      '本次重試最高信心 OCR', '本次重試最高信心數字候選'}
            rows = report_details(report) if report is not None else []
            text = '\n'.join(name + '：' + value for name, value in rows if name in labels)
            self.ocr_summary_label.setText(text)
            self.ocr_summary_label.setVisible(bool(text))

    def show_details(self):
        shot = self.current_shot()
        report = getattr(shot, 'rwtd_report', None)
        if report is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('診斷詳細資訊')
        dialog.resize(720, 640)
        content = QWidget()
        fields = QGridLayout(content)
        fields.setColumnStretch(1, 1)
        fields.setHorizontalSpacing(24)
        fields.setVerticalSpacing(14)
        for row, (name, value) in enumerate(report_details(report)):
            label = Label(name, 'caption', content)
            label.setAlignment(Qt.AlignmentFlag.AlignTop)
            detail = Label(value, 'body', content, word_wrap=True)
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            fields.addWidget(label, row, 0)
            fields.addWidget(detail, row, 1)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        close = Button('關閉')
        close.clicked.connect(dialog.accept)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(Label('診斷詳細資訊', 'subtitle', dialog))
        layout.addWidget(scroll, 1)
        layout.addWidget(close)
        theme.install(dialog)
        dialog.exec()
