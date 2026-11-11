"""App-local OK lifecycle adapter for the device-independent Dashboard editor."""
from functools import wraps


def is_offline_editor(task):
    # Farm inherits the Dashboard implementation but still requires a device.
    return (task is not None and type(task).__name__ in {'RwtdControlDashboardTask', 'RwtdResolutionCalibrationTask', 'RwtdRoundStatisticsTask'}
            and getattr(task, '_rwtd_offline_editor', False) is True)


def install(app, executor, notify, logger):
    controller = app.start_controller
    if getattr(controller, '_rwtd_editor_lifecycle', False):
        return
    original_start = controller._do_start
    original_request = getattr(controller, 'start', None)
    original_frame = executor.next_frame

    @wraps(original_start)
    def start(task=None, exit_after=False):
        selected = executor.onetime_tasks[task] if type(task) is int else task
        if not is_offline_editor(selected):
            return original_start(task, exit_after)
        # Use OK's ordinary enable/queue/executor lifecycle, without refreshing,
        # connecting, launching or validating the selected device/window.
        notify(False, None, controller.start_timeout)
        try:
            if exit_after:
                selected.exit_after_task = True
            if selected.enabled and selected.paused:
                selected.unpause()
            else:
                controller._mark_task_enabled(selected)
            executor.start()
            logger.info('RWTD_OFFLINE_EDITOR_START native task queued; no device preflight')
            notify(True, None, 0)
            return True
        except Exception as exc:
            logger.warning('RWTD_OFFLINE_EDITOR_START_FAILED ' + type(exc).__name__)
            notify(True, 'Dashboard 啟動失敗：' + type(exc).__name__, 0)
            return False

    @wraps(original_frame)
    def frame(*args, **kwargs):
        if is_offline_editor(executor.current_task):
            # TaskExecutor normally captures before run(); its unavailable-
            # device sleep can block forever. The editor needs no such frame.
            # Explicit preview/OCR/input retains its own official ADB checks.
            executor.check_enabled(check_pause=False)
            return None
        return original_frame(*args, **kwargs)

    controller._do_start = start
    if original_request is not None:
        @wraps(original_request)
        def request(task=None, exit_after=False):
            selected = executor.onetime_tasks[task] if type(task) is int else task
            if is_offline_editor(selected):
                # This path only queues a task and updates native state. Do not
                # wait behind a previous device preflight on the start Handler.
                logger.info('RWTD_OFFLINE_EDITOR_REQUEST ' + type(selected).__name__)
                return start(task, exit_after)
            return original_request(task, exit_after)
        controller.start = request
    executor.next_frame = frame
    controller._rwtd_editor_lifecycle = True
