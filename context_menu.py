"""Opt-in Explorer actions, per-user only; no shell interpretation of paths."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys

KEYS = (
    (r'Software\Classes\Directory\shell\LGTVCheck', '%1\\.'),
    (r'Software\Classes\Directory\Background\shell\LGTVCheck', '%V\\.'),
    (r'Software\Classes\*\shell\LGTVCheck', '%1'),
)
OWNER = 'LGTVCheck.v1'


def _command():
    if getattr(sys, 'frozen', False):
        return subprocess.list2cmdline([sys.executable])
    interpreter = Path(sys.executable).with_name('pythonw.exe')
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
    return subprocess.list2cmdline([str(interpreter), str(Path(__file__).resolve().with_name('main.py'))])


def _notify():
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def install():
    if os.name != 'nt':
        raise RuntimeError('Контекстное меню доступно только в Windows.')
    import winreg
    # Preflight all keys; never replace an unrelated registration.
    for name, _ in KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                try:
                    owner = winreg.QueryValueEx(key, 'LGTVCheckOwner')[0]
                except OSError:
                    owner = ''
                if owner != OWNER:
                    raise RuntimeError(f'Ключ {name} уже занят другой программой.')
        except FileNotFoundError:
            pass
    command = _command()
    icon = f'"{sys.executable}",0' if getattr(sys, 'frozen', False) else str(Path(__file__).resolve().parent / 'assets' / 'tvcheck.ico')
    for name, argument in KEYS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, name) as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'Проверить видео на ТВ (TVCheck)')
            winreg.SetValueEx(key, 'Icon', 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, 'LGTVCheckOwner', 0, winreg.REG_SZ, OWNER)
            winreg.SetValueEx(key, 'MultiSelectModel', 0, winreg.REG_SZ, 'Single')
            with winreg.CreateKey(key, 'command') as child:
                winreg.SetValueEx(child, '', 0, winreg.REG_SZ, command + ' -- "' + argument + '"')
    _notify()
    return 'Добавлен пункт с иконкой «Проверить видео на ТВ (TVCheck)»: ПКМ по папке, внутри папки или по файлу. Не перемещайте программу после установки меню; при переносе установите меню заново.'


def uninstall():
    if os.name != 'nt':
        raise RuntimeError('Контекстное меню доступно только в Windows.')
    import winreg
    for name, _ in KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                try:
                    owner = winreg.QueryValueEx(key, 'LGTVCheckOwner')[0]
                except OSError:
                    owner = ''
                if owner != OWNER:
                    raise RuntimeError(f'Ключ {name} не принадлежит LGTVCheck; удаление отменено.')
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name + r'\command')
            except FileNotFoundError:
                pass
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
        except FileNotFoundError:
            pass
    _notify()
    return 'Пункты LGTVCheck удалены. Настройки MediaInfo и ассоциации файлов не изменены.'
