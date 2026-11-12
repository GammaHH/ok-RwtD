"""Shared indigo and purple palette for the native shell and browser editors."""
from pathlib import Path


PALETTE = {
    'window': '#10121F',
    'sidebar': '#0D1020',
    'surface': '#181B2B',
    'surface_alt': '#202337',
    'control': '#24283D',
    'control_hover': '#30334B',
    'control_pressed': '#1B1E30',
    'border': '#343750',
    'border_strong': '#55516E',
    'separator': '#292D44',
    'text': '#EAE9F2',
    'text_secondary': '#B2B1C6',
    'text_tertiary': '#9694AD',
    'text_disabled': '#6C6B80',
    'accent': '#844FB5',
    'highlight': '#FFD800',
    'accent_text': '#D2FF00',
    'selected_surface': '#352442',
    'selection_fill': '#844FB522',
    'graph_highlight': '#FFD000',
    'cyan': '#72D7ED',
    'mint': '#79DEC0',
    'green': '#A1DC86',
    'orange': '#FFB900',
    'pink': '#E993CB',
    'danger': '#F08391',
    'warning': '#FFD800',
}
_native_installed = False
_native_receiver = None
_native_palette = dict(PALETTE)


def current_palette():
    from .theme_settings import ThemeStore
    try:return ThemeStore(Path(__file__).resolve().parents[2]).snapshot()['palette']
    except (ValueError,OSError,TypeError):return dict(PALETTE)


def browser_stylesheet():
    variables = ';'.join(f'--rwtd-{key.replace("_", "-")}:{value}' for key, value in current_palette().items())
    css = Path(__file__).resolve().parents[2] / 'assets/rwtd/theme.css'
    return ':root{' + variables + '}\n' + css.read_text(encoding='utf-8')


def install_native_theme():
    global _native_installed, _native_receiver
    if _native_installed:
        return
    from PySide6.QtCore import QObject, Signal, Slot
    from PySide6.QtGui import QColor
    from ok.ui.qt.kit.theme import DARK_TOKENS, Theme, readable_accent, theme
    from ok.ui.qt.common.config import cfg

    _native_palette.update(current_palette())
    DARK_TOKENS.update({key: value for key, value in _native_palette.items() if key in DARK_TOKENS})
    cfg.set_theme_mode(Theme.DARK.value)

    def apply_accent():
        color = QColor(_native_palette['accent'])
        expected = color if theme.is_dark() else readable_accent(color)
        if theme.accent().name() != expected.name():
            theme.set_accent(color)

    theme.changed.connect(apply_accent)
    theme.set_theme(Theme.DARK)
    apply_accent()
    class ThemeReceiver(QObject):
        requested=Signal(object)

        def __init__(self):
            super().__init__()
            self.requested.connect(self.apply)

        @Slot(object)
        def apply(self,palette):
            _native_palette.update(palette)
            DARK_TOKENS.update({key:value for key,value in palette.items() if key in DARK_TOKENS})
            apply_accent()
            theme._refresh()

    _native_receiver=ThemeReceiver()
    _native_installed = True


def request_native_palette(palette):
    if _native_receiver is None:return False
    _native_receiver.requested.emit(dict(palette))
    return True
