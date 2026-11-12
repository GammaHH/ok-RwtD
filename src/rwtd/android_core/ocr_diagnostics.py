"""Compact OCR candidates shared by Discord and native diagnostics; never routing."""
import copy
import math
import re
import unicodedata

NUMBER = re.compile(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\Z')


def highest(candidates):
    return copy.deepcopy(max(candidates, key=lambda c: (c['confidence'], c.get('_rank', 0)))) if candidates else None


def summarize(records, node=None):
    candidates, numbers, converted = [], [], []
    for record in (records or [])[-16:]:
        if not isinstance(record, dict):
            continue
        if node and record.get('node') not in (None, node['id']):
            continue
        for token in (record.get('tokens') or [])[:128]:
            if not isinstance(token, dict):
                continue
            text, score = token.get('text'), token.get('confidence')
            if (not isinstance(text, str) or type(score) not in (int, float)
                    or not math.isfinite(score) or not 0 <= score <= 1):
                continue
            item = dict(text=text[:512], confidence=score, threshold=record.get('threshold'),
                        route=record.get('route'), reason=record.get('reason'), result=record.get('result'),
                        frame_id=record.get('frame_id'), captured_utc=record.get('captured_utc'),
                        _rank={'engine': 0, 'fallback': 1, 'parsed': 2}.get(record.get('stage'), 0))
            candidates.append(item)
            normalized = unicodedata.normalize('NFKC', text).strip()
            if NUMBER.fullmatch(normalized):
                value = float(normalized.replace(',', ''))
                if math.isfinite(value):
                    numbers.append(dict(item, value=value))
            if node and node.get('ocr_mode') == 'raw':
                from .canvas_ocr import numeric_text, output_type, read_value
                if output_type(node) == 'number' and NUMBER.fullmatch(numeric_text(node, normalized)):
                    value = read_value(node, [text])
                    if type(value) in (int, float) and math.isfinite(value):
                        converted.append(dict(item, value=value))
    result = dict(best_text=highest(candidates), best_number=highest(numbers),
                  best_converted=highest(converted))
    for candidate in result.values():
        if candidate:
            candidate.pop('_rank', None)
    return result


def report_summary(records, trials=None):
    result = {'current_frame': summarize(records)}
    history = copy.deepcopy(trials[-100:]) if isinstance(trials, list) else []
    if history:
        result['retry_history'] = history
        for key in ('best_text', 'best_number', 'best_converted'):
            candidates = [dict(row[key], attempt=row.get('attempt')) for row in history
                          if isinstance(row, dict) and isinstance(row.get(key), dict)]
            result['retry_' + key] = highest(candidates)
    return result


def candidate_text(candidate):
    if not isinstance(candidate, dict):
        return '未取得候選（非信心 0%）'
    score = candidate.get('confidence')
    if type(score) not in (int, float) or not math.isfinite(score):
        return '信心值未記錄'
    text = str(candidate.get('text', ''))[:100].replace('\n', ' ')
    result = f'「{text}」 · {score * 100:.2f}%'
    if candidate.get('value') is not None:
        result += ' · 數值 ' + str(candidate['value'])
    threshold = candidate.get('threshold')
    if type(threshold) in (int, float) and math.isfinite(threshold) and score < threshold:
        result += f' · 低於門檻 {threshold * 100:.2f}%'
    if candidate.get('attempt') is not None:
        result += ' · 第 ' + str(candidate['attempt']) + ' 次'
    return result


def detail_rows(summary):
    if not isinstance(summary, dict):
        return []
    current = summary.get('current_frame') or {}
    # Do not add irrelevant empty OCR rows to pure COCO reports.
    if not current.get('best_text') and not summary.get('retry_history'):
        return []
    rows = [('當次畫面最高信心 OCR', candidate_text(current.get('best_text'))),
            ('當次畫面最高信心數字候選', candidate_text(current.get('best_number')))]
    best = current.get('best_text') or {}
    if best.get('reason'):
        reasons = {'below_threshold': '低於信心門檻', 'not_a_complete_number': '不是完整數字',
                   'numeric_conversion_rejected': '數字轉換未通過',
                   'OCR output empty, ambiguous or conversion rule did not match': '沒有可轉換的唯一數字／轉換規則未通過'}
        rows.append(('當次候選狀態', reasons.get(best['reason'], str(best['reason'])[:200])))
    if summary.get('retry_history'):
        rows.extend([('本次重試最高信心 OCR', candidate_text(summary.get('retry_best_text'))),
                     ('本次重試最高信心數字候選', candidate_text(summary.get('retry_best_number')))])
        if summary.get('retry_best_converted'):
            rows.append(('依設定轉換的最高信心候選', candidate_text(summary['retry_best_converted'])))
        rows.append(('各次重試（歷史文字，非當次截圖）', '\n'.join(
            '第 ' + str(row.get('attempt', '?')) + ' 次：' + candidate_text(row.get('best_text'))
            for row in summary['retry_history'] if isinstance(row, dict))))
    return rows


def notification_text(summary):
    rows = detail_rows(summary)
    return '\n'.join(label + ': ' + value for label, value in rows
                     if label != '各次重試（歷史文字，非當次截圖）')[:750]
