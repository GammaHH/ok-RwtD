"""Optional Discord slash-command transport; never captures or operates a device."""
import asyncio
import io
import json
import logging
import threading

import discord
from discord import app_commands


def start_bot(service,settings):
    for name in ('discord','discord.http','discord.gateway'):
        logging.getLogger(name).setLevel(logging.WARNING)
    class Client(discord.Client):
        def __init__(self):
            super().__init__(intents=discord.Intents.none())
            self.tree=app_commands.CommandTree(self)
            self.guild=discord.Object(id=settings.guild)
        async def setup_hook(self):
            self.tree.copy_global_to(guild=self.guild)
            await self.tree.sync(guild=self.guild)

    client=Client()

    def permitted(interaction):
        return settings.permits(interaction.guild_id,interaction.user.id,interaction.channel_id)

    async def execute(interaction,command,payload=None):
        if not permitted(interaction):
            await interaction.response.send_message('此使用者／伺服器／頻道未獲授權。',ephemeral=True)
            return
        # Authorized command results (including screenshots) are channel-visible.
        await interaction.response.defer(ephemeral=False,thinking=True)
        try:
            future=service.mailbox.submit(str(interaction.id),command,payload)
            logging.getLogger(__name__).info('RWTD_DC_ACCEPT command=%s id=%s user=%s guild=%s channel=%s',
                command,interaction.id,interaction.user.id,interaction.guild_id,interaction.channel_id)
            await interaction.edit_original_response(content='指令已接受並排入 OK 佇列，尚未完成。')
            result=await asyncio.wait_for(asyncio.wrap_future(future),120)
            message=result['status']+' · '+result['message']
            files=[]
            if result.get('data') is not None:
                data=result['data']
                if command=='trigger_status':
                    data=[{k:t.get(k) for k in ('id','name','status','reason','current_node','last_error')}
                          for t in data.get('tasks',[])]
                detail=json.dumps(data,ensure_ascii=False,default=str,indent=2)
                if len(detail)>1400:
                    encoded=detail.encode('utf-8')
                    if len(encoded)<=512*1024:
                        files.append(discord.File(io.BytesIO(encoded),filename='details.json'))
                        message+='\n完整資料見 details.json。'
                    else:message+='\n資料過大，請由 OK 查看完整紀錄。'
                else:message+='\n'+detail
            if result.get('png'):
                files.append(discord.File(io.BytesIO(result['png']),filename='phone.png'))
            await interaction.edit_original_response(content=message[:1900],attachments=files,
                                                       allowed_mentions=discord.AllowedMentions.none())
        except (asyncio.TimeoutError,asyncio.CancelledError):
            await interaction.edit_original_response(content='等待結果逾時；請用 /status 查詢，不會自動重送。')
        except Exception as exc:
            # Never echo tokens, transport exception strings, or request headers.
            await interaction.edit_original_response(content='指令失敗：'+type(exc).__name__+'；請查看 OK 狀態。')

    @client.tree.command(name='status',description='查詢 OK／FARM 目前狀態')
    async def status(interaction:discord.Interaction):await execute(interaction,'status')

    @client.tree.command(name='screenshot',description='由 OK 取得手機最新截圖')
    async def screenshot(interaction:discord.Interaction):await execute(interaction,'screenshot')

    farm=app_commands.Group(name='farm',description='控制現有 OK FARM 任務')
    @farm.command(name='start',description='啟動 FARM，可選指定已儲存 Canvas')
    async def farm_start(interaction:discord.Interaction,canvas:str=''):
        await execute(interaction,'farm_start',{'canvas':canvas} if canvas else {})

    @farm_start.autocomplete('canvas')
    async def canvases(interaction:discord.Interaction,current:str):
        if not permitted(interaction):return []
        return [app_commands.Choice(name=row['name'][:100],value=row['id'])
                for row in service.catalog if current.casefold() in row['name'].casefold()][:25]

    @farm.command(name='pause',description='在操作交接點暫停 FARM')
    async def pause(interaction:discord.Interaction):await execute(interaction,'farm_pause')
    @farm.command(name='resume',description='恢復已暫停的 FARM')
    async def resume(interaction:discord.Interaction):await execute(interaction,'farm_resume')
    @farm.command(name='stop',description='停止 FARM，取消尚未執行的操作')
    async def stop(interaction:discord.Interaction):await execute(interaction,'farm_stop')
    @farm.command(name='stop-after-round',description='本次主 FARM 輪次完成後停止')
    async def stop_after(interaction:discord.Interaction):await execute(interaction,'farm_stop_after_round')
    client.tree.add_command(farm)

    activity=app_commands.Group(name='activity',description='查詢或排入已設定的活動')
    @activity.command(name='list',description='顯示活動設定、待辦與到期狀態')
    async def activity_list(interaction:discord.Interaction):await execute(interaction,'activity_list')
    @activity.command(name='queue',description='排入活動，等主 FARM 安全輪次結束後執行')
    async def activity_queue(interaction:discord.Interaction,activity:str,runs:app_commands.Range[int,1,100]=1):
        await execute(interaction,'activity_queue',{'activity':activity,'runs':runs})
    client.tree.add_command(activity)
    trigger=app_commands.Group(name='trigger',description='查詢監看與 Solver')
    @trigger.command(name='status',description='顯示監看狀態及最後錯誤')
    async def trigger_status(interaction:discord.Interaction):await execute(interaction,'trigger_status')
    client.tree.add_command(trigger)
    diagnosis=app_commands.Group(name='diagnosis',description='查詢失敗證據')
    @diagnosis.command(name='latest',description='最近失敗摘要與歷史截圖')
    async def diagnosis_latest(interaction:discord.Interaction):await execute(interaction,'diagnosis_latest')
    client.tree.add_command(diagnosis)

    # Explicit connection lifecycle; discord.py handles Gateway reconnects.
    loop=asyncio.new_event_loop()
    class Handle:
        def __init__(self):
            self.client=client
            self.error=None
            self.stopping=threading.Event()
        def status(self):
            return '已連線' if client.is_ready() else ('連線失敗：'+self.error if self.error else '尚未連線／連線中')
        def close(self):
            self.stopping.set()
            if loop.is_running():asyncio.run_coroutine_threadsafe(client.close(),loop)
    handle=Handle()
    def run():
        asyncio.set_event_loop(loop)
        try:
            if not handle.stopping.is_set():loop.run_until_complete(client.start(settings.token))
        except Exception as exc:
            handle.error=type(exc).__name__
            logging.getLogger(__name__).error('Discord Bot disconnected: %s',type(exc).__name__)
        finally:
            loop.run_until_complete(client.close())
            loop.close()
    threading.Thread(target=run,name='RwtD-DiscordBot',daemon=True).start()
    return handle
