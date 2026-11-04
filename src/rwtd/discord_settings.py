"""Native OK software settings; token is stored with Windows user DPAPI."""
import os
import re
from pathlib import Path
from dataclasses import dataclass,field

NAME='Discord 遠端操控'
ENABLED='啟用 Discord Bot 操控'
GUILD='伺服器 ID'
USERS='允許使用者 ID'
CHANNELS='允許頻道 ID'
TOKEN='Bot Token'
ROOT=Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BotSettings:
    guild: int
    users: frozenset
    channels: frozenset
    token: str = field(repr=False)

    @classmethod
    def load(cls,value,token):
        def snowflake(item):
            text=str(item)
            if not text.isdigit() or not 16<=len(text)<=22 or int(text)==0:raise ValueError('Discord ID 格式錯誤')
            return int(text)
        if set(value)-{'enabled','guild_id','user_ids','channel_ids'}:raise ValueError('Bot 設定有未知欄位')
        users=value.get('user_ids');channels=value.get('channel_ids')
        if not isinstance(users,list) or not users or not isinstance(channels,list) or not channels:
            raise ValueError('必須設定允許的使用者及頻道 ID')
        token=(token or '').strip()
        if not token:raise ValueError('請先在軟體設定儲存 Bot Token')
        return cls(snowflake(value.get('guild_id')),frozenset(map(snowflake,users)),
                   frozenset(map(snowflake,channels)),token)

    def permits(self,guild,user,channel):
        return guild==self.guild and user in self.users and channel in self.channels


def token_path():return ROOT/'configs/rwtd-discord-token.dpapi'


def save_token(token):
    import win32crypt
    if not isinstance(token,str) or not 30<=len(token)<=250 or any(c.isspace() for c in token):
        raise ValueError('Bot Token 格式錯誤')
    encrypted=win32crypt.CryptProtectData(token.encode('utf-8'),'ok-RwtD Discord Bot',None,None,None,1)
    path=token_path();path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    try:
        temp.write_bytes(encrypted)
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


def read_token():
    if os.environ.get('RWTD_DISCORD_BOT_TOKEN'):return os.environ['RWTD_DISCORD_BOT_TOKEN'].strip()
    if not token_path().is_file():return ''
    import win32crypt
    return win32crypt.CryptUnprotectData(token_path().read_bytes(),None,None,None,1)[1].decode('utf-8')


def edit_token():
    from PySide6.QtWidgets import QApplication,QInputDialog,QLineEdit,QMessageBox
    parent=QApplication.activeWindow()
    value,ok=QInputDialog.getText(parent,'Discord Bot Token','輸入 Token（留空取消，不顯示既有 Token）：',QLineEdit.Password)
    if not ok or not value.strip():return
    try:
        save_token(value.strip())
        QMessageBox.information(parent,'Discord Bot','Token 已加密儲存於本機。設定完成後請重啟 OK。')
    except Exception:
        QMessageBox.warning(parent,'Discord Bot','Token 未儲存；請確認格式與 Windows 帳號權限。')


def remove_token():
    from PySide6.QtWidgets import QApplication,QMessageBox
    token_path().unlink(missing_ok=True)
    QMessageBox.information(QApplication.activeWindow(),'Discord Bot','本機 Token 已移除；重啟 OK 後套用。')


def show_status():
    from PySide6.QtWidgets import QApplication,QMessageBox
    from .discord_control import current
    service=current()
    message=service.bot.status() if service and hasattr(service,'bot') else 'Bot 未啟用或設定／依賴尚未就緒；完成設定後重啟 OK。'
    QMessageBox.information(QApplication.activeWindow(),'Discord Bot 連線狀態',message)


def option():
    from ok import ConfigOption
    return ConfigOption(NAME,{ENABLED:False,GUILD:'',USERS:'',CHANNELS:'',TOKEN:''},
        description='DC 指令控制 OK／FARM。只接受指定使用者與頻道；所有設定重啟 OK 後生效。OK 關閉時無法收指令。',
        config_description={ENABLED:'預設關閉；原本 Discord Webhook 通知設定不受影響。',
            GUILD:'Discord 伺服器 ID。',USERS:'可填多個 ID，以逗號或空白分隔；不可留空。',
            CHANNELS:'可填多個控制頻道 ID，以逗號或空白分隔；不可留空。',
            TOKEN:'使用遮罩輸入，透過 Windows DPAPI 加密儲存；此欄不保存 Token 明文。'},
        config_type={TOKEN:{'type':'button','buttons':[
            {'text':'設定／更新 Token','callback':edit_token},
            {'text':'移除本機 Token','callback':remove_token},
            {'text':'連線狀態','callback':show_status}]}})


def settings(config):
    return {'enabled':config.get(ENABLED,False),'guild_id':config.get(GUILD,''),
            'user_ids':[s for s in re.split(r'[,，\s]+',config.get(USERS,'')) if s],
            'channel_ids':[s for s in re.split(r'[,，\s]+',config.get(CHANNELS,'')) if s]}
