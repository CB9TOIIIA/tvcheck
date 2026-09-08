"""Streaming packet analysis: fixed one-second payload windows, no decoding."""
from __future__ import annotations

from collections import defaultdict
import json
import math
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from backend import app_dir
from tv_profiles import resource_dir


class Cancelled(Exception):
    pass


def find_ffprobe() -> str:
    for path in (app_dir() / 'tools' / 'ffprobe.exe', resource_dir() / 'tools' / 'ffprobe.exe'):
        if path.is_file():
            return str(path)
    command = shutil.which('ffprobe')
    if command:
        return command
    raise RuntimeError('Нет tools/ffprobe.exe. Восстановите полный дистрибутив: он нужен для измерения битрейта, отдельно устанавливать ничего не требуется.')


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None

def _split_ffprobe_diagnostics(value: str) -> tuple[str, list[str]]:
    relevant = []
    technical = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if 'Unsupported codec with id' in line and 'input stream' in line:
            continue
        if re.search(r'\bIncreasing reorder buffer to \d+\b', line, re.I):
            technical.append(line)
        else:
            relevant.append(line)
    return '\n'.join(relevant).strip(), technical


def measure(filename: str, stop: threading.Event, progress=None) -> dict:
    executable = find_ffprobe()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        header = subprocess.run([executable, '-v', 'error', '-show_entries',
                                 'stream=index,codec_type:stream_disposition=attached_pic:format=duration,start_time',
                                 '-of', 'json', filename], capture_output=True, timeout=45,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('FFprobe не ответил при чтении заголовков за 45 с.') from exc
    if stop.is_set():
        raise Cancelled()
    if header.returncode:
        raise RuntimeError('FFprobe не смог открыть файл: ' + header.stderr.decode('utf-8', errors='replace')[-1200:])
    data = json.loads(header.stdout)
    streams = {int(s['index']): s['codec_type'] for s in data.get('streams', [])
               if s.get('codec_type') in {'video', 'audio'} and not s.get('disposition', {}).get('attached_pic')}
    if not any(kind == 'video' for kind in streams.values()):
        raise RuntimeError('FFprobe не нашёл видеопакеты.')
    origin = _finite(data.get('format', {}).get('start_time')) or 0.0
    duration = _finite(data.get('format', {}).get('duration'))
    bins = {index: defaultdict(int) for index in streams}
    totals = dict.fromkeys(streams, 0)
    first, last, previous = {}, {}, {}
    missing = 0
    messages = []
    last_progress = 0.0
    command = [executable, '-v', 'warning', '-show_packets', '-show_entries',
               'packet=stream_index,dts_time,pts_time,duration_time,size', '-of', 'compact=p=0:nk=0', filename]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True,
                                   encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL,
                                   creationflags=flags)
        finished = threading.Event()
        timed_out = threading.Event()

        def watchdog():
            deadline = time.monotonic() + 7200
            while not finished.wait(0.1):
                if stop.is_set() or time.monotonic() >= deadline:
                    if not stop.is_set():
                        timed_out.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

        guard = threading.Thread(target=watchdog, daemon=True)
        guard.start()
        try:
            for line in process.stdout:
                fields = dict(part.split('=', 1) for part in line.strip().split('|') if '=' in part)
                if 'stream_index' not in fields:
                    continue  # Packet side-data lines are not packets.
                try:
                    index = int(fields['stream_index'])
                    if index not in streams:
                        continue
                    size = int(fields['size'])
                    timestamp = _finite(fields.get('dts_time'))
                    if timestamp is None:
                        timestamp = _finite(fields.get('pts_time'))
                    if timestamp is None or size < 0:
                        missing += 1
                        continue
                    relative = timestamp - origin
                    if abs(relative) > 366 * 86400:
                        missing += 1
                        continue
                    # Leading decoder-delay packets belong to the initial interval.
                    second = max(0, math.floor(relative + 1e-9))
                    bins[index][second] += size
                    totals[index] += size
                    step = _finite(fields.get('duration_time')) or 0.0
                    first[index] = min(first.get(index, timestamp), timestamp)
                    last[index] = max(last.get(index, timestamp), timestamp + max(0, step))
                    if index in previous and (timestamp < previous[index] - 1 or timestamp > previous[index] + 60):
                        if 'Разрывы временных меток: секундные окна могут быть неточными.' not in messages:
                            messages.append('Разрывы временных меток: секундные окна могут быть неточными.')
                    previous[index] = timestamp
                    now = time.monotonic()
                    if progress and now - last_progress >= 0.4:
                        percent = min(99, max(0, relative / duration * 100)) if duration and duration > 0 else None
                        progress(percent, max(0, relative))
                        last_progress = now
                except (KeyError, ValueError, OverflowError):
                    missing += 1
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            finished.set()
            guard.join(timeout=1)
        if stop.is_set():
            raise Cancelled()
        if timed_out.is_set():
            raise RuntimeError('Полный анализ превысил 2 часа; результаты не считаются полными.')
        errors.seek(0)
        diagnostic = errors.read(6000).decode('utf-8', errors='replace').strip()
    diagnostic, technical_diagnostics = _split_ffprobe_diagnostics(diagnostic)
    if code:
        raise RuntimeError(f'FFprobe завершился с кодом {code}. {diagnostic}')
    if diagnostic:
        messages.append('FFprobe сообщил проблемы видео/аудиопотоков: ' + diagnostic)
    if missing:
        messages.append(f'Пакетов без корректного размера/времени: {missing}; измерение неполное.')
    if not first:
        raise RuntimeError('Нет пакетов с пригодными временными метками; пики определить нельзя.')
    combined = defaultdict(int)
    results = []
    for index, kind in streams.items():
        if not bins[index]:
            messages.append(f'Поток {index}: нет пакетов с временными метками.')
            continue
        peak_second = max(bins[index], key=bins[index].get)
        span = last[index] - first[index]
        if span <= 0:
            messages.append(f'Поток {index}: не удалось определить длительность.')
        results.append(dict(index=index, type=kind, average_mbps=totals[index] * 8 / span / 1e6 if span > 0 else None,
                            peak_mbps=bins[index][peak_second] * 8 / 1e6, peak_second=peak_second,
                            payload_bytes=totals[index], duration=span))
        for second, size in bins[index].items():
            combined[second] += size
    peak_second = max(combined, key=combined.get)
    span = max(last.values()) - min(first.values())
    if duration and duration > 10 and span < duration * 0.9:
        messages.append(f'Пакеты покрывают {span:.1f} с из заявленных {duration:.1f} с; возможен обрыв/неполное чтение.')
    if duration and duration < 1:
        messages.append('Файл короче секунды: пик фиксированного окна не характеризует длительное воспроизведение.')
    if progress:
        progress(100, span)
    return dict(complete=not messages, method='fixed-1s-payload', window_seconds=1,
                average_mbps=sum(totals.values()) * 8 / span / 1e6 if span > 0 else None,
                peak_mbps=combined[peak_second] * 8 / 1e6, peak_second=peak_second,
                duration=span, streams=results, issues=messages,
                diagnostics=technical_diagnostics,
                note='Сумма пакетов видео и всех аудиодорожек; без накладных расходов контейнера/сети. Пик за фиксированную 1 с, не мгновенный и не скользящий максимум.')


def apply(report: dict, measurement: dict | None, mode: str, link_mbps: float | None = None, error: str = ''):
    """Add optional measured evidence and preserve MediaInfo-only verdicts."""
    mode = mode.lower()
    rank = {'ok': 0, 'warn': 1, 'fail': 2, 'error': 3}

    def issue(status, message):
        if rank[status] > rank[report['status']]:
            report['status'] = status
        report['issues'].append(message)

    report['bitrate'] = measurement or {
        'complete': False,
        'issues': [error or 'Битрейт не измерялся; полный анализ доступен отдельно.'],
    }
    if measurement and measurement.get('diagnostics'):
        report.setdefault('technical_notes', []).extend(
            'FFprobe: ' + message for message in measurement['diagnostics']
        )
    if not measurement:
        if error:
            issue('warn', error)
    else:
        if not measurement['complete']:
            issue('warn', 'Полный анализ обнаружил проблемы: ' + ' '.join(measurement['issues']))
        video_measurements = [s for s in measurement['streams'] if s['type'] == 'video']
        if len(video_measurements) != len(report['video']):
            issue('warn', 'Число видеопотоков MediaInfo/FFprobe различается; сопоставление пиков с кодеками не подтверждено.')
        else:
            for track, measured in zip(report['video'], video_measurements):
                track['measured_bitrate'] = measured
                limit = track.get('limit_mbps')
                peak = measured['peak_mbps']
                if limit and peak > limit:
                    message = f"Пик видео {peak:.2f} Мбит/с на {timestamp(measured['peak_second'])} выше предела {limit:g} Мбит/с. Возможны рывки/зависания; перекодировать с maxrate/bufsize ниже предела."
                    track['status'] = 'fail'
                    track['issues'].append(message)
                elif limit and peak > limit * 0.85:
                    message = f'Пик видео {peak:.2f} Мбит/с близок к пределу {limit:g} Мбит/с; запас менее 15% (эвристика, не отдельный лимит LG).'
                    if track['status'] == 'ok':
                        track['status'] = 'warn'
                    track['issues'].append(message)
            if report['video'] and all(t['status'] == 'fail' for t in report['video']):
                issue('fail', 'Совместимая видеодорожка не подтверждена; см. видео и измеренный битрейт ниже.')
            elif any(t['status'] != 'ok' for t in report['video']):
                issue('warn', 'Проверьте ограничения видеодорожек и их пиковый битрейт.')
        if link_mbps is not None:
            report['link_mbps'] = link_mbps
            peak = measurement['peak_mbps']
            if peak > link_mbps:
                issue('warn', f'Суммарный пик {peak:.2f} Мбит/с выше указанной устойчивой скорости {link_mbps:g} Мбит/с. Риск остановок буферизации; это не запрет кодека.')
            elif peak > link_mbps * 0.8:
                issue('warn', f'Суммарный пик {peak:.2f} Мбит/с оставляет менее 20% запаса к скорости {link_mbps:g} Мбит/с (эвристика).')
        if mode == 'network':
            report['issues'].append('Отказ DLNA при рабочем USB не доказывает превышение битрейта. Проверьте direct play/транскодирование, MIME-профиль контейнера и выбранную аудиодорожку на сервере.')

    # Missing but non-contradictory metadata is a technical note, not a failure.
    # A complete measured USB file with no hard risk is therefore simply OK.
    if mode == 'usb' and measurement and measurement.get('complete') and report['status'] == 'warn':
        risk_markers = (
            'Пик видео ', 'выше указанной устойчивой скоростью',
            'Число видеопотоков MediaInfo/FFprobe', 'Полный анализ обнаружил проблемы',
        )
        hard_risk = any(any(marker in issue for marker in risk_markers) for issue in report['issues'])
        hard_risk = hard_risk or any(
            any(marker in issue for marker in ('Пик видео ', 'выше указанной устойчивой скоростью'))
            for section in ('video', 'audio') for track in report.get(section, [])
            for issue in track.get('issues', [])
        )
        has_failure = any(
            track.get('status') in {'fail', 'error'}
            for section in ('video', 'audio')
            for track in report.get(section, [])
        )
        if not hard_risk and not has_failure:
            technical_notes = list(report.get('technical_notes', []))
            technical_notes.extend(report.get('issues', []))
            report['technical_notes'] = technical_notes
            report['issues'] = []
            for section in ('video', 'audio'):
                for track in report.get(section, []):
                    if track.get('status') == 'warn':
                        report.setdefault('technical_track_notes', []).append({
                            'label': track.get('label', 'Дорожка'),
                            'issues': list(track.get('issues', [])),
                        })
                        track['issues'] = []
                        track['status'] = 'ok'
            report['status'] = 'ok'

    if measurement:
        summary = {
            'ok': 'ОК по профилю и измеренному потоку. Воспроизведение на ТВ не тестировалось.',
            'warn': 'Нужна проверка: есть риск, неизвестные параметры или ограничения доставки.',
            'fail': 'Найдено несоответствие профилю ТВ: см. видео, звук и битрейт.',
            'error': 'Ошибка чтения: совместимость не определена.',
        }
    else:
        summary = {
            'ok': 'ОК по характеристикам MediaInfo. Битрейт не измерялся.',
            'warn': 'Нужна проверка характеристик MediaInfo или доставки.',
            'fail': 'Найдено несоответствие профилю ТВ по MediaInfo.',
            'error': 'Ошибка чтения: совместимость не определена.',
        }
    report['summary'] = summary[report['status']]
    return report

def timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
