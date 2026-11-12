"""Route native feature entry points through the shared Dashboard workspace."""


def route_to_workspace(task, view):
    workspace = getattr(task, '_native_workspace', None)
    if workspace is not None and not workspace.closed:
        return False
    executor = task.executor
    dashboard = next((item for item in executor.onetime_tasks
                      if type(item).__name__ == 'RwtdControlDashboardTask'), None)
    if dashboard is None:
        raise ValueError('RwtD 控制面板任務尚未註冊，無法建立共用工作區')
    if executor.current_task is not task:
        raise ValueError('目前任務尚未取得原生操作權')
    if any(item is not task and (item.enabled or item.running) for item in executor.onetime_tasks):
        raise ValueError('另一個任務仍在執行或排隊，請先停止該任務')
    dashboard._workspace_entry = view
    dashboard.enable()
    return True
