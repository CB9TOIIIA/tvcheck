"""TVCheck desktop UI with six UI-kit skins; workers never touch widgets."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import backend
import context_menu
from bitrate import timestamp
from engine import DISCLAIMER
import tv_profiles

STATUS = {'ok': 'ОК', 'warn': 'Проверить', 'fail': 'Не подходит', 'error': 'Ошибка', 'info': 'Справка'}
STATUS_ICON = {'ok': '✓', 'warn': '!', 'fail': '×', 'error': '!', 'info': 'i'}
SKIN_IDS = (
    '01_blue_day', '02_crimson_night', '03_green_cozy',
    '04_violet_neon', '05_sakura_red', '06_slate_blue',
)
MODES = {'USB': 'usb', 'DLNA / сеть': 'network'}


def _mbps(value):
    return f'{value:.2f}' if isinstance(value, (int, float)) else '—'


def _load_skins():
    root = tv_profiles.resource_dir() / 'assets' / 'skins'
    layout = json.loads((root / 'layout_spec.json').read_text(encoding='utf-8'))
    result = {}
    for skin_id in SKIN_IDS:
        folder = root / skin_id
        theme = json.loads((folder / 'theme.json').read_text(encoding='utf-8'))
        colors = dict(theme['colors'], soft=theme['colors']['surface2'])
        result[skin_id] = dict(
            id=skin_id, name=theme['name'], colors=colors, layout=layout,
            sizes=theme['sizes'], dark=theme['dark'],
            recommendedWindow=dict(
                width=layout['window']['preferred'][0], height=layout['window']['preferred'][1],
                minWidth=layout['window']['minimum'][0], minHeight=layout['window']['minimum'][1],
            ),
            art=folder / 'hero.png', folder=folder,
        )
    return result


def text_report(snapshot, reports):
    lines = ['TVCheck — проверка совместимости', f"ТВ: {snapshot['profile']['name']}",
             f"Режим: {snapshot['mode']} | Путь: {snapshot['path']}",
             f"Проверка битрейта: {'да' if snapshot['full'] else 'нет (MediaInfo-only)'} | Отчёт неполный: {snapshot.get('partial', False)}", DISCLAIMER]
    for report in reports:
        lines += ['', '=' * 70, f"[{STATUS[report['status']]}] {report['path']}", report['summary']]
        rate = report.get('bitrate', {})
        lines += [f"Битрейт Σ: средний {_mbps(rate.get('average_mbps'))}, пик 1 с {_mbps(rate.get('peak_mbps'))} Мбит/с; время {timestamp(rate.get('peak_second', 0))}"]
        lines += ['- ' + issue for issue in report['issues']]
        lines += ['Справка: ' + issue for issue in report.get('technical_notes', [])]
        for note in report.get('technical_track_notes', []):
            lines += [f"Справка · {note['label']}: " + issue for issue in note.get('issues', [])]
        for kind in ('video', 'audio', 'subtitles'):
            for track in report[kind]:
                lines += [f"[{STATUS[track['status']]}] {track['label']}: {track['details']}"]
                lines += ['  - ' + issue for issue in track['issues']]
        for mode, value in report.get('playback', {}).items():
            lines += [f"{mode.upper()}: {STATUS[value['status']]} — {value['summary']}"]
    lines += ['', 'Источники профиля:', *snapshot['profile']['sources']]
    return '\n'.join(lines) + '\n'


class _AutoScrollbar(ttk.Scrollbar):
    """Keep ttk's drag/page bindings; only remove a fitting view's grid track."""

    def __init__(self, parent, orient='vertical', **kwargs):
        super().__init__(parent, orient=orient, style=f'Skin.{orient.title()}.TScrollbar', **kwargs)
        self._shown = True

    def set(self, first, last):
        super().set(first, last)
        shown = float(first) > 0.0 or float(last) < 1.0
        if shown != self._shown:
            self._shown = shown
            if shown:
                self.grid()
            else:
                self.grid_remove()


class Application:
    def __init__(self, root, initial_path='', mediainfo=''):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.closed = False
        self.ready = False
        self.dependencies = {}
        self.reports = []
        self.snapshot = {}
        self.profiles, self.profile_errors = tv_profiles.load_profiles()
        self.skins = _load_skins()
        if not self.skins:
            raise RuntimeError('Не найдены assets/skins: восстановите полный исходный каталог.')
        settings = tv_profiles.load_settings()
        selected_skin = settings.get('skin', '01_blue_day')
        if selected_skin not in self.skins:
            selected_skin = next(iter(self.skins))
        self.path = tk.StringVar(value=initial_path)
        self.skin = tk.StringVar(value=selected_skin)
        self.layout = self.skins[selected_skin]['layout']
        chosen = settings.get('profile_id', tv_profiles.DEFAULT_ID)
        self.profile_id = chosen if chosen in self.profiles else next(iter(self.profiles), '')
        self.model = tk.StringVar()
        self.mode = tk.StringVar(value='USB')
        self.dlna_enabled = tk.BooleanVar(value=bool(settings.get('dlna_enabled', False)))
        self.full = tk.BooleanVar(value=False)
        self.recursive = tk.BooleanVar(value=True)
        self.link = tk.StringVar(value='')
        self.mediainfo = mediainfo
        self.initial_path = initial_path
        self.activity = tk.StringVar(value='Проверка встроенных MediaInfo и FFprobe…')
        self.counts = tk.StringVar(value='Файлов: 0')
        self.dep_text = tk.StringVar(value='Анализаторы: проверка…')
        self.summary_value = tk.StringVar(value='Готово к проверке')
        self.summary_hint = tk.StringVar(value='Быстрая проверка MediaInfo; полный анализ битрейта запускается отдельно.')
        self.video_metric = tk.StringVar(value='—')
        self.audio_metric = tk.StringVar(value='—')
        self.average = tk.StringVar(value='—')
        self.peak = tk.StringVar(value='—')
        self.peak_at = tk.StringVar(value='—')
        self.usb = tk.StringVar(value='USB: —')
        self.network = tk.StringVar(value='DLNA: —')
        self.controls = []
        self._tk_backgrounds = {'bg': [], 'surface': [], 'soft': [], 'accent': []}
        self._tk_foregrounds = {'text': [], 'muted': [], 'accent': [], 'ok': [], 'warn': [], 'bad': [], 'white': []}
        self.root.title('TVCheck — проверка совместимости видео')
        recommended = self.skins[selected_skin].get('recommendedWindow', {})
        self.root.geometry(f"{recommended.get('width', 1100)}x{recommended.get('height', 800)}")
        self.root.minsize(recommended.get('minWidth', 960), recommended.get('minHeight', 700))
        self.style = ttk.Style(root)
        self.style.theme_use('clam')
        self._init_scrollbars()
        icon = tv_profiles.resource_dir() / 'assets' / 'tvcheck.ico'
        if icon.is_file():
            self.root.iconbitmap(str(icon))
        self._menu()
        self._build()
        self._refresh_profiles()
        self._theme()
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        self.root.bind('<Return>', lambda event: self.scan() if isinstance(event.widget, ttk.Entry) else None)
        self.root.bind('<Configure>', self._on_resize)
        self.root.after(60, self._poll)
        self._check_dependencies()
        if self.profile_errors:
            root.after(300, lambda: messagebox.showwarning('Профили пропущены', '\n'.join(self.profile_errors), parent=root))

    @property
    def skin_data(self):
        return self.skins[self.skin.get()]

    def _track(self, widget, bg='bg', fg=None):
        if bg:
            self._tk_backgrounds.setdefault(bg, []).append(widget)
        if fg:
            self._tk_foregrounds.setdefault(fg, []).append(widget)
        return widget

    def _frame(self, parent, role='bg', **kwargs):
        return self._track(tk.Frame(parent, **kwargs), role)

    def _label(self, parent, text='', role='bg', fg='text', **kwargs):
        label = tk.Label(parent, text=text, **kwargs)
        return self._track(label, role, fg)

    def _button(self, parent, text, command, role='soft', fg='text', **kwargs):
        button = tk.Button(parent, text=text, command=command, relief='flat', borderwidth=0,
                           highlightthickness=0, cursor='hand2', **kwargs)
        return self._track(button, role, fg)

    def _init_scrollbars(self):
        self.scrollbar_images = {}
        for orientation in ('Vertical', 'Horizontal'):
            vertical = orientation == 'Vertical'
            width, height = (14, 24) if vertical else (24, 14)
            images = {state: tk.PhotoImage(master=self.root, width=width, height=height)
                      for state in ('normal', 'active', 'pressed')}
            images['trough'] = tk.PhotoImage(master=self.root, width=14 if vertical else 1,
                                           height=1 if vertical else 14)
            self.scrollbar_images[orientation] = images
            trough = f'TVCheck.{orientation}.Scrollbar.trough'
            thumb = f'TVCheck.{orientation}.Scrollbar.thumb'
            self.style.element_create(trough, 'image', images['trough'], sticky='nswe')
            self.style.element_create(
                thumb, 'image', images['normal'], ('pressed', images['pressed']), ('active', images['active']),
                border=(0, 8, 0, 8) if vertical else (8, 0, 8, 0), sticky='nswe',
            )
            self.style.layout(f'Skin.{orientation}.TScrollbar', [
                (trough, {'sticky': 'nswe', 'children': [
                    (thumb, {'expand': 1, 'sticky': 'nswe'}),
                ]}),
            ])

    def _theme_scrollbars(self, colors):
        background = tuple(channel // 257 for channel in self.root.winfo_rgb(colors['surface']))
        for orientation, images in self.scrollbar_images.items():
            images['trough'].put(colors['surface'], to=(0, 0, images['trough'].width(), images['trough'].height()))
            self.style.configure(f'Skin.{orientation}.TScrollbar', background=colors['surface'],
                                 troughcolor=colors['surface'], borderwidth=0, relief='flat')
            for state, role, strength in (('normal', 'muted', 0.65), ('active', 'accent', 0.8), ('pressed', 'accent', 1.0)):
                foreground = tuple(channel // 257 for channel in self.root.winfo_rgb(colors[role]))
                rows = []
                for y in range(24):
                    row = []
                    for x in range(14):
                        # A six-pixel pill inside a fourteen-pixel native hit area.
                        distance = math.hypot(x + 0.5 - 7, max(6 - (y + 0.5), y + 0.5 - 18, 0))
                        alpha = max(0.0, min(1.0, 3.5 - distance)) * strength
                        rgb = tuple(round(bg + (fg - bg) * alpha) for bg, fg in zip(background, foreground))
                        row.append('#%02x%02x%02x' % rgb)
                    rows.append(row)
                if orientation == 'Horizontal':
                    rows = zip(*rows)
                images[state].put(' '.join('{' + ' '.join(row) + '}' for row in rows))

    def _table_shift_wheel(self, event):
        if event.delta:
            units = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
            self.table.xview_scroll(units, 'units')
        return 'break'

    def _menu(self):
        menu = tk.Menu(self.root)
        file = tk.Menu(menu, tearoff=False)
        file.add_command(label='Выбрать файл…', command=self._file)
        file.add_command(label='Выбрать папку…', command=self._folder)
        file.add_command(label='Сохранить отчёт…', command=self.export)
        file.add_separator()
        file.add_command(label='Выход', command=self._close)
        menu.add_cascade(label='Файл', menu=file)
        options = tk.Menu(menu, tearoff=False)
        options.add_checkbutton(label='Проверка совместимости по DLNA/сети',
                                variable=self.dlna_enabled, command=self._dlna_changed)
        options.add_separator()
        self.skin_menu = tk.Menu(options, tearoff=False)
        for skin_id, data in self.skins.items():
            self.skin_menu.add_radiobutton(label=data['name'], variable=self.skin,
                                           value=skin_id, command=lambda: self._theme(save=True))
        options.add_cascade(label='Оформление', menu=self.skin_menu)
        options.add_separator()
        options.add_command(label='Профили телевизоров…', command=self._profile_editor)
        options.add_command(label='Скорость носителя / сети…', command=self._advanced)
        options.add_command(label='Проверить зависимости…', command=self._dependency_dialog)
        options.add_separator()
        options.add_command(label='Добавить в ПКМ', command=lambda: self._shell(True))
        options.add_command(label='Убрать из ПКМ', command=lambda: self._shell(False))
        menu.add_cascade(label='Настройки', menu=options)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label='Что означает ОК?', command=self._show_about)
        menu.add_cascade(label='Справка', menu=help_menu)
        self.root.configure(menu=menu)

    def _build(self):
        self.outer = self._frame(self.root, 'bg')
        self.outer.pack(fill='both', expand=True)
        self.outer.columnconfigure(0, weight=1)
        self.outer.rowconfigure(2, weight=1)

        self.hero = self._frame(self.outer, 'bg', height=300)
        self.hero.grid(row=0, column=0, sticky='ew')
        self.hero.grid_propagate(False)
        self.art = self._label(self.hero, '', 'bg', borderwidth=0, highlightthickness=0, padx=0, pady=0)
        self.art.place(relx=1, y=0, anchor='ne')
        self.form = form = self._frame(self.hero, 'bg')
        form.place(x=24, y=16, width=646)
        self.art.lower(form)  # The 640×300 image stays behind the form where they overlap.
        form.columnconfigure(1, weight=1)
        brand = self._frame(form, 'bg')
        brand.grid(row=0, column=0, columnspan=4, sticky='ew', pady=(0, 14))
        self.logo = self._label(brand, '', 'bg')
        self.logo.pack(side='left', padx=(0, 10))
        title = self._frame(brand, 'bg')
        title.pack(side='left')
        self._label(title, 'TVCheck', 'bg', 'accent', font=('Segoe UI', 25, 'bold')).pack(anchor='w')
        self._label(title, 'Проверка совместимости видео с телевизором', 'bg', 'text', font=('Segoe UI', 9)).pack(anchor='w')
        self._label(form, 'Телевизор:', 'bg', font=('Segoe UI', 10)).grid(row=1, column=0, sticky='w', padx=(0, 12))
        self.model_box = ttk.Combobox(form, textvariable=self.model, state='readonly', width=32)
        self.model_box.grid(row=1, column=1, columnspan=2, sticky='ew')
        self.model_box.bind('<<ComboboxSelected>>', self._model_changed)
        self.profile_button = self._button(form, '⚙  Профиль', self._profile_editor, font=('Segoe UI', 9), padx=10, pady=6)
        self.profile_button.grid(row=1, column=3, padx=(8, 0), sticky='ew')
        self.model_note = self._label(form, '', 'bg', 'muted', font=('Segoe UI', 8))
        self.model_note.grid(row=2, column=1, columnspan=3, sticky='w', pady=(2, 8))
        self._label(form, 'Видео:', 'bg', font=('Segoe UI', 10)).grid(row=3, column=0, sticky='w')
        self.path_entry = ttk.Entry(form, textvariable=self.path)
        self.path_entry.grid(row=3, column=1, sticky='ew')
        self.file_button = self._button(form, 'Файл…', self._file, font=('Segoe UI', 9), padx=10, pady=6)
        self.file_button.grid(row=3, column=2, padx=(8, 0))
        self.folder_button = self._button(form, 'Папка…', self._folder, font=('Segoe UI', 9), padx=10, pady=6)
        self.folder_button.grid(row=3, column=3, padx=(8, 0), sticky='ew')
        options = self._frame(form, 'bg')
        options.grid(row=4, column=0, columnspan=4, sticky='w', pady=(10, 10))
        self.scan_options = options
        self.mode_box = ttk.Combobox(options, textvariable=self.mode, values=['USB'], state='readonly', width=11)
        self.recursive_box = ttk.Checkbutton(options, text='Вложенные папки', variable=self.recursive)
        self.full_box = ttk.Checkbutton(options, text='Битрейт · читать весь файл', variable=self.full, command=self._analysis_changed)
        self._layout_scan_options()
        action = self._frame(form, 'bg')
        action.grid(row=5, column=1, columnspan=3, sticky='w')
        self.scan_button = self._button(action, '▶  Проверить', self.scan, 'bg', 'white',
                                        compound='center', font=('Segoe UI', 12, 'bold'), padx=0, pady=0)
        self.scan_button.pack(side='left')
        self.cancel_button = self._button(action, '■  Стоп', self.cancel, font=('Segoe UI', 9), padx=14, pady=9, state='disabled')
        self.cancel_button.pack(side='left', padx=(10, 0))
        self.controls.extend((
            (self.model_box, 'readonly'), (self.profile_button, 'normal'),
            (self.path_entry, 'normal'), (self.file_button, 'normal'), (self.folder_button, 'normal'),
            (self.mode_box, 'readonly'), (self.recursive_box, 'normal'), (self.full_box, 'normal'),
        ))
        self.scan_button.bind('<Enter>', lambda _: self._button_image(True))
        self.scan_button.bind('<Leave>', lambda _: self._button_image(False))

        activity = self._frame(self.outer, 'bg', padx=24)
        activity.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        self.activity_label = self._label(activity, textvariable=self.activity, role='bg', fg='muted', font=('Segoe UI', 9), anchor='w')
        self.activity_label.pack(fill='x')
        progress_track = self._frame(activity, 'bg', height=4)
        progress_track.pack(fill='x', pady=(5, 0))
        self.progress = ttk.Progressbar(progress_track, maximum=100, style='Skin.Horizontal.TProgressbar')
        self.progress_trough = tk.PhotoImage(width=1, height=4)
        self.progress_fill = tk.PhotoImage(width=1, height=4)
        self.style.element_create('TVCheck.Progress.trough', 'image', self.progress_trough, sticky='nswe')
        self.style.element_create('TVCheck.Progress.pbar', 'image', self.progress_fill, sticky='nswe')
        self.style.layout('Skin.Horizontal.TProgressbar', [
            ('TVCheck.Progress.trough', {'sticky': 'nswe', 'children': [
                ('TVCheck.Progress.pbar', {'side': 'left', 'sticky': 'ns'}),
            ]}),
        ])
        self.progress.place(x=0, y=0, relwidth=1, height=4)

        self.card_canvas = self._track(tk.Canvas(self.outer, highlightthickness=0, height=300), 'bg')
        self.card_canvas.grid(row=2, column=0, sticky='nsew', padx=20)
        self.card_shape = self.card_canvas.create_polygon(0, 0, 1, 1, smooth=True, splinesteps=24)
        card = self._frame(self.card_canvas, 'surface')
        self.card_window = self.card_canvas.create_window(14, 12, anchor='nw', window=card)
        self.card_canvas.bind('<Configure>', self._resize_card)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        banner = self._frame(card, 'surface')
        banner.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        banner.columnconfigure(1, weight=1)
        self.health_icon = self._label(banner, '○', 'surface', 'muted', font=('Segoe UI', 27, 'bold'), width=2)
        self.health_icon.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        health = self._frame(banner, 'surface')
        health.grid(row=0, column=1, rowspan=2, sticky='ew')
        self.health_value = self._label(health, textvariable=self.summary_value, role='surface', fg='text', font=('Segoe UI', 15, 'bold'))
        self.health_value.pack(anchor='w')
        self.health_hint = self._label(health, textvariable=self.summary_hint, role='surface', fg='muted', font=('Segoe UI', 9), wraplength=590, justify='left')
        self.health_hint.pack(anchor='w')
        delivery = self._frame(banner, 'surface')
        delivery.grid(row=0, column=2, rowspan=2, padx=14)
        self.usb_label = self._label(delivery, textvariable=self.usb, role='surface', fg='muted', font=('Segoe UI', 9))
        self.usb_label.pack(anchor='e')
        self.network_label = self._label(delivery, textvariable=self.network, role='surface', fg='muted', font=('Segoe UI', 9))
        if self.dlna_enabled.get():
            self.network_label.pack(anchor='e')
        self.details_button = self._button(banner, 'Подробнее →', self._focus_details, font=('Segoe UI', 9), padx=12, pady=7, state='disabled')
        self.details_button.grid(row=0, column=3, rowspan=2)
        metrics = self._frame(card, 'surface')
        metrics.grid(row=1, column=0, sticky='ew', pady=(0, 12))
        for col, (label, variable) in enumerate((
            ('Видео', self.video_metric), ('Звук', self.audio_metric),
            ('Средний Σ · Мбит/с', self.average), ('Пик Σ / 1 с · Мбит/с', self.peak), ('Время пика', self.peak_at),
        )):
            metrics.columnconfigure(col, weight=1, uniform='metric')
            metric = self._frame(metrics, 'soft', padx=10, pady=7)
            metric.grid(row=0, column=col, sticky='nsew', padx=(0, 4))
            self._label(metric, label, 'soft', 'muted', font=('Segoe UI', 8)).pack(anchor='w')
            self._label(metric, textvariable=variable, role='soft', fg='text', font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        listing = self._frame(card, 'surface')
        listing.grid(row=2, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        columns = ('status', 'file', 'video', 'audio', 'average', 'peak', 'time')
        self.table = ttk.Treeview(listing, columns=columns, show='headings', height=6, selectmode='browse')
        for key, label, width in (
            ('status', 'Результат', 115), ('file', 'Файл', 290), ('video', 'Видео', 75),
            ('audio', 'Звук', 90), ('average', 'Средний Σ', 100), ('peak', 'Пик Σ/1с', 100), ('time', 'Время пика', 100),
        ):
            self.table.heading(key, text=label, anchor='w')
            self.table.column(key, width=width, minwidth=90 if key == 'file' else width, stretch=key == 'file')
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical = _AutoScrollbar(listing, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = _AutoScrollbar(listing, orient='horizontal', command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.bind('<<TreeviewSelect>>', self.select)
        self.table.bind('<Double-1>', lambda _: self._focus_details())
        self.table.bind('<Return>', lambda _: self._focus_details())
        self.table.bind('<Shift-MouseWheel>', self._table_shift_wheel)

        self.detail_window = tk.Toplevel(self.root)
        self.detail_window.withdraw()
        self.detail_window.title('TVCheck · Подробности проверки')
        self.detail_window.geometry('860x580')
        self.detail_window.minsize(640, 400)
        self.detail_window.protocol('WM_DELETE_WINDOW', self.detail_window.withdraw)
        notebook = ttk.Notebook(self.detail_window)
        notebook.pack(fill='both', expand=True, padx=12, pady=12)
        self.texts = {}
        for key, title in (('tracks', 'Видео и звук'), ('bitrate', 'Битрейт / доставка'), ('subtitles', 'Субтитры · справка')):
            frame = ttk.Frame(notebook, style='Surface.TFrame')
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            text = tk.Text(frame, wrap='word', relief='flat', padx=16, pady=12, font=('Segoe UI', 10), state='disabled')
            text.grid(row=0, column=0, sticky='nsew')
            bar = _AutoScrollbar(frame, command=text.yview)
            bar.grid(row=0, column=1, sticky='ns')
            text.configure(yscrollcommand=bar.set)
            notebook.add(frame, text=title)
            self.texts[key] = text
        footer = self._frame(self.outer, 'bg', padx=24, pady=10)
        footer.grid(row=3, column=0, sticky='ew')
        self._label(footer, textvariable=self.counts, role='bg', fg='muted', font=('Segoe UI', 8)).pack(side='left')
        self._label(footer, textvariable=self.dep_text, role='bg', fg='muted', font=('Segoe UI', 8)).pack(side='right')
        self._set_text('tracks', [('Выберите файл для проверки характеристик MediaInfo.', 'muted')])

    def _analysis_changed(self):
        self.scan_button.configure(text='▶  Проверить + битрейт' if self.full.get() else '▶  Проверить')

    def _button_image(self, hover=False):
        state = 'disabled' if str(self.scan_button['state']) == 'disabled' else 'hover' if hover else 'normal'
        self.scan_button.configure(image=self.button_images[state])

    def _resize_card(self, event):
        w, h, r = event.width - 1, event.height - 1, 14
        self.card_canvas.coords(self.card_shape, r, 1, w-r, 1, w, 1, w, r,
                                w, h-r, w, h, w-r, h, r, h, 1, h, 1, h-r, 1, r, 1, 1)
        self.card_canvas.itemconfigure(self.card_window, width=max(1, w-28), height=max(1, h-24))

    def _on_resize(self, event=None):
        if self.closed or (event is not None and event.widget != self.root):
            return
        width = self.root.winfo_width()
        if width < self.layout['responsive']['hideDecorativeArtBelowWidth']:
            self.hero.configure(height=286)
            self.art.place_forget()
            self.form.place_configure(width=646)
        else:
            self.hero.configure(height=300)
            self.form.place_configure(width=min(646, width - 534))
            self.art.place(relx=1, y=0, anchor='ne')
        self.health_hint.configure(wraplength=max(300, width - 510))

    def _layout_scan_options(self):
        for widget in (self.mode_box, self.recursive_box, self.full_box):
            widget.pack_forget()
        if self.dlna_enabled.get():
            self.mode_box.configure(values=list(MODES))
            self.mode_box.pack(side='left', padx=(0, 12))
            self.recursive_box.pack(side='left', padx=(0, 12))
        else:
            self.mode_box.configure(values=['USB'])
            self.recursive_box.pack(side='left')
        self.full_box.pack(side='left')

    def _dlna_changed(self):
        enabled = self.dlna_enabled.get()
        if not enabled:
            self.mode.set('USB')
            self.network_label.pack_forget()
            self.network.set('DLNA: отключено')
        else:
            self.network_label.pack(anchor='e')
            self.network.set('DLNA: —')
        self._layout_scan_options()
        self._save_settings()

    def _theme(self, save=False):
        skin = self.skin_data
        colors = skin['colors']
        good = colors.get('good', colors.get('ok', colors['accent']))
        bad = colors.get('bad', colors.get('fail', colors['accent']))
        role_colors = dict(colors, ok=good, good=good, bad=bad, fail=bad)
        self.palette = role_colors
        self.root.configure(bg=colors['bg'])
        self.detail_window.configure(bg=colors['bg'])
        self.card_canvas.itemconfigure(self.card_shape, fill=colors['surface'], outline=colors['border'])
        for role, widgets in self._tk_backgrounds.items():
            color = colors.get(role, colors['bg'])
            for widget in widgets:
                try:
                    widget.configure(bg=color)
                except tk.TclError:
                    pass
        for role, widgets in self._tk_foregrounds.items():
            color = '#ffffff' if role == 'white' else role_colors.get(role, colors['text'])
            for widget in widgets:
                try:
                    widget.configure(fg=color)
                except tk.TclError:
                    pass
        self.style.configure('.', background=colors['bg'], foreground=colors['text'], font=('Segoe UI', 10))
        self.style.configure('TFrame', background=colors['bg'])
        for widget_style in ('TEntry', 'TCombobox', 'Treeview', 'Treeview.Heading'):
            self.style.configure(widget_style, bordercolor=colors['border'], lightcolor=colors['surface'], darkcolor=colors['surface'])
        self.style.configure('Surface.TFrame', background=colors['surface'])
        self.style.configure('TLabel', background=colors['bg'], foreground=colors['text'])
        self.style.configure('TButton', background=colors['surface'], foreground=colors['text'], padding=(10, 6), borderwidth=0)
        self.style.map('TButton', background=[('active', colors['accent2']), ('disabled', colors['bg'])], foreground=[('disabled', colors['muted'])])
        self.style.configure('TEntry', fieldbackground=colors['surface'], foreground=colors['text'], insertcolor=colors['text'], padding=5)
        self.style.configure('TCheckbutton', background=colors['bg'], foreground=colors['text'], indicatorbackground=colors['surface'])
        self.style.map('TCheckbutton', background=[('active', colors['bg'])],
                       foreground=[('disabled', colors['muted'])])
        self.style.configure('TCombobox', background=colors['surface'], foreground=colors['text'], arrowcolor=colors['accent'], padding=4)
        self.style.map('TCombobox', fieldbackground=[('readonly', colors['surface'])], foreground=[('readonly', colors['text'])], selectbackground=[('readonly', colors['surface'])], selectforeground=[('readonly', colors['text'])])
        self.root.option_add('*TCombobox*Listbox.background', colors['surface'])
        self.root.option_add('*TCombobox*Listbox.foreground', colors['text'])
        self.style.configure('Treeview', background=colors['surface'], fieldbackground=colors['surface'], foreground=colors['text'],
                             rowheight=skin['sizes']['tableRow'], font=('Segoe UI', 10), borderwidth=0)
        self.style.configure('Treeview.Heading', background=colors['soft'], foreground=colors['text'],
                             padding=(8, 9), font=('Segoe UI', 9, 'bold'), borderwidth=0)
        self.style.map('Treeview', background=[('selected', colors['selection'])], foreground=[('selected', colors['text'])])
        self.style.map('Treeview.Heading', background=[('active', colors['selection'])])
        self._theme_scrollbars(colors)
        self.style.configure('TNotebook', background=colors['bg'], borderwidth=0)
        self.style.configure('TNotebook.Tab', background=colors.get('soft', colors['surface']), foreground=colors['text'], padding=(10, 4))
        self.style.map('TNotebook.Tab', background=[('selected', colors['surface'])])
        self.style.configure('Skin.Horizontal.TProgressbar', background=colors['accent'], troughcolor=colors['soft'],
                             borderwidth=0, relief='flat', thickness=3, bordercolor=colors['soft'],
                             lightcolor=colors['accent'], darkcolor=colors['accent'])
        self.progress_trough.put(colors['soft'], to=(0, 0, 1, 4))
        self.progress_fill.put(colors['accent'], to=(0, 0, 1, 4))
        for key in ('ok', 'warn', 'fail', 'error'):
            self.table.tag_configure(key, foreground=bad if key in {'fail', 'error'} else good if key == 'ok' else colors.get(key, colors['text']))
        for text in self.texts.values():
            text.configure(bg=colors['surface'], fg=colors['text'], insertbackground=colors['text'], selectbackground=colors.get('soft', colors['surface']))
            for key, color in (('ok', good), ('warn', colors['warn']), ('fail', bad), ('error', bad), ('info', colors['muted']), ('muted', colors['muted']), ('heading', colors['accent'])):
                text.tag_configure(key, foreground=color)
            text.tag_configure('heading', font=('Segoe UI', 9, 'bold'))
        self.scan_button.configure(bg=colors['bg'], activebackground=colors['bg'], activeforeground='#ffffff',
                                   disabledforeground=colors['muted'])
        self.cancel_button.configure(activebackground=colors['selection'])
        self.details_button.configure(activebackground=colors['selection'])
        self.art_image = tk.PhotoImage(file=str(skin['art']))
        self.art.configure(image=self.art_image)
        self.logo_image = tk.PhotoImage(file=str(skin['folder'] / 'logo.png'))
        self.logo.configure(image=self.logo_image)
        self.button_images = {state: tk.PhotoImage(file=str(skin['folder'] / f'primary_{state}.png'))
                              for state in ('normal', 'hover', 'disabled')}
        self._button_image()
        if self.table.selection():
            self.select()
        if save:
            self._save_settings()

    def _show_about(self):
        messagebox.showinfo('О программе', DISCLAIMER + '\n\nИсходные файлы не изменяются.\n\n' + '\n'.join(self.profiles.get(self.profile_id, {}).get('sources', [])), parent=self.root)

    def _save_settings(self):
        try:
            tv_profiles.save_settings({
                'skin': self.skin.get(),
                'profile_id': self.profile_id,
                'dlna_enabled': self.dlna_enabled.get(),
            })
        except OSError as exc:
            self.activity.set(f'Настройки не сохранены: {exc}')

    def _refresh_profiles(self):
        self.model_names = {f"{p['name']} [{key}]": key for key, p in self.profiles.items()}
        self.model_box.configure(values=list(self.model_names))
        self.model.set(next((name for name, key in self.model_names.items() if key == self.profile_id), ''))
        profile = self.profiles.get(self.profile_id, {})
        self.model_note.configure(text=f"ПО: {profile.get('firmware') or 'не указано'}  ·  {'профиль по документации' if profile.get('verified') else 'непроверенный пользовательский профиль'}")

    def _model_changed(self, _=None):
        if self.model.get() not in self.model_names:
            return
        self.profile_id = self.model_names[self.model.get()]
        self._refresh_profiles()
        self._save_settings()
        if self.reports:
            self.activity.set('Профиль изменён. Старые результаты сохранены для прежнего ТВ — запустите новую проверку.')

    def _file(self):
        if self.running:
            return
        value = filedialog.askopenfilename(parent=self.root, title='Выберите видео', filetypes=[('Видео', '*.mkv *.mp4 *.avi *.ts *.m2ts *.mov *.webm'), ('Все файлы', '*.*')])
        if value:
            self.path.set(value)

    def _folder(self):
        if self.running:
            return
        value = filedialog.askdirectory(parent=self.root, title='Папка с видео')
        if value:
            self.path.set(value)

    def _check_dependencies(self):
        def check():
            try:
                dependencies = backend.check_dependencies(self.mediainfo)
            except Exception as exc:
                dependencies = {
                    'MediaInfo': {'ok': False, 'error': f'Проверка зависимости завершилась ошибкой: {exc}'},
                    'FFprobe': {'ok': False, 'error': 'Проверка не выполнена из-за ошибки MediaInfo.'},
                }
            self.events.put(('deps', dependencies))
        threading.Thread(target=check, daemon=True).start()

    def _dependency_dialog(self):
        text = '\n\n'.join(f"{name}: {'ОК' if value['ok'] else 'ОШИБКА'}\n{value.get('path', '')}\n{value.get('version') or value.get('error')}" for name, value in self.dependencies.items())
        messagebox.showinfo('Встроенные анализаторы', text or 'Проверка ещё выполняется.', parent=self.root)

    def scan(self):
        if self.running or self.closed:
            return
        path = self.path.get().strip().strip('"')
        if not path:
            messagebox.showinfo('Путь', 'Выберите файл или папку.', parent=self.root)
            return
        try:
            link = float(self.link.get().replace(',', '.')) if self.link.get().strip() else None
            if link is not None and (not math.isfinite(link) or link <= 0):
                raise ValueError()
        except ValueError:
            messagebox.showerror('Скорость', 'Укажите положительное число Мбит/с или оставьте пустым.', parent=self.root)
            return
        profile = self.profiles.get(self.profile_id)
        if not profile:
            messagebox.showerror('Профиль ТВ', 'Профиль телевизора не загружен. Откройте «Настройки → Профили телевизоров».', parent=self.root)
            return
        self.snapshot = dict(path=os.path.abspath(os.path.expandvars(os.path.expanduser(path))), mode=MODES[self.mode.get()],
                             recursive=self.recursive.get(), full=self.full.get(), profile_id=self.profile_id,
                             profile=profile, link_mbps=link, mediainfo=self.mediainfo,
                             started_at=datetime.now().astimezone().isoformat(), partial=False)
        self.reports = []
        self.table.delete(*self.table.get_children())
        self.counts.set('Файлов: 0')
        for value in (self.average, self.peak, self.peak_at, self.video_metric, self.audio_metric):
            value.set('—')
        self.summary_value.set('Проверка…')
        self.summary_hint.set('MediaInfo проверяет характеристики; битрейт измеряется только при включённой дополнительной опции.')
        self.health_icon.configure(text='…')
        self.usb.set('USB: —')
        self.network.set('DLNA: —')
        for key in self.texts:
            self._set_text(key, [('Выполняется проверка…', 'muted')])
        self.stop = threading.Event()
        self.running = True
        for widget, _ in self.controls:
            widget.configure(state='disabled')
        self.scan_button.configure(state='disabled')
        self._button_image()
        self.cancel_button.configure(state='normal')
        self.details_button.configure(state='disabled')
        self.progress.configure(value=0)
        snapshot = dict(self.snapshot)
        stop = self.stop

        def work():
            failed = False
            try:
                for report in backend.iter_reports(snapshot['path'], snapshot['recursive'], snapshot['mode'], snapshot['mediainfo'], stop,
                                                   snapshot['profile_id'], snapshot['full'], snapshot['link_mbps'],
                                                   lambda filename, stage, percent: self.events.put(('progress', (filename, stage, percent)))):
                    self.events.put(('report', report))
            except Exception as exc:
                failed = True
                self.events.put(('report', backend.error_report(snapshot['path'], str(exc))))
            finally:
                self.events.put(('done', {'partial': stop.is_set() or failed, 'cancelled': stop.is_set()}))
        threading.Thread(target=work, daemon=True, name='TVCheck-scan').start()

    def _poll(self):
        if self.closed:
            return
        for _ in range(80):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'deps':
                self.dependencies = value
                self.ready = bool(self.profiles)
                dependencies_ok = all(entry.get('ok') for entry in value.values())
                self.dep_text.set('MediaInfo + FFprobe: ОК' if dependencies_ok else 'Проблема зависимостей / профиля')
                self.activity.set('Выберите видео или папку.' if dependencies_ok else 'Зависимость не прошла проверку; запуск покажет точную ошибку в таблице.')
                self.scan_button.configure(state='normal' if self.ready and not self.running else 'disabled')
                self._button_image()
                if self.initial_path and self.ready and not self.running:
                    self.initial_path = ''
                    self.scan()
            elif kind == 'progress':
                filename, stage, percent = value
                self.activity.set(f"{stage}: {Path(filename).name}" + (f' · {percent:.0f}%' if percent is not None else ''))
                self.progress.configure(value=percent or 0)
            elif kind == 'report':
                self._add(value)
            elif kind == 'done':
                self._finish(value)
        self.root.after(60, self._poll)

    def _add(self, report):
        index = str(len(self.reports))
        self.reports.append(report)
        origin = self.snapshot['path']
        origin = origin if os.path.isdir(origin) else os.path.dirname(origin)
        try:
            name = os.path.relpath(report['path'], origin)
        except ValueError:
            name = report['path']
        rate = report.get('bitrate', {})
        video = ', '.join(t.get('codec', '?') for t in report['video']) or '—'
        audio = ', '.join(dict.fromkeys(t.get('codec', '?') for t in report['audio'])) or '—'
        peak_time = timestamp(rate['peak_second']) if 'peak_second' in rate else '—'
        self.table.insert('', 'end', iid=index, values=(
            STATUS[report['status']], name, video, audio, _mbps(rate.get('average_mbps')),
            _mbps(rate.get('peak_mbps')), peak_time,
        ), tags=(report['status'],))
        counts = Counter(r['status'] for r in self.reports)
        self.counts.set(f"Файлов: {len(self.reports)}  |  " + '  ·  '.join(f'{STATUS[k]}: {counts[k]}' for k in ('ok', 'warn', 'fail', 'error')))
        if not self.table.selection():
            self.table.selection_set(index)
            self.select()

    def cancel(self):
        if self.running:
            self.stop.set()
            self.cancel_button.configure(state='disabled')
            self.activity.set('Остановка измерения… Заголовки MediaInfo/FFprobe могут потребовать до 45 с. Отчёт будет неполным.')

    def _finish(self, result):
        self.running = False
        self.snapshot.update(result, finished_at=datetime.now().astimezone().isoformat())
        for widget, state in self.controls:
            widget.configure(state=state)
        self.scan_button.configure(state='normal' if self.profiles else 'disabled')
        self._button_image()
        self.cancel_button.configure(state='disabled')
        self.details_button.configure(state='normal' if self.reports else 'disabled')
        self.progress.configure(value=0 if result['partial'] else 100)
        self.activity.set('Остановлено — неполный отчёт.' if result['partial'] else f"Готово · {self.snapshot['profile']['name']} · {self.snapshot['mode'].upper()} · {len(self.reports)} файлов.")
        if not self.reports:
            self.activity.set('Отменено; результатов нет.' if result['partial'] else 'Видеофайлы не найдены. Проверьте путь и вложенные папки.')
            self.summary_value.set('Нет результатов')
            self.summary_hint.set(self.activity.get())
            self._set_text('tracks', [(self.activity.get(), 'muted')])

    def _set_text(self, key, lines):
        text = self.texts[key]
        text.configure(state='normal')
        text.delete('1.0', 'end')
        for value, tag in lines:
            text.insert('end', value + '\n', tag)
        text.configure(state='disabled')
        text.yview_moveto(0)

    def _focus_details(self):
        if not self.reports:
            return
        if not self.table.selection():
            self.table.selection_set('0')
        self.select()
        self.detail_window.deiconify()
        self.detail_window.lift()
        self.texts['tracks'].focus_set()

    def select(self, _=None):
        selection = self.table.selection()
        if not selection:
            return
        report = self.reports[int(selection[0])]
        self.summary_value.set(STATUS[report['status']])
        self.summary_hint.set(report['summary'])
        status = report['status']
        color_key = 'bad' if status == 'error' else status
        self.health_value.configure(fg=self.palette.get(color_key, self.palette['text']))
        self.health_icon.configure(text=STATUS_ICON.get(status, '!'), fg=self.palette.get(color_key, self.palette['text']))
        for mode, variable, label in (('usb', self.usb, self.usb_label), ('network', self.network, self.network_label)):
            playback = report.get('playback', {}).get(mode, {})
            playback_status = playback.get('status')
            variable.set(f"{'USB' if mode == 'usb' else 'DLNA'}: {STATUS[playback_status] if playback_status else '—'}")
            label.configure(fg=self.palette.get('bad' if playback_status == 'error' else playback_status, self.palette['muted']))
        video_codecs = ', '.join(dict.fromkeys(t.get('codec', '?') for t in report['video'])) or '—'
        audio_codecs = ', '.join(dict.fromkeys(t.get('codec', '?') for t in report['audio'])) or '—'
        self.video_metric.set(video_codecs)
        self.audio_metric.set(audio_codecs)
        rate = report.get('bitrate', {})
        self.average.set(_mbps(rate.get('average_mbps')))
        self.peak.set(_mbps(rate.get('peak_mbps')))
        self.peak_at.set(timestamp(rate['peak_second']) if 'peak_second' in rate else '—')
        lines = [(report['summary'], report['status']), (f"ТВ: {self.snapshot['profile']['name']} · {self.snapshot['mode'].upper()} · {report['container']}", 'muted'), (report['path'], 'muted')]
        lines += [('Справка: ' + issue, 'info') for issue in report.get('technical_notes', [])]
        for note in report.get('technical_track_notes', []):
            lines.append((f"Справка · {note['label']}", 'info'))
            lines += [('  ' + issue, 'info') for issue in note.get('issues', [])]
        for kind, title in (('video', 'ВИДЕО'), ('audio', 'ЗВУК')):
            lines.append(('\n' + title, 'heading'))
            for track in report[kind]:
                lines += [(f"[{STATUS[track['status']]}] {track['label']}", track['status']), (track['details'], '')]
                lines += [('  ' + issue, '') for issue in track['issues']]
        self._set_text('tracks', lines)
        lines = [('ИЗМЕРЕНИЕ ПО ВСЕМУ ФАЙЛУ', 'heading'), ('Полное: ' + ('да' if rate.get('complete') else 'нет — битрейт не измерялся'), '')]
        for track in report['video']:
            measured = track.get('measured_bitrate', {})
            lines.append((f"{track['label']}: средний {_mbps(measured.get('average_mbps'))}; пик {_mbps(measured.get('peak_mbps'))} Мбит/с на {timestamp(measured.get('peak_second', 0))}; лимит ТВ {_mbps(track.get('limit_mbps'))} Мбит/с.", ''))
        lines += [(issue, '') for issue in rate.get('issues', [])]
        lines += [('Справка FFprobe: ' + issue, 'info') for issue in rate.get('diagnostics', [])]
        playback_modes = ('usb', 'network') if self.dlna_enabled.get() else ('usb',)
        lines.append(('\nUSB И DLNA ОЦЕНИВАЮТСЯ ОТДЕЛЬНО' if self.dlna_enabled.get() else '\nUSB', 'heading'))
        for mode in playback_modes:
            value = report.get('playback', {}).get(mode, {})
            if not value:
                continue
            lines.append((f"{mode.upper()}: {STATUS[value['status']]} — {value['summary']}", value['status']))
            lines += [('  ' + issue, '') for issue in value['issues']]
        lines.append(('\n' + DISCLAIMER, 'muted'))
        self._set_text('bitrate', lines)
        lines = [('Субтитры не меняют статус совместимости видео и звука.', 'info')]
        for track in report['subtitles']:
            lines += [('\n' + track['label'], 'heading'), (track['details'], 'info')]
            lines += [(issue, 'info') for issue in track['issues']]
        if not report['subtitles']:
            lines.append(('Встроенных субтитров нет.', 'info'))
        self._set_text('subtitles', lines)

    def export(self):
        if self.running or not self.snapshot:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title='Сохранить отчёт', initialfile='TVCheck-report.json', defaultextension='.json', filetypes=[('JSON', '*.json'), ('Текст', '*.txt')])
        if not destination:
            return
        try:
            content = text_report(self.snapshot, self.reports) if destination.lower().endswith('.txt') else json.dumps(dict(scan=self.snapshot, results=self.reports, disclaimer=DISCLAIMER), ensure_ascii=False, indent=2)
            Path(destination).write_text(content, encoding='utf-8')
        except (OSError, ValueError) as exc:
            messagebox.showerror('Отчёт не сохранён', str(exc), parent=self.root)

    def _shell(self, install):
        try:
            result = context_menu.install() if install else context_menu.uninstall()
            messagebox.showinfo('Контекстное меню', result, parent=self.root)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror('Контекстное меню', str(exc), parent=self.root)

    def _advanced(self):
        if self.running:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Скорость доставки')
        dialog.geometry('550x260')
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Устойчивая скорость USB / сети, Мбит/с (необязательно)').pack(anchor='w')
        value = tk.StringVar(value=self.link.get())
        ttk.Entry(frame, textvariable=value).pack(fill='x', pady=8)
        ttk.Label(frame, text='Введите измеренную устойчивую скорость, не цифру на коробке.\nПусто = неизвестно. 1 МБ/с = 8 Мбит/с.\nПрограмма не измеряет соединение с телевизором.', style='Muted.TLabel').pack(anchor='w')

        def save():
            try:
                number = float(value.get().replace(',', '.')) if value.get().strip() else None
                if number is not None and (not math.isfinite(number) or number <= 0):
                    raise ValueError()
            except ValueError:
                messagebox.showerror('Скорость', 'Нужно положительное число или пустое поле.', parent=dialog)
                return
            self.link.set(value.get())
            dialog.destroy()
        ttk.Button(frame, text='Применить', command=save).pack(anchor='e', pady=12)

    def _profile_editor(self):
        if self.running:
            return
        if not self.profile_id or self.profile_id not in self.profiles:
            messagebox.showerror('Профиль', 'Нет доступного профиля телевизора.', parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Новый профиль телевизора · JSON')
        dialog.geometry('780x670')
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Профиль — реальные ограничения конкретного ТВ из его руководства.\nШаблон скопирован с выбранного ТВ: измените ограничения, id, name и sources.\nverified=false оставляет жёлтый статус, пока данные не подтверждены.', wraplength=730).pack(anchor='w', pady=(0, 10))
        editor = tk.Text(frame, wrap='none', font=('Consolas', 10), undo=True)
        editor.pack(fill='both', expand=True)
        profile = dict(self.profiles[self.profile_id])
        profile.update(id='my-tv', name='Мой телевизор', firmware='', verified=False, sources=[])
        editor.insert('1.0', json.dumps(profile, ensure_ascii=False, indent=2))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(10, 0))

        def import_json():
            source = filedialog.askopenfilename(parent=dialog, filetypes=[('Профиль JSON', '*.json')])
            if not source:
                return
            try:
                profile = tv_profiles.validate(json.loads(Path(source).read_text(encoding='utf-8-sig')))
                editor.delete('1.0', 'end')
                editor.insert('1.0', json.dumps(profile, ensure_ascii=False, indent=2))
            except (OSError, ValueError, TypeError) as exc:
                messagebox.showerror('Профиль', str(exc), parent=dialog)

        def save():
            try:
                profile = tv_profiles.save_profile(json.loads(editor.get('1.0', 'end')))
            except (OSError, ValueError, TypeError) as exc:
                messagebox.showerror('Профиль', str(exc), parent=dialog)
                return
            self.profiles, self.profile_errors = tv_profiles.load_profiles()
            self.profile_id = profile['id']
            self._refresh_profiles()
            self._save_settings()
            self.activity.set('Новый профиль выбран. Запустите проверку заново.')
            dialog.destroy()
        ttk.Button(buttons, text='Импорт JSON…', command=import_json).pack(side='left')
        ttk.Button(buttons, text='Сохранить и выбрать', command=save).pack(side='right')
        dialog.protocol('WM_DELETE_WINDOW', dialog.destroy)

    def _close(self):
        self.closed = True
        self.stop.set()
        self.root.destroy()


def run(initial_path='', mediainfo=''):
    root = tk.Tk()
    Application(root, initial_path, mediainfo)
    root.mainloop()
