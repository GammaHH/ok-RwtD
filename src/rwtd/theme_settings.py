"""Persistent, validated theme colors and the device-independent settings TASK."""
import copy
import hashlib
import json
import queue
import re
import threading
from pathlib import Path

from .android_core.module_store import atomic_json, safe_path
from .android_core.user_data import definition_root


LABELS = {
    'window':'視窗底色','sidebar':'側欄／Canvas 底色','surface':'面板／節點底色',
    'surface_alt':'次要面板底色','control':'控制項底色','control_hover':'控制項滑過底色',
    'control_pressed':'控制項按下底色','border':'一般邊框','border_strong':'強調邊框','separator':'分隔線',
    'text':'主要文字','text_secondary':'次要文字','text_tertiary':'輔助文字','text_disabled':'停用文字',
    'accent':'紫色主色／圖示','highlight':'強調文字／鍵盤焦點','accent_text':'連結文字',
    'selected_surface':'選取／Guard 底色','selection_fill':'框選填色（可含透明度）',
    'graph_highlight':'聚焦節點／執行路徑','cyan':'標題一','mint':'連線／標題二',
    'green':'完成／標題三','orange':'提醒／標題五','pink':'標題六','danger':'失敗／刪除','warning':'警示',
}
_store_lock=threading.RLock()


class ThemeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = safe_path(definition_root(self.root),'assets/rwtd/scheduler/theme.json')
        self.lock = _store_lock

    def snapshot(self):
        from .theme import PALETTE
        with self.lock:
            palette = dict(PALETTE)
            if self.path.exists():
                value=json.loads(self.path.read_text(encoding='utf-8-sig'))
                if not isinstance(value,dict) or set(value)!={'schema_version','palette'} or value['schema_version']!=1:
                    raise ValueError('配色設定檔格式無效')
                palette.update(self.validate(value['palette']))
            revision=hashlib.sha256(json.dumps(palette,sort_keys=True).encode('utf-8')).hexdigest()
            return dict(palette=palette,defaults=dict(PALETTE),revision=revision,labels=dict(LABELS))

    @staticmethod
    def validate(palette):
        from .theme import PALETTE
        if not isinstance(palette,dict) or set(palette)-set(PALETTE):
            raise ValueError('配色欄位無效')
        if any(not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?',value) for value in palette.values()):
            raise ValueError('色碼需為 #RRGGBB 或 #RRGGBBAA')
        if any(len(value)==9 and key!='selection_fill' for key,value in palette.items()):
            raise ValueError('僅框選填色可設定透明度；其他欄位請使用 #RRGGBB')
        return {key:value.upper() for key,value in palette.items()}

    def save(self, palette, expected_revision):
        with self.lock:
            current=self.snapshot()
            if current['revision']!=expected_revision:raise ValueError('配色已被其他視窗修改，請重新載入')
            validated=self.validate(palette)
            if set(validated)!=set(current['palette']):raise ValueError('請送出完整配色設定')
            atomic_json(self.path,dict(schema_version=1,palette=validated))
            return self.snapshot()


class ThemeSettingsEditor:
    def __init__(self, root, notify_native=None):
        self.root=Path(root)
        self.store=ThemeStore(root)
        self.jobs=queue.Queue(maxsize=4)
        self.closing=threading.Event()
        self.lock=threading.RLock()
        self.notify_native=notify_native
        self.state=dict(settings=self.store.snapshot(),results={})

    def _log(self,*args):
        pass

    def image(self,which):
        return None

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.state)

    def enqueue(self,request_id,command,payload):
        if command not in {'save','reload','close'} or not isinstance(payload,dict) or not isinstance(request_id,str) or not 1<=len(request_id)<=100:
            raise ValueError('無效的配色設定指令')
        if set(payload)!=({'palette','expected_revision'} if command=='save' else set()):
            raise ValueError('配色設定參數不完整或包含未知欄位')
        with self.lock:
            if request_id in self.state['results']:return dict(self.state['results'][request_id],request_id=request_id)
            try:self.jobs.put_nowait((request_id,command,copy.deepcopy(payload)))
            except queue.Full:raise ValueError('配色設定請求已滿，請稍候') from None
            self.state['results'][request_id]={'status':'QUEUED'}
        return dict(request_id=request_id,status='QUEUED')

    def process_next(self):
        try:request_id,command,payload=self.jobs.get_nowait()
        except queue.Empty:return
        try:
            if command=='close':
                self.closing.set()
                result={'status':'SUCCESS'}
            else:
                saved=self.store.save(payload.get('palette'),payload.get('expected_revision')) if command=='save' else self.store.snapshot()
                queued=False
                if command=='save':
                    notify=self.notify_native
                    if notify is None:
                        from .theme import request_native_palette
                        notify=request_native_palette
                    try:queued=bool(notify(saved['palette']))
                    except Exception:queued=False
                with self.lock:self.state['settings']=saved
                result=dict(status='SUCCESS',native_update_queued=queued)
        except Exception as exc:result=dict(status='FAILED',reason=str(exc)[:300])
        with self.lock:
            self.state['results'][request_id]=result
            while len(self.state['results'])>50:self.state['results'].pop(next(iter(self.state['results'])))
