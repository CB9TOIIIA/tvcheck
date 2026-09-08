"""Windows app entry point; --cli also supports batch use and JSON reports."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys


def _detach_own_console():
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        import ctypes
        processes = (ctypes.c_ulong * 2)()
        if ctypes.windll.kernel32.GetConsoleProcessList(processes, 2) == 1:
            ctypes.windll.kernel32.FreeConsole()


def main() -> int:
    parser = argparse.ArgumentParser(description='TVCheck: кодеки, аудио и измеренные пики битрейта. Без --cli открывается окно.')
    parser.add_argument('path', nargs='?', default='', help='Файл или папка')
    parser.add_argument('--cli', action='store_true', help='Текстовый отчёт вместо окна')
    parser.add_argument('--mode', choices=['usb', 'network'], default='usb', help='USB или сеть/DLNA (CLI)')
    parser.add_argument('--no-recursive', action='store_true', help='Без вложенных папок (CLI)')
    parser.add_argument('--mediainfo', default='', help='Путь к MediaInfo.dll или MediaInfo.exe CLI')
    parser.add_argument('--json', metavar='FILE', help='Сохранить JSON-отчёт (CLI)')
    parser.add_argument('--full', action='store_true', help='Дополнительно прочитать весь файл и измерить средний/пиковый битрейт')
    parser.add_argument('--quick', action='store_true', help='Совместимость: оставить только проверку MediaInfo (режим по умолчанию)')
    parser.add_argument('--profile', default='lg-uk6300', help='ID профиля телевизора (CLI)')
    parser.add_argument('--link-mbps', type=float, help='Измеренная устойчивая скорость USB/сети, Мбит/с (CLI)')
    parser.add_argument('--check-deps', action='store_true', help='Проверить запуск встроенных анализаторов')
    parser.add_argument('--install-menu', action='store_true', help='Установить пункт ПКМ для текущего пользователя')
    parser.add_argument('--uninstall-menu', action='store_true', help='Удалить пункт ПКМ')
    parser.add_argument('--probe-json', nargs=2, metavar=('DLL', 'FILE'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    if args.probe_json:
        from backend import dll_metadata
        try:
            print(json.dumps(dll_metadata(*args.probe_json), ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f'Ошибка MediaInfo DLL: {exc}. Проверьте разрядность DLL (нужна x64).', file=sys.stderr)
            return 3
    if args.check_deps:
        from backend import check_dependencies
        dependencies = check_dependencies(args.mediainfo)
        print(json.dumps(dependencies, ensure_ascii=False, indent=2))
        return 0 if all(value['ok'] for value in dependencies.values()) else 3
    if args.install_menu or args.uninstall_menu:
        import context_menu
        try:
            print(context_menu.uninstall() if args.uninstall_menu else context_menu.install())
            return 0
        except (OSError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 3
    if not args.cli:
        _detach_own_console()
        from gui import run
        run(args.path, args.mediainfo)
        return 0
    from backend import iter_reports
    from engine import DISCLAIMER
    from tv_profiles import get_profile
    try:
        profile = get_profile(args.profile)
        if args.link_mbps is not None:
            import math
            if not math.isfinite(args.link_mbps) or args.link_mbps <= 0:
                raise ValueError('--link-mbps: нужно положительное число.')
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    path = args.path or input('Путь к видео или папке: ').strip()
    interactive = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    if interactive and os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 4)
    colors = {'ok': '\033[92m', 'warn': '\033[93m', 'fail': '\033[91m', 'error': '\033[91m'}
    labels = {'ok': 'ОК', 'warn': 'ВНИМАНИЕ', 'fail': 'НЕСОВМЕСТИМО', 'error': 'ОШИБКА', 'info': 'СПРАВКА'}
    print(profile['name'] + '  /  ПО ' + (profile['firmware'] or 'не указано') + '  /  ' + args.mode.upper())
    print(DISCLAIMER + '\n')
    reports = []
    interrupted = False
    try:
        for report in iter_reports(path, not args.no_recursive, args.mode, args.mediainfo,
                                   profile_id=args.profile, full=args.full and not args.quick, link_mbps=args.link_mbps):
            reports.append(report)
            status = report['status']
            start, end = (colors[status], '\033[0m') if interactive else ('', '')
            print(f"{start}[{labels[status]}] {report['path']}{end}")
            print('  ' + report['summary'])
            from bitrate import timestamp
            rate = report.get('bitrate', {})
            if rate.get('peak_mbps') is not None:
                average = rate.get('average_mbps')
                average_text = f'{average:.2f}' if average is not None else 'не определён'
                print(f"  Поток Σ: средний {average_text} Мбит/с; пик за 1 с {rate['peak_mbps']:.2f} Мбит/с на {timestamp(rate['peak_second'])}")
            for delivery, value in report.get('playback', {}).items():
                print(f"  {delivery.upper()}: {labels[value['status']]}")
            for section in ('video', 'audio', 'subtitles'):
                for track in report[section]:
                    print(f"  [{labels[track['status']]}] {track['label']}: {track['details']}")
                    for issue in track['issues']:
                        print('    - ' + issue)
            for issue in report['issues']:
                print('  - ' + issue)
            print()
    except KeyboardInterrupt:
        interrupted = True
        print('\nОтменено; отчёт неполный.')
    counts = Counter(report['status'] for report in reports)
    print('ИТОГО: ' + ', '.join(f'{labels[key]}: {counts[key]}' for key in ('ok', 'warn', 'fail', 'error')))
    if not reports:
        print('Видеофайлы не найдены. Можно передать отдельный файл с любым расширением.')
    if args.json:
        document = dict(model=profile['name'], firmware=profile['firmware'], mode=args.mode,
                        path=path, partial=interrupted, disclaimer=DISCLAIMER, profile=profile,
                        sources=profile['sources'], results=reports)
        try:
            Path(args.json).write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            print(f'Не удалось сохранить отчёт: {exc}', file=sys.stderr)
            return 3
    return 130 if interrupted else 3 if counts['error'] or not reports else 2 if counts['fail'] else 1 if counts['warn'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
