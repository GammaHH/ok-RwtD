"""User-run SQLite/legacy-log round statistics; no device access."""
import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.rwtd.android_core.round_timing_store import (
    display_version, full_version, version_parts, version_matches,
)

MARKER = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*RwtdFarmTask:info_set Farm rounds (\d+)\s*$')
CANVAS = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}.*RwtdFarmTask:(Farm Canvas[：:]|info_set Canvas)\s*(.*)$')
STAGE = re.compile(r'第\s*([一二三四五1-5])\s*關')
STAGE_NUMBERS = dict(zip('一二三四五12345', (1,2,3,4,5,1,2,3,4,5)))
CANVAS_ID = re.compile(r'\s*\[(Canvas_[^\]]+)\]\s*$')
CANVAS_VERSION = re.compile(r'(?i)(?<![A-Za-z0-9])v(\d+(?:\.\d+)*)\b')


def parse_time(value):
    if not value:
        return None
    for pattern in ('%Y-%m-%d %H:%M:%S,%f', '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    raise ValueError('Use local log time: YYYY-MM-DD HH:MM:SS or YYYY-MM-DD HH:MM:SS,fff')


def select_rounds(path, count, stage, before=None, version_filter=None, from_version=None, to_version=None):
    """Stream round boundaries and retain at most N rows per Canvas/version."""
    remaining = path.stat().st_size
    previous = None
    session = 0
    buckets = defaultdict(lambda: deque(maxlen=count))
    available = Counter()
    gaps = 0
    canvas_name = None
    canvas_id = None
    current_stage = None
    with path.open('rb') as stream:
        line = 0
        while remaining:
            raw = stream.readline(remaining)
            if not raw:
                break
            remaining -= len(raw)
            line += 1
            text = raw.decode('utf-8-sig', errors='replace').rstrip()
            canvas = CANVAS.match(text)
            if canvas:
                identity = CANVAS_ID.search(canvas[2])
                name = CANVAS_ID.sub('', canvas[2]).strip()
                name_changed = name != canvas_name
                if canvas[1].startswith('Farm Canvas') or name_changed:
                    previous = None
                canvas_name = name
                if canvas[1].startswith('Farm Canvas'):
                    canvas_id = identity[1] if identity else None
                elif name_changed:
                    canvas_id = None
                found_stage = STAGE.search(name)
                current_stage = STAGE_NUMBERS[found_stage[1]] if found_stage else None
                continue
            match = MARKER.match(text)
            if not match:
                continue
            stamp, value = match.groups()
            when, number = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S,%f'), int(value)
            current = (when, number, line)
            if previous is None:
                session += 1
            elif number == previous[1] and when >= previous[0]:
                continue  # Duplicate UI publication: retain original completion time.
            elif number == 0 or number < previous[1] or when <= previous[0]:
                session += 1  # Never include stopped time between separate runs.
            elif number == previous[1] + 1:
                version = display_version(canvas_name)
                if current_stage == stage and (before is None or when <= before) and version_matches(full_version(canvas_name),version_filter,from_version,to_version):
                    row = dict(session=session, stage=current_stage, canvas=canvas_name,
                               canvas_id=canvas_id, canvas_version=version,
                               canvas_fine_version=full_version(canvas_name), round=number,
                               start=previous[0].isoformat(), end=when.isoformat(),
                               seconds=round((when-previous[0]).total_seconds(), 3),
                               start_line=previous[2], end_line=line)
                    key = (canvas_id or canvas_name, version)
                    buckets[key].append(row)
                    available[key] += 1
            else:
                if current_stage == stage and (before is None or when <= before):
                    gaps += 1  # Missing markers cannot prove individual round durations.
            previous = current
    rows = [row for bucket in buckets.values() for row in bucket]
    rows.sort(key=lambda row: (row['end'], row['end_line']))
    return rows, available, gaps


def round_statistics(rows):
    durations = [row['seconds'] for row in rows]
    ordered = sorted(durations)

    def percentile(p):
        if not ordered:
            return None
        index = (len(ordered)-1)*p
        lower, upper = math.floor(index), math.ceil(index)
        return round(ordered[lower]+(ordered[upper]-ordered[lower])*(index-lower), 3)

    counts = Counter(min(math.floor(value/10)*10, 890) for value in durations if 120 <= value <= 900)
    bins = [dict(lower_seconds=lower, upper_seconds=lower+10, upper_inclusive=lower == 890,
                 count=counts[lower], percent=round(counts[lower]*100/len(rows), 2))
            for lower in sorted(counts)]
    return dict(start=rows[0]['start'] if rows else None, end=rows[-1]['end'] if rows else None,
                total_seconds=round(sum(durations), 3), mean_seconds=round(statistics.mean(durations), 3) if rows else None,
                median_seconds=round(statistics.median(durations), 3) if rows else None,
                p90_seconds=percentile(.9), p95_seconds=percentile(.95),
                shortest=min(rows, key=lambda r: r['seconds']) if rows else None,
                longest=max(rows, key=lambda r: r['seconds']) if rows else None,
                below_120_seconds=sum(value < 120 for value in durations),
                above_900_seconds=sum(value > 900 for value in durations), bins=bins)


def select_database(path, count, stage, before=None, version_filter=None, import_logs=(), from_version=None, to_version=None,
                    scope='farm', activity_canvas=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.rwtd.android_core.round_timing_store import RoundTimingStore
    store = RoundTimingStore(path)
    if scope=='activity' and import_logs:
        raise ValueError('活動報表只使用 scope=activity 摘要；舊 Farm rounds Log 不能可靠還原活動輪次')
    for log in import_logs:
        rows, _, _ = select_rounds(Path(log).resolve(strict=True), None, stage)
        for row in rows:
            store.append(dict(row, source='log', runtime_id='log:'+str(row['session']),
                              session='log:'+Path(log).name+':'+str(row['session']),
                              outcome='LEGACY_UNVERIFIED', details={
                                  'log':str(Path(log).resolve()), 'start_line':row['start_line'],
                                  'end_line':row['end_line']}))
    buckets = defaultdict(lambda: deque(maxlen=count))
    available = Counter()
    for row in store.rows(stage, before=before.isoformat() if before else None, version=version_filter,
                          from_version=from_version,to_version=to_version,
                          scope=scope,canvas_id=activity_canvas):
        if row.get('outcome') != 'SUCCESS':
            continue  # Legacy boundaries cannot prove absence of failed exits.
        key = (row['canvas_id'] or row['canvas'], row['canvas_version'])
        row.update(start_line=None, end_line=None)
        buckets[key].append(row)
        available[key] += 1
    rows = [row for bucket in buckets.values() for row in bucket]
    rows.sort(key=lambda row: (row['end'], row['record_id']))
    return rows, available, 0


def activity_catalog(root, store):
    """Discover current and historical activities without hard-coded event IDs."""
    registry = root/'assets/rwtd/scheduler/canvases.json'
    entries = json.loads(registry.read_text(encoding='utf-8-sig')).get('entries',{}) if registry.exists() else {}
    known = {}
    for row in store.rows(None,scope='activity'):
        key = row['canvas_id'] or row['canvas']
        known[key] = row['canvas']
    config = root/'configs/RwtdFarmTask.json'
    if config.exists():
        settings = json.loads(config.read_text(encoding='utf-8-sig'))
        schedules = settings.get('activity_schedules',[])
        if isinstance(schedules,str):
            schedules = json.loads(schedules)
        if not isinstance(schedules,list):
            raise ValueError('活動排程清單格式無效')
        for item in schedules:
            selection = item.get('canvas','')
            key = selection.split(' · ',1)[0].strip()
            if key:
                known.setdefault(key,entries.get(key,{}).get('name',selection))
        legacy = settings.get('activity_canvas','').split(' · ',1)[0].strip()
        if legacy:
            known.setdefault(legacy,entries.get(legacy,{}).get('name',legacy))
    output = []
    for key,item in entries.items():
        if key in known or '活動' in item.get('name',''):
            output.append({'id':key,'name':item['name']})
            known.pop(key,None)
    output.extend({'id':key,'name':name} for key,name in known.items())
    return output


def main(argv=None, *, quiet=False):
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument('--log')
    sources.add_argument('--database')
    parser.add_argument('--import-logs', nargs='+', default=[])
    parser.add_argument('--before', default='', help='Inclusive cutoff in local log time; omitted means latest record')
    parser.add_argument('--rounds', type=int, default=100, help='Latest N complete rounds per Canvas version')
    parser.add_argument('--stage', type=int, choices=range(0, 6), default=0)
    parser.add_argument('--scope', choices=('farm','activity'), default='farm')
    parser.add_argument('--activity-canvas', default='')
    parser.add_argument('--list-activities', action='store_true')
    parser.add_argument('--canvas-version', default='', help='Series 1.6 or exact fine version 1.6.2')
    parser.add_argument('--from-version', default='')
    parser.add_argument('--to-version', default='')
    parser.add_argument('--list-versions', action='store_true')
    parser.add_argument('--output-dir')
    args = parser.parse_args(argv)
    from src.rwtd.android_core.round_timing_store import RoundTimingStore
    root = Path(__file__).resolve().parents[2]
    if args.list_activities:
        if not args.database:
            raise ValueError('Activity catalog requires database path')
        print(json.dumps({'activities':activity_catalog(root,RoundTimingStore(Path(args.database).resolve()))},ensure_ascii=False))
        return
    if args.scope=='farm' and not 1<=args.stage<=5:
        raise ValueError('Farm report requires stage 1..5')
    if args.scope=='activity' and (not args.activity_canvas or not args.database):
        raise ValueError('Activity report requires a Canvas ID and SQLite database')
    if args.scope=='activity' and args.activity_canvas not in {
            item['id'] for item in activity_catalog(root,RoundTimingStore(Path(args.database).resolve()))}:
        raise ValueError('所選活動不在排程、Canvas 活動清單或已存活動摘要中；請重新選擇')
    if args.list_versions:
        from src.rwtd.android_core.round_timing_store import RoundTimingStore
        names = []
        if args.database:
            names.extend(row['canvas'] for row in RoundTimingStore(Path(args.database).resolve()).rows(
                args.stage,scope=args.scope,canvas_id=args.activity_canvas or None))
        registry = root/'assets/rwtd/scheduler/canvases.json'
        if registry.exists():
            names.extend(item.get('name','') for key,item in json.loads(registry.read_text(encoding='utf-8-sig')).get('entries',{}).items()
                         if args.scope=='farm' or key==args.activity_canvas)
        paths = ([Path(args.log)] if args.log else list((root/'logs').glob('ok-script.log*'))) if args.scope=='farm' else []
        for log in paths:
            if not log.is_file():
                continue
            with log.open(encoding='utf-8-sig',errors='replace') as stream:
                for text in stream:
                    match = CANVAS.match(text)
                    if match:
                        names.append(CANVAS_ID.sub('',match[2]).strip())
        versions = {full_version(name) for name in names if full_version(name) and
                    (args.scope=='activity' or ((match:=STAGE.search(name or '')) and STAGE_NUMBERS[match[1]]==args.stage))}
        ordered = sorted(versions,key=version_parts)
        print(json.dumps({'stage':args.stage,'scope':args.scope,'activity_canvas':args.activity_canvas or None,
                         'latest':ordered[-1] if ordered else None,'versions':ordered},ensure_ascii=False))
        return
    if args.rounds < 1:
        raise ValueError('rounds must be positive')
    version_filter = args.canvas_version.strip()
    if version_filter:
        if not re.fullmatch(r'(?i)v?\d+(?:\.\d+)*', version_filter):
            raise ValueError('canvas-version must look like V1.5.4')
        version_filter = version_filter.lstrip('vV')
    lower, upper = args.from_version.strip().lstrip('vV'), args.to_version.strip().lstrip('vV')
    for value in (lower,upper):
        if value:
            version_parts(value)
    if version_filter and (lower or upper):
        raise ValueError('Choose a version or a version range, not both')
    if lower and upper and not version_matches(lower,to_version=upper):
        raise ValueError('Version range starts after its end')
    before = parse_time(args.before)
    if args.import_logs and not args.database:
        raise ValueError('Import requires --database')
    path = Path(args.database).resolve() if args.database else Path(args.log).resolve(strict=True)
    if not args.output_dir:
        raise ValueError('output-dir is required for a report')
    output = Path(args.output_dir)
    targets = [output / name for name in ('rounds.csv', 'report.json', 'report.md')]
    if not output.is_dir() or any(p.exists() for p in targets):
        raise ValueError('Output directory must exist and report files must be new')

    if args.database:
        all_rows, available, gaps = select_database(path, args.rounds, args.stage, before,
                                                  version_filter, args.import_logs, lower, upper,args.scope,args.activity_canvas or None)
    else:
        all_rows, available, gaps = select_rounds(path, args.rounds, args.stage, before, version_filter, lower, upper)
    groups = {}
    for row in all_rows:
        # The Canvas ID keeps different files with the same display version separate.
        key = (row['canvas_id'] or row['canvas'], row['canvas_version'])
        groups.setdefault(key, []).append(row)
    selected = []
    summaries = []
    for (canvas_key, version), rows in groups.items():
        selected.extend(rows)
        summaries.append(dict(canvas_id=rows[-1]['canvas_id'], canvas_version=version,
                              revisions=sorted({row['revision'] for row in rows if row.get('revision')}),
                              canvas_names=sorted({row['canvas'] for row in rows}),
                              available_rounds=available[canvas_key, version], rounds=len(rows),
                              insufficient=available[canvas_key, version] < args.rounds,
                              **round_statistics(rows)))
    summaries.sort(key=lambda item: item['end'], reverse=True)
    selected.sort(key=lambda row: (row['end'], row.get('end_line') or 0))
    label = '第' + '一二三四五'[args.stage-1] + '關' if args.scope=='farm' else next(
        (item['name'] for item in activity_catalog(root,RoundTimingStore(path)) if item['id']==args.activity_canvas),args.activity_canvas)
    scope = ('依 Farm Canvas／info_set Canvas 名稱識別關卡與顯示版本，按 Canvas ID＋顯示版本分組，'
             '每組各取最近指定筆數；未標版本的 Canvas 單獨列出。顯示版本不是不可變的 Canvas revision；'
             '同名版本中途修改內容時，舊日誌無法精確拆分。只計相鄰輪次的完整紀錄；'
             '整組 Farm 一輪包含所有 CHAIN、Solver、回訪及等待。批次停止時間不納入，缺漏輪次不推估。'
             '首輪起點採 Farm rounds 0 的 UI 紀錄時間。')
    if args.database:
        scope = ('來源為永久 SQLite 輪次摘要，依 Canvas ID＋顯示版本分組，每組各取最近指定筆數。'
                 '只納入 outcome=SUCCESS；含失敗退出與 LEGACY_UNVERIFIED 紀錄不納入。'
                 'runtime 紀錄使用單調時鐘，包含該輪 CHAIN、Solver、回訪與等待；插入活動另存 scope=activity，'
                 '不灌入主 FARM 下一輪。停止中的未完成輪次不記錄。revision 與批次保留於資料庫及 CSV。'
                 '匯入舊 Log 的紀錄標為 log／LEGACY_UNVERIFIED，保留舊輪次邊界語意，無法補回已清除的資料。')
    scope += ' 報表以主／次版本（如 1.6）合併，先依細版本或含邊界的版本區間篩選，再取各組最近筆數；完整細版本保留在 Canvas 名稱及 CSV。'
    if args.scope=='activity':
        scope += ' 本報表僅統計所選活動的 scope=activity 摘要；不混入主 FARM，未完成活動不列為完整輪次。'
    report = dict(schema_version=4, source=str(path), label=label, stage=args.stage,
                  execution_scope=args.scope,activity_canvas=args.activity_canvas or None,
                  requested_rounds_per_version=args.rounds, canvas_version_filter=version_filter or None,
                  from_version=lower or None,to_version=upper or None,
                  before=before.isoformat() if before else None,
                  available_rounds=sum(item['available_rounds'] for item in summaries),
                  rounds=len(selected), missing_marker_gaps=gaps, groups=summaries, scope=scope)
    fields = ['session', 'stage', 'canvas', 'canvas_id', 'canvas_version', 'canvas_fine_version', 'round',
              'start', 'end', 'seconds', 'start_line', 'end_line', 'revision', 'source', 'scope', 'outcome']
    with targets[0].open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(selected)
    with targets[1].open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2))
    lines = [f'# {label} 輪次耗時（依 Canvas 版本）', '', scope, '',
             f'每個版本指定最近 {args.rounds} 筆；共找到 {report["available_rounds"]} 筆完整紀錄；已統計 {len(selected)} 筆。', '']
    if version_filter:
        lines.extend([f'版本篩選：{version_filter}', ''])
    if lower or upper:
        lines.extend([f'版本區間：{lower or "不限"} ～ {upper or "不限"}（含邊界系列）',''])
    if gaps:
        lines.extend([f'有 {gaps} 處輪次編號缺漏；未將跨越缺漏的時間算成單輪。', ''])
    if not summaries:
        lines.append('沒有符合條件的完整輪次。')
    for item in summaries:
        version = item['canvas_version'] or '未標版本'
        lines.extend([f'## {version}｜{", ".join(item["canvas_names"])}', '',
                      f'Canvas ID：{item["canvas_id"] or "日誌未記錄"}',
                      f'找到 {item["available_rounds"]} 筆；已統計 {item["rounds"]} 筆。',
                      f'時間：{item["start"]} ～ {item["end"]}（電腦本機日誌時間）。'])
        if item['insufficient']:
            lines.append('可用紀錄不足指定筆數，已統計此版本所有找到的完整紀錄。')
        lines.extend([f'平均：{item["mean_seconds"]} 秒；中位數：{item["median_seconds"]} 秒；'
                      f'P90：{item["p90_seconds"]} 秒；P95：{item["p95_seconds"]} 秒。',
                      f'最短：批次 {item["shortest"]["session"]} 第 {item["shortest"]["round"]} 輪，'
                      f'{item["shortest"]["seconds"]} 秒；最長：批次 {item["longest"]["session"]} '
                      f'第 {item["longest"]["round"]} 輪，{item["longest"]["seconds"]} 秒。',
                      f'表格範圍外：少於 120 秒 {item["below_120_seconds"]} 筆；'
                      f'超過 900 秒 {item["above_900_seconds"]} 筆。比例以此版本 {item["rounds"]} 筆為分母。',
                      '', '| 秒數區間 | 輪數 | 比例 |', '|---|---:|---:|'])
        lines.extend(f'| {bin_["lower_seconds"]}–{"≤" if bin_["upper_inclusive"] else "<"}'
                     f'{bin_["upper_seconds"]} 秒 | {bin_["count"]} | {bin_["percent"]}% |'
                     for bin_ in item['bins'])
        lines.append('')
    with targets[2].open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines)+'\n')
    if not quiet:
        print('\n'.join(lines))
        print(f'FARM_ROUND_REPORT_COMPLETE rounds={len(selected)}')


if __name__ == '__main__':
    main()
