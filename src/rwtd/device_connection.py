"""App-local ADB refresh repair; never selects or launches another device."""
import ipaddress
import re
from functools import wraps


def network_address(address):
    if not isinstance(address, str) or not address or address != address.strip():
        return False
    try:
        # A bare IPv4 address uses adb's default TCP port. Require brackets
        # and an explicit port for IPv6 rather than guessing its boundary.
        return ipaddress.ip_address(address).version == 4
    except ValueError:
        pass
    if re.fullmatch(r'[A-Za-z0-9_.-]+\._adb-tls-connect\._tcp\.?', address):
        return True
    match = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.-]+):(\d{1,5})', address)
    if not match or not 1 <= int(match[2]) <= 65535:
        return False
    if match[1].startswith('['):
        try:
            return ipaddress.ip_address(match[1][1:-1]).version == 6
        except ValueError:
            return False
    return True


def install(manager, notify, logger):
    if getattr(manager, '_rwtd_connection_repair', False):
        return
    original_refresh = manager.refresh_phones
    original_shell = manager.shell_device

    def publish():
        try:
            notify(True)
        except Exception as exc:
            # Reporting a disconnection must not hide the original ADB error.
            logger.warning(f'RWTD_ADB_STATE_NOTIFY_FAILED error={exc}')

    def invalidate(address, state='missing'):
        for record in list(manager.device_dict.values()):
            if record.get('device') == 'adb' and record.get('address') == address:
                record['connected'] = False
                record['adb_state'] = state
        if getattr(getattr(manager, '_device', None), 'serial', None) == address:
            manager._device = None
        getattr(manager, 'resolution_dict', {}).pop(address, None)

    def lookup(address):
        rows = manager.adb.list()
        row = next((r for r in rows if r.serial == address), None)
        if row is not None and row.state == 'device':
            return manager.adb.device(serial=address), 'device'
        return None, row.state if row is not None else 'missing'

    def connect(address, try_connect=True):
        if manager.exit_event.is_set():
            return None
        try:
            device, state = lookup(address)
            # A USB serial is an identifier, not a DNS name or TCP endpoint.
            if device is None and try_connect and state in {'missing', 'offline'} and network_address(address):
                if state == 'offline':
                    manager.adb.disconnect(address)
                manager.adb.connect(address, timeout=5)
                device, state = lookup(address)
            if device is None:
                invalidate(address, state)
                logger.info(f'RWTD_ADB_UNAVAILABLE address={address} state={state}')
            else:
                for record in list(manager.device_dict.values()):
                    if record.get('device') == 'adb' and record.get('address') == address:
                        record.update(connected=True, adb_state='device')
            return device
        except Exception as exc:
            invalidate(address, 'unavailable')
            logger.warning(f'RWTD_ADB_CONNECT_FAILED address={address} error={exc}')
            return None

    @wraps(original_refresh)
    def refresh(current=False):
        if manager.adb_capture_config is None:
            return
        # Clear stale connected flags even when enumeration fails. Keep the
        # preference and offline entry so a refresh cannot switch phones.
        for record in list(manager.device_dict.values()):
            if record.get('device') == 'adb' and not record.get('emulator'):
                record.update(connected=False, adb_state='unavailable')
        try:
            rows = manager.adb.list()
            states = {r.serial: r.state for r in rows}
            for record in list(manager.device_dict.values()):
                if record.get('device') == 'adb' and not record.get('emulator'):
                    address = record.get('address')
                    state = states.get(address, 'missing')
                    record.update(connected=state == 'device', adb_state=state)
                    if state != 'device':
                        invalidate(address, state)
            preferred = manager.get_preferred_device()
            for row in rows:
                if manager.exit_event.is_set():
                    return
                if row.state != 'device':
                    continue
                try:
                    device = manager.adb.device(serial=row.serial)
                    imei = manager.adb_get_imei(device)
                    if not imei or current and preferred is not None and preferred['imei'] != imei:
                        continue
                    existing = next((r for r in list(manager.device_dict.values())
                                     if r.get('adb_imei') == imei), None)
                    if existing and existing.get('emulator'):
                        continue
                    if (existing and existing.get('address') != row.serial
                            and states.get(existing.get('address')) == 'device'):
                        # The same phone may expose USB and wireless transports.
                        # Retain the saved live transport, not enumeration order.
                        continue
                    width, height = manager.get_resolution(device)
                    record = dict(existing or {})
                    record.update(address=row.serial, device='adb', connected=True,
                                  adb_state='device', imei=imei, adb_imei=imei, player_id=-1,
                                  resolution=f'{width}x{height}')
                    if not record.get('nick'):
                        record['nick'] = device.prop.model or imei
                    manager.device_dict[imei] = record
                    cached = getattr(manager, '_device', None)
                    if cached is not None and cached.serial != row.serial and preferred is not None and preferred['imei'] == imei:
                        manager._device = None
                except Exception as exc:
                    invalidate(row.serial, 'unavailable')
                    logger.warning(f'RWTD_ADB_REFRESH_DEVICE_FAILED address={row.serial} error={exc}')
        except Exception as exc:
            for record in list(manager.device_dict.values()):
                if record.get('device') == 'adb' and not record.get('emulator'):
                    invalidate(record.get('address'), 'unavailable')
            logger.warning(f'RWTD_ADB_REFRESH_FAILED error={exc}')
        finally:
            publish()

    @wraps(original_shell)
    def shell(device, *args, **kwargs):
        if device is None:
            raise ValueError('ADB device is unavailable')
        # Do not invoke the framework's disconnect/reconnect policy for a USB
        # identifier. Preserve the exception and invalidate only this device.
        try:
            return device.shell(*args, **kwargs)
        except Exception as exc:
            text = str(exc).lower()
            if any(word in text for word in ('not found', 'offline', 'closed', 'unauthorized', 'disconnected')):
                invalidate(device.serial, 'unavailable')
                publish()
            raise

    manager.adb_connect = connect
    manager.refresh_phones = refresh
    manager.shell_device = shell
    manager._rwtd_connection_repair = True
    logger.info('RWTD_DEVICE_CONNECTION_REPAIR installed')
