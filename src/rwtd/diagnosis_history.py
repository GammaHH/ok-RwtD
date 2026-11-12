"""Read saved RwtD failure evidence for local and Discord diagnostics."""
import json
import re
from datetime import datetime

from .android_core.module_store import safe_path


_DIRECTORY = 'docs/rwtd/exceptions'
_RECORD_ID = re.compile(r'exception_\d{8}T\d{6}_\d{6}Z_[a-f0-9]{8}')
_REPORT_LIMIT = 2 * 1024 * 1024
_PNG_LIMIT = 8 * 1024 * 1024


def _read(root, record_id, filename, limit):
    if not _RECORD_ID.fullmatch(record_id):
        raise ValueError('無效的診斷紀錄 ID')
    path = safe_path(root, f'{_DIRECTORY}/{record_id}/{filename}')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('診斷檔案超過讀取上限')
    return data


def report_ids(root):
    directory = safe_path(root, _DIRECTORY)
    return sorted((path.name for path in directory.glob('exception_*')
                   if _RECORD_ID.fullmatch(path.name)), reverse=True)


def read_report(root, record_id):
    report = json.loads(_read(root, record_id, 'report.json', _REPORT_LIMIT))
    if not isinstance(report, dict):
        raise ValueError('無效的診斷報告')
    return report


def read_image(root, record_id):
    try:
        return _read(root, record_id, 'analyzed_frame.png', _PNG_LIMIT)
    except (OSError, ValueError):
        return None


def recent_reports(root, limit=25):
    reports = []
    for record_id in report_ids(root):
        try:
            report = read_report(root, record_id)
        except (OSError, ValueError):
            continue  # A report may still be written or already retired.
        reports.append((record_id, report))
        if len(reports) >= limit:
            break
    return reports


def report_details(report):
    """Present saved evidence, including older truncated detail strings."""
    details = report.get('error_details', {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            # Older reports truncated the serialized details after 3000 chars.
            # Recover complete scalar fields while leaving incomplete values out.
            fields = {}
            for match in re.finditer(
                    r'"([^"\\]+)"\s*:\s*("(?:[^"\\]|\\.)*"|true|false|null|-?\d+(?:\.\d+)?)',
                    details):
                fields.setdefault(match[1], json.loads(match[2]))
            details = fields
    if not isinstance(details, dict):
        details = {}

    def text(value):
        if value is True:
            return '是'
        if value is False:
            return '否'
        return str(value) if isinstance(value, (str, int, float)) else '未記錄'

    def local_time(value):
        try:
            return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
        except (TypeError, ValueError):
            return text(value)

    categories = {
        'RECOGNITION_RETRY_EXHAUSTED': '辨識重試次數用完',
        'NODE_EXECUTION_LIMIT': '節點執行次數達上限',
        'WATCHDOG_STALLED': '看門狗偵測到流程停滯',
        'CHAIN_TIMEOUT': 'Chain 執行逾時',
    }
    category = report.get('exception_category')
    reason = details.get('reason') or report.get('description')
    if reason == 'No valid COCO batch; no guessed input':
        reason = '找不到符合條件的可操作 COCO 目標'
    rows = [
        ('失敗類型', categories.get(category, text(category))),
        ('失敗原因', text(reason)),
        ('發生時間', local_time(report.get('timestamp_utc'))),
        ('Canvas', text(details.get('canvas_name') or details.get('canvas_id'))),
        ('Chain', text(details.get('chain'))),
        ('節點', text(details.get('node') or report.get('task_name'))),
        ('模組', text(details.get('module'))),
    ]
    attempts, maximum = details.get('attempt_count'), details.get('attempt_limit')
    rows.append(('嘗試次數', text(attempts) + ' / ' + text(maximum)))
    recognition = details.get('recognition')
    if isinstance(recognition, dict):
        features = recognition.get('features')
        if isinstance(features, list):
            rows.append(('辨識目標', '、'.join(str(value) for value in features)))
        if recognition.get('threshold') is not None:
            rows.append(('信心門檻', text(recognition['threshold'])))
    from .android_core.ocr_diagnostics import detail_rows, report_summary
    ocr_summary = report.get('ocr_summary')
    if not isinstance(ocr_summary, dict):
        ocr_summary = report_summary(report.get('ocr_records'), details.get('ocr_retry_history'))
    rows.extend(detail_rows(ocr_summary))
    for region in report.get('screenshot_overlays') or []:
        if isinstance(region, dict) and isinstance(region.get('roi'), list):
            rows.append(('搜尋區域 ' + text(region.get('label')), ', '.join(map(str, region['roi']))))
    device = report.get('device_state')
    if isinstance(device, dict):
        rows.extend([('裝置', text(device.get('model'))),
                     ('裝置已連線', text(device.get('connected')))])
    rows.extend([
        ('截圖時間', local_time(report.get('frame_timestamp_utc'))),
        ('截圖是否過期', text(report.get('stale'))),
        ('最近成功步驟', text(report.get('last_successful_step'))),
        ('復原嘗試次數', text(report.get('recovery_attempts'))),
        ('已要求停止', text(report.get('stop_requested'))),
    ])
    return rows
