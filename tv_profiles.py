"""Versioned, editable TV capability profiles. User files override shipped IDs."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import sys

DEFAULT_ID = 'lg-uk6300'


def resource_dir() -> Path:
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def user_dir() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TVCheck'


def validate(profile: dict) -> dict:
    if not isinstance(profile, dict) or profile.get('schema_version') != 1:
        raise ValueError('Профиль должен быть JSON-объектом с schema_version: 1.')
    for key in ('id', 'name', 'firmware'):
        if not isinstance(profile.get(key), str) or (key != 'firmware' and not profile[key].strip()):
            raise ValueError(f'Поле {key}: требуется строка.')
    if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,79}', profile['id']):
        raise ValueError('id: только латинские строчные буквы, цифры, дефис и подчёркивание (до 80 символов).')
    if not isinstance(profile.get('verified'), bool):
        raise ValueError('verified должно быть true или false.')
    for key in ('sources', 'unsupported_video', 'unsupported_audio', 'hdr_formats', 'uhd_containers', 'uhd_video', 'uhd_audio'):
        values = profile.get(key)
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f'{key}: требуется список непустых строк.')
    if profile['verified'] and not profile['sources']:
        raise ValueError('Для verified=true укажите sources — ссылки на документацию модели.')
    containers = profile.get('containers')
    if not isinstance(containers, dict) or not containers:
        raise ValueError('containers: нужна таблица контейнеров с video/audio.')
    for name, pair in containers.items():
        if not isinstance(name, str) or not isinstance(pair, dict):
            raise ValueError('Некорректная запись containers.')
        for kind in ('video', 'audio'):
            if not isinstance(pair.get(kind), list) or any(not isinstance(item, str) for item in pair[kind]):
                raise ValueError(f'containers.{name}.{kind}: требуется список кодеков.')
    rules = profile.get('video_rules')
    if not isinstance(rules, dict) or not rules:
        raise ValueError('video_rules: нужна таблица ограничений видеокодеков.')
    for codec, rule in rules.items():
        if not isinstance(rule, dict):
            raise ValueError(f'video_rules.{codec}: требуется объект.')
        for key in ('profiles', 'chroma'):
            if not isinstance(rule.get(key), list) or any(not isinstance(item, str) for item in rule[key]):
                raise ValueError(f'{codec}.{key}: требуется список строк (пустой = неизвестно).')
        if not isinstance(rule.get('bit_depths'), list) or any(type(x) is not int or x <= 0 for x in rule['bit_depths']):
            raise ValueError(f'{codec}.bit_depths: требуется список положительных целых чисел.')
        for key in ('max_width', 'max_height', 'max_fps', 'max_level_fhd', 'max_level_uhd', 'max_mbps_fhd', 'max_mbps_uhd'):
            number = rule.get(key)
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0):
                raise ValueError(f'{codec}.{key}: положительное число или null (неизвестно).')
    options = profile.get('options')
    if not isinstance(options, dict):
        raise ValueError('options: требуется объект.')
    for key in ('dts_network', 'aac_main', 'gmc_qpel', 'dolby_vision', 'width_4096_uncertain'):
        if not isinstance(options.get(key), bool):
            raise ValueError(f'options.{key}: требуется true/false.')
    return copy.deepcopy(profile)


def load_profiles() -> tuple[dict, list[str]]:
    result, errors = {}, []
    directories = [resource_dir() / 'profiles', user_dir() / 'profiles']
    for folder in directories:
        if not folder.exists():
            continue
        for path in sorted(folder.glob('*.json')):
            try:
                profile = validate(json.loads(path.read_text(encoding='utf-8-sig')))
                result[profile['id']] = profile
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f'{path.name}: {exc}')
    return result, errors


def get_profile(profile_id: str = DEFAULT_ID) -> dict:
    profiles, errors = load_profiles()
    if profile_id not in profiles:
        raise ValueError(f'Профиль {profile_id} не найден. ' + '; '.join(errors))
    return profiles[profile_id]


def save_profile(profile: dict) -> dict:
    profile = validate(profile)
    if profile['id'] == DEFAULT_ID:
        raise ValueError('Для нового/изменённого профиля задайте другой id; заводской LG-профиль защищён.')
    folder = user_dir() / 'profiles'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (profile['id'] + '.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    return profile


def load_settings() -> dict:
    try:
        data = json.loads((user_dir() / 'settings.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'settings.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
