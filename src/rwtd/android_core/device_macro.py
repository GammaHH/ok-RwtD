"""One official ADB shell stream per known-coordinate segment.

Progress is one-way telemetry, never a PC acknowledgement gating the next input.
Cancellation requests stop at touch-up; an uncertain stream is never replayed.
"""
import shlex
import socket
import time
import uuid
import json
import math
from datetime import datetime, timezone


STEP_INTERVAL_KEY = 'Operation Step Interval (s)'


def step_interval(task):
    """Minimum pause after a completed gesture, shared by both input paths."""
    config = getattr(task, 'config', {}) or {}
    value = config.get(STEP_INTERVAL_KEY, 0)
    if isinstance(value, bool):
        raise ValueError(f'{STEP_INTERVAL_KEY} must be a number from 0 to 30')
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{STEP_INTERVAL_KEY} must be a number from 0 to 30') from exc
    if not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise ValueError(f'{STEP_INTERVAL_KEY} must be a number from 0 to 30')
    return seconds

def log_marker(task, label, fields):
    """Explicit SEND/DONE are independent of DEBUG logs of command text."""
    from .watchdog import mark
    mark(task,label,**fields)
    message='['+label+'] '+json.dumps(fields,ensure_ascii=False,sort_keys=True)
    for method,args in (('log_info',(message,)),('dashboard_event',('MACRO_TIMING',message))):
        callback=getattr(task,method,None)
        if callable(callback):
            try:
                callback(*args)
            except Exception:
                pass  # Diagnostic failure must not interrupt an in-flight gesture.


def eligible(step):
    return (step.get('name') in {'TapTest', 'LongPressTest', 'SwipeCalibration', 'WaitTest'}
            and not any(step.get(k) for k in ('features', 'feature', 'color_mode', 'ocr_target', 'target_conditions', 'mid_x', 'input_conditions',
                                              'precondition_feature', 'verify_feature', 'previous_feature'))
            and 'mid_x' not in step and 'mid_y' not in step
            and step.get('verification_policy', 'manual') in {'manual', 'none'}
            and (step.get('name') == 'WaitTest' or step.get('allow_unconfirmed_target') is True))


def compile_script(steps, token, interval_seconds=0):
    """Only validated numeric coordinates/delays enter shell commands."""
    marker = '/data/local/tmp/rwtd_macro_' + token + '.stop'
    prefix = 'RWTD_' + token
    lines = [f"stopfile={shlex.quote(marker)}", "trap 'rm -f \"$stopfile\"' EXIT",
             f"check() {{ [ ! -e \"$stopfile\" ] || {{ echo {prefix}:STOP; exit 0; }}; }}",
             # Only the wait timer is cancellable; never kill an input gesture.
             'delay() { check; [ "$1" != "0" ] || return 0; '
             'sleep "$1" & rwtd_timer=$!; '
             '(while [ ! -e "$stopfile" ]; do sleep 0.05; done; '
             'kill "$rwtd_timer" 2>/dev/null) & rwtd_watcher=$!; '
             'wait "$rwtd_timer" 2>/dev/null; '
             'kill "$rwtd_watcher" 2>/dev/null; wait "$rwtd_watcher" 2>/dev/null; check; }']
    seconds = 0
    for index, step in enumerate(steps):
        lines += ['check', f'echo {prefix}:BEGIN:{index}']
        delay = step.get('delay_seconds', 0)
        lines.append('delay ' + (format(delay, '.9f').rstrip('0').rstrip('.') or '0'))
        seconds += delay
        for repetition in range(step.get('repeat_count', 1)):
            if step['name'] != 'WaitTest':
                x, y = step['x'], step['y']
                end_x, end_y = ((step['end_x'], step['end_y'])
                                if step['name'] == 'SwipeCalibration' else (x, y))
                duration = step.get('duration_ms', 50)
                lines += ['check', f'echo {prefix}:INPUT:{index}:{repetition}',
                          f'input swipe {x} {y} {end_x} {end_y} {duration}',
                          # Deliberately ignore per-input stdout and exit status.
                          f'echo {prefix}:UP:{index}:{repetition}']
                seconds += duration / 1000
            wait = step.get('post_wait_seconds', 0)
            if step['name'] != 'WaitTest':
                wait = max(wait, interval_seconds)
            lines.append('delay ' + (format(wait, '.9f').rstrip('0').rstrip('.') or '0'))
            seconds += wait
        lines += [f'echo {prefix}:END:{index}']
    lines += [f'echo {prefix}:DONE']
    return 'sh -c ' + shlex.quote('\n'.join(lines)), marker, prefix, seconds


def execute(task, steps, cancelled, *, context=None):
    """Return completed step/gesture boundaries, without claiming game success."""
    token = uuid.uuid4().hex
    packing_started=time.perf_counter()
    physical_steps = [task.screen_step(step) for step in steps]
    command, marker, prefix, duration = compile_script(physical_steps, token, step_interval(task))
    packing_seconds=time.perf_counter()-packing_started
    started = None
    result = {'started': set(), 'attempted': {}, 'returned': {}, 'completed': set(),
              'done': False, 'cancelled': False, 'stop_confirmed': False,
              'failure': None, 'transport': 'official_adb_macro'}
    result['segment_id']=token
    details=dict(context or {},segment=token,packing_seconds=round(packing_seconds,6),
                 theoretical_seconds=round(duration,6))
    result['timing_seconds']={'configured_total':round(duration,6),'actual_segment':None,
                              'extra_segment':None,'comparison_valid':False}
    if cancelled():
        result['cancelled'] = True
        log_marker(task,'RWTD_MACRO_SKIPPED',dict(details,utc=datetime.now(timezone.utc).isoformat(),
                   reason='Cancelled before submission; no Android input sent'))
        return result
    stream = None
    signalled = False
    buffer = b''
    try:
        # Measure from submission, excluding validation and waiting for the owner.
        started = time.monotonic()
        result['sent_utc']=datetime.now(timezone.utc).isoformat()
        log_marker(task,'RWTD_MACRO_SEND',dict(details,utc=result['sent_utc']))
        deadline = started + duration + max(30, sum(s.get('repeat_count', 1) for s in steps) * 2)
        stream = task.adb_shell(command, stream=True)
        stream.conn.settimeout(.1)
        while True:
            if result['done'] or result['stop_confirmed']:
                break
            if not signalled and (cancelled() or time.monotonic() > deadline):
                result['cancelled'] = True
                signalled = True
                result['cancel_requested_utc']=datetime.now(timezone.utc).isoformat()
                log_marker(task,'RWTD_MACRO_CANCEL_REQUEST',dict(details,utc=result['cancel_requested_utc']))
                # The selected official task API is also used for cancellation.
                # This does not inject input or kill a finger-down gesture.
                task.adb_shell('touch ' + shlex.quote(marker), timeout=5)
                deadline = time.monotonic() + 25
            if signalled and time.monotonic() > deadline:
                raise TimeoutError('Device macro cancellation completion unavailable')
            try:
                data = stream.conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            buffer += data
            while b'\n' in buffer:
                raw, buffer = buffer.split(b'\n', 1)
                line = raw.decode('utf-8', errors='replace').strip()
                if not line.startswith(prefix + ':'):
                    continue
                fields = line[len(prefix) + 1:].split(':')
                kind = fields[0]
                if kind == 'DONE':
                    result['done'] = True
                    result['received_utc']=datetime.now(timezone.utc).isoformat()
                elif kind == 'STOP':
                    result['cancelled'] = True
                    result['stop_confirmed'] = True
                    result['received_utc']=datetime.now(timezone.utc).isoformat()
                elif kind in {'BEGIN', 'END', 'INPUT', 'UP'}:
                    index = int(fields[1])
                    if not 0 <= index < len(steps):
                        raise ValueError('Invalid device macro boundary')
                    if kind == 'BEGIN':
                        result['started'].add(index)
                    elif kind == 'END':
                        result['completed'].add(index)
                    else:
                        result['attempted' if kind == 'INPUT' else 'returned'][index] = int(fields[2]) + 1
        if not result['done'] and not result['stop_confirmed']:
            result['failure'] = 'Device macro stream ended without completion; delivery uncertain; no replay'
    except Exception as exc:
        result['failure'] = str(exc)
        # A disconnected stream does not establish that Android stopped.
        controller = getattr(task, 'controller', None)
        if controller is not None:
            controller.device_stop.set()
    finally:
        # Completion receipt is the endpoint; connection cleanup is excluded.
        actual = time.monotonic()-started if started is not None else None
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                result['failure'] = str(exc)
    if result['failure']:
        controller = getattr(task, 'controller', None)
        if controller is not None:
            controller.device_stop.set()
    result['stop_policy'] = 'finish_current_gesture'
    result['timing_seconds'].update(actual_segment=round(actual,6) if actual is not None else None,
        extra_segment=round(actual-duration,6) if result['done'] and not result['failure'] else None,
        comparison_valid=bool(result['done'] and not result['failure']))
    label=('RWTD_MACRO_UNKNOWN' if result['failure'] else
           'RWTD_MACRO_DONE' if result['done'] else 'RWTD_MACRO_STOP')
    log_marker(task,label,dict(details,utc=result.get('received_utc') or datetime.now(timezone.utc).isoformat(),
        sent_utc=result.get('sent_utc'),actual_seconds=result['timing_seconds']['actual_segment'],
        cancel_requested_utc=result.get('cancel_requested_utc'),
        extra_seconds=result['timing_seconds']['extra_segment'],reason=result['failure'],
        packing_savings_upper_bound_percent=(round(100*packing_seconds/(packing_seconds+actual),4)
            if result['timing_seconds']['comparison_valid'] and actual is not None and packing_seconds+actual>0 else None)))
    return result
