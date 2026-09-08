"""MediaInfo discovery and bounded, read-only per-file analysis."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading

EXTENSIONS = frozenset('.mkv .mp4 .m4v .mov .avi .wmv .asf .ts .trp .tp .mts .m2ts .mpg .mpeg .dat .vob .3gp .3g2 .rm .rmvb .webm .ogm .ogv .m2v .divx .flv .hevc .h264 .h265 .mxf'.split())
TIMEOUT = 45
TRANSIENT_WINERRORS = frozenset({50})
TRANSIENT_RETRY_ATTEMPTS = 5


def _is_transient_io_error(exc: BaseException) -> bool:
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, 'winerror', None) in TRANSIENT_WINERRORS:
            return True
        if '[winerror 50]' in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _retry_transient(operation, stop_event: threading.Event, on_retry=None, attempts: int = TRANSIENT_RETRY_ATTEMPTS, delay: float = 0.8):
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_transient_io_error(exc) or attempt >= attempts:
                raise
            if stop_event.is_set():
                return None
            if on_retry:
                on_retry(attempt, exc)
            if stop_event.wait(delay * attempt):
                return None
def _retry_failure_message(exc: BaseException) -> str:
    if _is_transient_io_error(exc):
        return (f'Ошибка доступа не исчезла после {TRANSIENT_RETRY_ATTEMPTS} попыток: {exc}. '
                'Файл занят другой программой или носитель отвечает ошибкой; '
                'повторите проверку или перезапустите программу обычным способом (двойным щелчком).')
    return str(exc) or exc.__class__.__name__


def app_dir() -> Path:
    return Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent


def _gui_executable(path: Path) -> bool:
    # Never launch MediaInfo GUI as if it were the command-line edition.
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            return False
        stream.seek(0x3C)
        pe_offset = struct.unpack('<I', stream.read(4))[0]
        stream.seek(pe_offset + 24 + 68)
        return struct.unpack('<H', stream.read(2))[0] == 2


def _installed_paths():
    if os.name != 'nt':
        return
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\App Paths\MediaInfo.exe', 0, winreg.KEY_READ | view) as key:
                    yield Path(winreg.QueryValue(key, None)).parent / 'MediaInfo.dll'
            except OSError:
                pass
            try:
                with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as base:
                    for index in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base, winreg.EnumKey(base, index)) as key:
                                name = winreg.QueryValueEx(key, 'DisplayName')[0]
                                if 'mediainfo' not in name.lower():
                                    continue
                                try:
                                    location = winreg.QueryValueEx(key, 'InstallLocation')[0]
                                    if location:
                                        yield Path(location) / 'MediaInfo.dll'
                                except OSError:
                                    pass
                                try:
                                    icon = winreg.QueryValueEx(key, 'DisplayIcon')[0].rsplit(',', 1)[0].strip('"')
                                    yield Path(icon).parent / 'MediaInfo.dll'
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                pass


def find_mediainfo(explicit: str = '') -> str:
    candidates = []
    if explicit.strip():
        candidates.append(Path(os.path.expandvars(explicit.strip().strip('"'))).expanduser())
    else:
        candidates.extend([app_dir() / 'tools' / 'MediaInfo.exe', app_dir() / 'MediaInfo.dll'])
        if getattr(sys, 'frozen', False):
            candidates.append(Path(sys._MEIPASS) / 'tools' / 'MediaInfo.exe')
        command = shutil.which('mediainfo')
        if command:
            candidates.append(Path(command))
        candidates.extend(_installed_paths())
        for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            base = os.environ.get(variable)
            if base:
                candidates.extend([Path(base) / 'MediaInfo' / 'MediaInfo.dll', Path(base) / 'Programs' / 'MediaInfo' / 'MediaInfo.dll'])
    errors = []
    for candidate in dict.fromkeys(candidates):
        if candidate.is_dir():
            candidate = candidate / 'MediaInfo.dll'
        if not candidate.is_file():
            continue
        try:
            if candidate.suffix.lower() == '.dll':
                return str(candidate.resolve())
            if _gui_executable(candidate):
                dll = candidate.with_name('MediaInfo.dll')
                if dll.is_file():
                    return str(dll.resolve())
                errors.append('Выбран графический MediaInfo.exe без соседнего MediaInfo.dll.')
                continue
            return str(candidate.resolve())
        except (OSError, struct.error) as exc:
            errors.append(str(exc))
    raise RuntimeError('MediaInfo не найден. Выберите MediaInfo.dll из установленной программы или MediaInfo.exe версии CLI. '
                       'Официальная загрузка: https://mediaarea.net/en/MediaInfo/Download/Windows. ' + ' '.join(errors))


def dll_metadata(library: str, filename: str) -> dict:
    """Called only in a disposable child: native parser cannot freeze the UI."""
    dll = ctypes.WinDLL(library)
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t
    text = ctypes.c_wchar_p
    for name, arguments, result in (
        ('New', [], pointer), ('Delete', [pointer], None),
        ('Open', [pointer, text], size), ('Close', [pointer], None),
        ('Option', [pointer, text, text], text), ('Inform', [pointer, size], text),
    ):
        function = getattr(dll, 'MediaInfo_' + name)
        function.argtypes = arguments
        function.restype = result
    handle = dll.MediaInfo_New()
    if not handle:
        raise RuntimeError('MediaInfo: не удалось создать анализатор.')
    try:
        dll.MediaInfo_Option(handle, 'Output', 'JSON')
        dll.MediaInfo_Option(handle, 'Language', 'raw')
        dll.MediaInfo_Option(handle, 'Complete', '1')
        if not dll.MediaInfo_Open(handle, filename):
            raise RuntimeError('MediaInfo не смог открыть файл.')
        return json.loads(dll.MediaInfo_Inform(handle, 0) or '{}')
    finally:
        dll.MediaInfo_Close(handle)
        dll.MediaInfo_Delete(handle)


def read_metadata(filename: str, analyzer: str) -> dict:
    if Path(analyzer).suffix.lower() == '.dll':
        command = [sys.executable]
        if not getattr(sys, 'frozen', False):
            command.append(str(app_dir() / 'main.py'))
        command.extend(['--probe-json', analyzer, filename])
    else:
        command = [analyzer, '--Output=JSON', '--Language=raw', '--Full', filename]
    try:
        # stdin=DEVNULL: a windowed GUI is often started with invalid std handles;
        # inheriting them makes CreateProcess fail with WinError 50 ("not supported").
        completed = subprocess.run(command, capture_output=True, timeout=TIMEOUT,
                                   stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'MediaInfo не ответил за {TIMEOUT} с; файл пропущен.') from exc
    if completed.returncode:
        detail = completed.stderr.decode('utf-8', errors='replace').strip()[-1200:]
        raise RuntimeError(f'MediaInfo завершился с кодом {completed.returncode}. {detail}')
    try:
        data = json.loads(completed.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('MediaInfo не вернул JSON. Нужна версия CLI или совместимая MediaInfo.dll.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('media'), dict) or not data['media'].get('track'):
        raise RuntimeError('MediaInfo не распознал медиапотоки; файл пуст, повреждён или имеет неизвестный формат.')
    tracks = data['media']['track']
    if not isinstance(tracks, list) or not any(isinstance(t, dict) and t.get('@type') in {'Video', 'Audio'} for t in tracks):
        raise RuntimeError('MediaInfo не распознал ни видео, ни аудио: файл повреждён, пуст или имеет неизвестный формат.')
    return data


def error_report(path: str, message: str) -> dict:
    return dict(path=path, status='error', summary=message, container='—',
                video=[], audio=[], subtitles=[], issues=[message])


def iter_reports(path: str, recursive: bool = True, mode: str = 'usb',
                 mediainfo: str = '', stop_event: threading.Event | None = None,
                 profile_id: str = 'lg-uk6300', full: bool = False,
                 link_mbps: float | None = None, progress=None):
    from engine import assess
    from tv_profiles import get_profile
    from bitrate import measure, apply, Cancelled
    stop_event = stop_event or threading.Event()
    profile = get_profile(profile_id)
    if link_mbps is not None:
        import math
        if not math.isfinite(link_mbps) or link_mbps <= 0:
            raise ValueError('Устойчивая скорость должна быть положительным числом Мбит/с.')
    original = path
    if not path.strip().strip('"'):
        yield error_report(original, 'Укажите путь к файлу или папке.')
        return
    target = Path(os.path.expandvars(path.strip().strip('"'))).expanduser().absolute()
    try:
        _retry_transient(
            lambda: target.stat(),
            stop_event,
            (lambda attempt, exc: progress(
                str(target),
                f'Доступ к файлу: повтор {attempt}/4 после временной ошибки',
                None,
            )) if progress else None,
        )
        analyzer = _retry_transient(
            lambda: find_mediainfo(mediainfo),
            stop_event,
            (lambda attempt, exc: progress(
                str(target),
                f'MediaInfo: повтор {attempt}/4 после временной ошибки',
                None,
            )) if progress else None,
        )
    except (OSError, RuntimeError) as exc:
        yield error_report(str(target), _retry_failure_message(exc))
        return
    if target.is_file():
        sources = iter([target])  # Explicit files are inspected regardless of extension.
    elif target.is_dir():
        sources = _walk(target, recursive, stop_event)
    else:
        yield error_report(str(target), 'Путь не является обычным файлом или папкой.')
        return
    for source in sources:
        if stop_event.is_set():
            break
        if isinstance(source, dict):
            yield source
            continue
        try:
            if progress:
                progress(str(source), 'MediaInfo', None)
            metadata = _retry_transient(
                lambda: read_metadata(str(source), analyzer),
                stop_event,
                (lambda attempt, exc: progress(
                    str(source),
                    f'MediaInfo: повтор {attempt}/4 после временной ошибки',
                    None,
                )) if progress else None,
            )
            measurement, measurement_error = None, ''
            if full and not stop_event.is_set():
                try:
                    if progress:
                        progress(str(source), 'Чтение всего файла / пики', 0)
                    measurement = _retry_transient(
                        lambda: measure(
                            str(source),
                            stop_event,
                            (lambda percent, seconds: progress(
                                str(source), 'Измерение битрейта', percent,
                            )) if progress else None,
                        ),
                        stop_event,
                        (lambda attempt, exc: progress(
                            str(source),
                            f'Измерение: повтор {attempt}/4 после временной ошибки',
                            None,
                        )) if progress else None,
                    )
                except Cancelled:
                    break
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    measurement_error = f'Пики не измерены: {exc}'
            if stop_event.is_set():
                break
            # Raw stream order is preserved by both parsers. Fill absent averages only
            # when video counts match; measured fields remain separate in the report.
            if measurement:
                videos = [t for t in metadata['media']['track'] if isinstance(t, dict) and t.get('@type') == 'Video']
                measured = [t for t in measurement['streams'] if t['type'] == 'video']
                if len(videos) == len(measured):
                    for video, value in zip(videos, measured):
                        if not video.get('BitRate') and value['average_mbps']:
                            video['BitRate'] = str(value['average_mbps'] * 1e6)
            alternatives = {}
            for delivery in ('usb', 'network'):
                assessment = assess(str(source), metadata, delivery, profile)
                apply(assessment, measurement, delivery, link_mbps if delivery == mode else None, measurement_error)
                alternatives[delivery] = assessment
            report = alternatives[mode]
            report['playback'] = {
                delivery: {key: value[key] for key in ('status', 'summary', 'issues')}
                for delivery, value in alternatives.items()
            }
            report['mode'] = mode
            report['profile_id'] = profile_id
            report['full_scan'] = full
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            report = error_report(str(source), _retry_failure_message(exc))
        yield report


def _walk(root: Path, recursive: bool, stop: threading.Event):
    # Do not follow junctions/symlinks: avoid cycles and escaping the chosen tree.
    pending = [root]
    while pending and not stop.is_set():
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                children = sorted(entries, key=lambda item: item.name.casefold())
            subfolders = []
            for entry in children:
                if stop.is_set():
                    return
                try:
                    if entry.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(entry.path)):
                        continue
                    if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in EXTENSIONS:
                        yield Path(entry.path)
                    elif recursive and entry.is_dir(follow_symlinks=False):
                        subfolders.append(Path(entry.path))
                except OSError as exc:
                    yield error_report(entry.path, f'Нет доступа: {exc}')
            pending.extend(reversed(subfolders))
        except OSError as exc:
            yield error_report(str(folder), f'Не удалось прочитать папку: {exc}')


def check_dependencies(mediainfo: str = '') -> dict:
    from bitrate import find_ffprobe
    result = {}
    for name, find, args in (
        ('MediaInfo', lambda: find_mediainfo(mediainfo), ['--Version']),
        ('FFprobe', find_ffprobe, ['-version']),
    ):
        try:
            executable = find()
            if Path(executable).suffix.lower() == '.dll':
                # Architecture/API validity is checked in the isolated file reader.
                result[name] = {'ok': True, 'path': executable, 'version': 'DLL: API будет проверен при чтении файла'}
                continue
            process = subprocess.run([executable, *args], capture_output=True, timeout=10,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            output = process.stdout.decode('utf-8-sig', errors='replace').strip()
            if process.returncode or not output:
                raise RuntimeError(f'Не запускается: код {process.returncode}. Восстановите tools из дистрибутива.')
            result[name] = {'ok': True, 'path': executable, 'version': ' '.join(output.splitlines()[:2])}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result[name] = {'ok': False, 'error': str(exc)}
    return result
