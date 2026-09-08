"""Pure MediaInfo-based checks against the LG webOS 2018 guide and UK6300 specs."""

import math
import re
from pathlib import PureWindowsPath

DISCLAIMER = (
    "ОК = проверенные параметры соответствуют выбранному профилю, не гарантия воспроизведения. "
    "Быстрая проверка использует характеристики MediaInfo. Дополнительная полная проверка читает "
    "пакеты всего файла и измеряет пики за фиксированные интервалы 1 с; это не тест декодирования "
    "и не измерение скорости флешки/сети. Короткие всплески внутри секунды, повреждения кадров и "
    "особенности прошивки могут остаться незамеченными. DLNA дополнительно зависит от сервера, "
    "MIME-профиля и транскодирования. Для ПО 05.50.70 отдельная таблица кодеков в источниках LG "
    "не опубликована."
)

EXTENSIONS = {
    "asf": "asf", "wmv": "asf", "avi": "avi", "mp4": "mp4", "m4v": "mp4",
    "mov": "mp4", "3gp": "3gp", "3g2": "3gp", "mkv": "mkv", "ts": "ts",
    "trp": "ts", "tp": "ts", "mts": "ts", "m2ts": "ts", "mpg": "mpg",
    "mpeg": "mpg", "dat": "mpg", "vob": "vob", "rm": "rm", "rmvb": "rm",
    "webm": "webm", "ogg": "ogg", "ogv": "ogg", "flv": "flv",
}
RANK = {"ok": 0, "warn": 1, "fail": 2, "error": 3}
AUDIO_ADVICE = "Выбрать совместимую аудиодорожку или перекодировать только звук в AAC LC / AC-3; видео можно копировать без перекодирования."


def _text(track, key):
    value = track.get(key, "")
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _joined(track, *keys):
    return " / ".join(filter(None, (_text(track, key) for key in keys)))


def _number(value):
    """Read raw SI values; tolerate familiar human-readable MediaInfo units, not junk."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip().replace("\u00a0", " ")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:\s*/\s*([0-9]+(?:\.[0-9]+)?))?\s*(kbit/s|kb/s|mbit/s|mb/s|bit/s|b/s|fps|pixels?|bits?|hz|khz)?", text, re.I)
    if not match:
        return None
    value = float(match[1])
    if match[2]:
        denominator = float(match[2])
        if not denominator:
            return None
        value /= denominator
    unit = (match[3] or "").lower()
    value *= 1000000 if unit in {"mbit/s", "mb/s"} else 1000 if unit in {"kbit/s", "kb/s", "khz"} else 1
    return value if math.isfinite(value) and value > 0 else None


def _label(track, kind, index):
    fields = [f"{kind} {index}"]
    for key in ("Language", "Title"):
        value = _text(track, key)
        if value:
            fields.append(value)
    default = _text(track, "Default").lower()
    if default in {"yes", "1", "true"}:
        fields.append("по умолчанию")
    if _text(track, "Forced").lower() in {"yes", "1", "true"}:
        fields.append("принудительная")
    return " | ".join(fields)


def _result(track, kind, index):
    return {"label": _label(track, kind, index), "status": "ok", "details": "", "issues": []}


def _issue(result, status, message):
    if RANK[status] > RANK[result["status"]]:
        result["status"] = status
    result["issues"].append(message)


def _container(general, path):
    fmt = _text(general, "Format").lower()
    ext = PureWindowsPath(path).suffix.lower().lstrip(".")
    if fmt == "matroska":
        return "mkv", ext
    if fmt in {"mpeg-4", "quicktime"}:
        return ("3gp" if ext in {"3gp", "3g2"} else "mp4"), ext
    if fmt in {"mpeg-ts", "mpeg-bdAV".lower()}:
        return "ts", ext
    if fmt in {"mpeg-ps", "mpeg video"}:
        return ("vob" if ext == "vob" else "mpg"), ext
    if fmt in {"avi", "asf", "webm", "ogg"}:
        return fmt, ext
    if fmt in {"realmedia", "real media"}:
        return "rm", ext
    if fmt == "flash video":
        return "flv", ext
    return "", ext


def _video_codec(track):
    fmt = _text(track, "Format").upper()
    code = _text(track, "CodecID").upper()
    if fmt in {"AVC", "H.264", "H264"}:
        return "AVC"
    if fmt in {"HEVC", "H.265", "H265"}:
        return "HEVC"
    if fmt in {"MPEG-4 VISUAL", "MPEG-4", "XVID", "DIVX"}:
        return "MPEG-4"
    if fmt == "MPEG VIDEO":
        version = _text(track, "Format_Version").lower()
        return "MPEG-2" if "2" in version else "MPEG-1" if "1" in version else "MPEG Video (версия неизвестна)"
    if fmt in {"JPEG", "MJPEG", "MOTION JPEG"}:
        return "MJPEG"
    if fmt in {"VC-1", "WMV", "VP8", "VP9", "AV1"}:
        return fmt
    if fmt in {"REALVIDEO", "REAL VIDEO"} and code in {"RV30", "RV40"}:
        return code
    return _text(track, "Format") or "неизвестный кодек"


def _audio_codec(track):
    fmt = _text(track, "Format").upper()
    combined = _joined(track, "Format", "Format_Commercial_IfAny", "Format_AdditionalFeatures", "CodecID").upper()
    if "TRUEHD" in combined or "TRUE HD" in combined or fmt == "MLP FBA":
        return "TrueHD"
    if fmt.startswith("E-AC-3") or fmt == "EAC3":
        return "E-AC-3"
    if fmt in {"AC-3", "AC3"}:
        return "AC-3"
    if fmt.startswith("AAC") or fmt.startswith("HE-AAC"):
        return "AAC"
    if fmt.startswith("DTS"):
        return "DTS"
    if fmt == "MPEG AUDIO":
        profile = _text(track, "Format_Profile").upper()
        return {"LAYER 1": "MP1", "LAYER 2": "MP2", "LAYER 3": "MP3", "LAYER I": "MP1", "LAYER II": "MP2", "LAYER III": "MP3"}.get(profile, "MPEG Audio (слой неизвестен)")
    if fmt in {"PCM", "LPCM", "DVD-LPCM"}:
        return "PCM"
    if "ADPCM" in fmt:
        return "ADPCM"
    if fmt.startswith("WMA"):
        return "WMA"
    if fmt == "AMR":
        return "AMR-WB" if "WB" in combined or "WIDE BAND" in combined else "AMR-NB" if "NB" in combined or "NARROW BAND" in combined else "AMR (профиль неизвестен)"
    if fmt == "COOK":
        return "Cook"
    return _text(track, "Format") or "неизвестный кодек"


def _pair(result, codec, container, tv, audio=False):
    pairs = tv["containers"]
    if container in pairs and codec not in pairs[container]["audio" if audio else "video"]:
        _issue(result, "warn", f"Связка {container.upper()} + {codec} не указана в профиле ТВ. Если кодек совместим, выполнить remux в поддерживаемый контейнер; смены расширения недостаточно.")


def _bitrate(track, result):
    maxima = []
    for key in ("BitRate_Maximum", "BitRate_Nominal"):
        # Nominal is not a ceiling; it is only a fallback below.
        if key == "BitRate_Nominal":
            continue
        raw = _text(track, key)
        value = _number(raw)
        if raw and value is None:
            _issue(result, "warn", "Максимальный битрейт записан некорректно; пики неизвестны.")
        if value:
            maxima.append(value)
    settings = _joined(track, "Encoded_Library_Settings", "Format_Settings")
    for match in re.finditer(r"\bvbv[-_]maxrate\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)", settings, re.I):
        value = _number(match[1])
        if value:
            maxima.append(value * 1000)
    average = _number(track.get("BitRate")) or _number(track.get("BitRate_Nominal"))
    if maxima:
        return max(maxima + ([average] if average else [])), "макс./VBV"
    return average, "средний/номинальный"


def _level(track):
    raw = _text(track, "Format_Level")
    if not raw:
        match = re.search(r"@L([0-9]+(?:\.[0-9]+)?)", _text(track, "Format_Profile"), re.I)
        raw = match[1] if match else ""
    level = _number(raw.removeprefix("L"))
    if level and level >= 10 and level.is_integer():
        level /= 10
    return level


def _video(track, index, container, tv):
    result = _result(track, "Видео", index)
    codec = _video_codec(track)
    width, height = _number(track.get("Width")), _number(track.get("Height"))
    uhd = bool((width and width > 1920) or (height and height > 1080))
    fps = _number(track.get("FrameRate"))
    fps_max = _number(track.get("FrameRate_Maximum"))
    if fps_max:
        fps = max(fps or 0, fps_max)
    if not fps:
        numerator = _number(track.get("FrameRate_Num"))
        denominator = _number(track.get("FrameRate_Den"))
        fps = numerator / denominator if numerator and denominator else None
    depth = _number(track.get("BitDepth"))
    chroma = _text(track, "ChromaSubsampling")
    profile = _text(track, "Format_Profile")
    level = _level(track)
    bitrate, rate_kind = _bitrate(track, result)
    details = [codec, profile or "профиль ?", f"{width:g}×{height:g}" if width and height else "размер ?", f"{fps:g} кадр/с" if fps else "частота ?", f"{depth:g} бит" if depth else "глубина ?", chroma or "цветность ?", f"{bitrate / 1000000:g} Мбит/с ({rate_kind})" if bitrate else "битрейт ?"]
    result["details"] = "; ".join(details)
    _pair(result, codec, container, tv)
    rule = tv["video_rules"].get(codec, {})
    result["codec"] = codec
    result["limit_mbps"] = rule.get("max_mbps_uhd" if uhd else "max_mbps_fhd")
    if codec.casefold() in {name.casefold() for name in tv["unsupported_video"]}:
        _issue(result, "fail", f"{codec} не поддерживается выбранным ТВ. Требуется перекодирование видео в кодек из профиля.")
    elif codec not in {name for pair in tv["containers"].values() for name in pair["video"]}:
        _issue(result, "warn", f"Поддержка видеокодека {codec} не документирована в выбранном профиле.")
    for value, name in ((width, "ширина"), (height, "высота"), (fps, "частота кадров"), (bitrate, "битрейт")):
        if not value:
            _issue(result, "warn", f"Неизвестна/некорректна {name}; полностью проверить ограничения нельзя.")
    for actual, key, name in ((width, "max_width", "Ширина"), (height, "max_height", "Высота"), (fps, "max_fps", "Частота кадров")):
        limit = rule.get(key)
        if not limit:
            _issue(result, "warn", f"{name}: в профиле нет численного предела.")
        if actual and limit and actual > limit + 0.001:
            uncertain = key == "max_width" and actual <= 4096 and tv["options"]["width_4096_uncertain"]
            _issue(result, "warn" if uncertain else "fail",
                   f"{name} {actual:g} выше предела профиля {limit:g}." +
                   (" Руководство также упоминает 4096, но не в численной таблице; нужна проба на ТВ." if uncertain else " Уменьшить параметр при перекодировании."))
    if _text(track, "FrameRate_Mode").upper() == "VFR" and not fps_max:
        _issue(result, "warn", "Переменная частота кадров без максимума; среднее не исключает превышения предела ТВ.")
    if rule:
        for actual, allowed, name in ((depth, rule["bit_depths"], "Глубина цвета"), (chroma.split(" (")[0], rule["chroma"], "Цветность")):
            if not actual or not allowed:
                _issue(result, "warn", f"{name}: значение или ограничение профиля неизвестно.")
            elif actual not in allowed:
                _issue(result, "fail", f"{name} {actual} не поддерживается; допустимо: {', '.join(map(str, allowed))}. Перекодировать видео.")
        base = profile.split("@")[0].strip().lower().removeprefix("profile ").strip()
        allowed_profiles = {name.lower() for name in rule["profiles"]}
        if not base or not allowed_profiles:
            _issue(result, "warn", "Профиль видеокодека или его поддержка неизвестны.")
        elif base not in allowed_profiles:
            _issue(result, "fail", f"Профиль {profile} вне поддерживаемых: {', '.join(rule['profiles'])}. Нужна другая версия или перекодирование.")
        color = _text(track, "ColorSpace").upper()
        if color and color not in {"YUV", "YCBCR"} and rule["chroma"] == ["4:2:0"]:
            _issue(result, "fail", f"Цветовое пространство {color} не соответствует YUV 4:2:0.")
        limit_level = rule.get("max_level_uhd" if uhd else "max_level_fhd")
        if limit_level:
            if not level:
                _issue(result, "warn", "Уровень (Level) неизвестен.")
            elif level > limit_level + 0.0001:
                _issue(result, "fail", f"{codec} L{level:g} выше документированного L{limit_level:g}. Перекодировать с нужным уровнем; смена метки Level не доказывает соответствие потока.")
        limit_rate = result["limit_mbps"]
        if not limit_rate:
            _issue(result, "warn", "В профиле нет документированного предела битрейта этого кодека.")
        elif bitrate and bitrate > limit_rate * 1000000:
            average = _number(track.get("BitRate"))
            exceeds_average = average and average > limit_rate * 1000000
            _issue(result, "fail" if exceeds_average else "warn",
                   f"{rate_kind.capitalize()} битрейт {bitrate / 1000000:g} Мбит/с выше предела {limit_rate:g} Мбит/с. " +
                   ("Перекодировать с ограничением максимума/VBV." if exceeds_average else "Заявленный VBV не равен измеренному пику; требуется полное измерение."))
        if codec == "HEVC" and ("@HIGH" in profile.upper() or _text(track, "Format_Tier").lower() == "high"):
            _issue(result, "warn", "HEVC High Tier отдельно не подтверждён профилем ТВ.")
    else:
        _issue(result, "warn", "Профиль ТВ не содержит численных ограничений для этого кодека.")
    if codec in {"VC-1", "WMV"} and tv["id"] == "lg-uk6300":
        if _text(track, "CodecID").upper() == "WMVA":
            _issue(result, "fail", "VC-1 WMVA явно исключён руководством; перекодировать видео в H.264.")
        if profile and not any(name in profile.lower() for name in ("simple", "main", "advanced")):
            _issue(result, "warn", f"Профиль {profile} не указан в таблице VC-1.")
    for key, name in (("Format_Settings_QPel", "Qpel"), ("Format_Settings_GMC", "GMC")):
        value = _text(track, key).lower()
        if not tv["options"]["gmc_qpel"] and value and value not in {"no", "false", "0", "0 warppoints", "0 warp points", "none"}:
            if value in {"yes", "true"} or re.match(r"[1-9]", value):
                _issue(result, "fail", f"Включён {name}, явно запрещённый руководством LG; перекодировать видео без GMC/Qpel.")
            else:
                _issue(result, "warn", f"Значение {name} «{value}» не удалось интерпретировать.")
    hdr = _joined(track, "HDR_Format", "HDR_Format_Compatibility", "HDR_Format_Commercial", "HDR_Format_Profile")
    if not tv["options"]["dolby_vision"] and ("dolby vision" in hdr.lower() or _text(track, "CodecID").lower() in {"dvhe", "dvh1", "dvav", "dva1"}):
        if "hdr10" in hdr.lower():
            _issue(result, "warn", "Dolby Vision у выбранной модели не заявлен; указан HDR10-совместимый базовый слой. Возможен HDR10 fallback, не Dolby Vision; проверить на ТВ.")
        else:
            _issue(result, "fail", "Dolby Vision не поддерживается моделью, совместимый HDR10-слой не подтверждён. Нужна HDR10/SDR-версия; при конвертации требуется корректное преобразование цветов, не простое удаление HDR-метки.")
    elif "hdr10+" in hdr.lower() and "HDR10+" not in tv["hdr_formats"]:
        _issue(result, "warn", "HDR10+ не заявлен; возможен базовый HDR10 без динамических метаданных.")
    if hdr:
        result["details"] += "; HDR: " + hdr
    if uhd and (container not in tv["uhd_containers"] or codec not in tv["uhd_video"]):
        _issue(result, "warn", "Эта связка контейнера и кодека не подтверждена UHD-таблицей профиля ТВ; проверить профиль или выполнить remux/перекодирование.")
    return result, uhd


def _audio(track, index, container, mode, uhd, tv):
    result = _result(track, "Аудио", index)
    codec = _audio_codec(track)
    profile = _joined(track, "Format", "Format_Profile", "Format_AdditionalFeatures", "Format_Commercial_IfAny", "CodecID")
    details = [codec]
    for key, title in (("Format_Profile", "профиль"), ("Channels", "каналов"), ("SamplingRate", "Гц"), ("BitRate", "бит/с")):
        value = _text(track, key)
        if value:
            details.append(f"{value} {title}")
    result["details"] = "; ".join(details)
    result["codec"] = codec
    _pair(result, codec, container, tv, audio=True)
    if codec.casefold() in {name.casefold() for name in tv["unsupported_audio"]}:
        _issue(result, "fail", f"{codec} не поддерживается выбранным ТВ. {AUDIO_ADVICE}")
    elif codec not in {name for pair in tv["containers"].values() for name in pair["audio"]}:
        _issue(result, "warn", f"Аудиокодек {codec} не распознан либо не документирован. {AUDIO_ADVICE}")
    if codec == "AAC":
        if not tv["options"]["aac_main"] and re.search(r"\bmain\b", profile, re.I):
            _issue(result, "fail", f"AAC Main не поддерживается выбранным профилем. {AUDIO_ADVICE}")
        elif not (tv["options"]["aac_main"] and re.search(r"\bmain\b", profile, re.I)) and not re.search(r"\bLC\b|HE-AAC|SBR|A_AAC/MPEG[24]/LC", profile, re.I):
            _issue(result, "warn", f"Профиль AAC не подтверждён как LC/HE-AAC; требуется уточнение. {AUDIO_ADVICE}")
    if codec == "DTS":
        if mode != "usb" and not tv["options"]["dts_network"]:
            _issue(result, "fail", f"В выбранном профиле DTS не поддерживается по сети. {AUDIO_ADVICE}")
        if tv["id"] == "lg-uk6300" and re.search(r"DTS.?HD|MA\b|HRA\b|XLL|XBR|EXPRESS|LBR|DTS:X", profile, re.I):
            _issue(result, "warn", "DTS-HD/DTS Express указаны в характеристиках UK6300PLB, поэтому не запрещены целиком. Поддержка конкретного расширения/профиля и доступность DTS core по метаданным не гарантируются; проверить на USB, при проблеме извлечь core или перекодировать звук в AC-3.")
    if codec == "WMA" and tv["id"] == "lg-uk6300":
        version = _text(track, "Format_Version").lower()
        if re.search(r"speech|voice|wma\s*v?1\b|wma1|0x?0?0?0?a\b", profile, re.I) or version in {"version 1", "1"}:
            _issue(result, "fail", f"WMA v1 / Speech явно исключены LG. {AUDIO_ADVICE}")
        elif re.search(r"pro\b|lossless", profile, re.I):
            _issue(result, "warn", "В ASF/WMV указан только WMA Standard, не Pro/Lossless; рекомендуется AAC/AC-3 в MKV/MP4.")
        elif not re.search(r"standard|wma2|version 2|version 3", profile + " " + version, re.I) and _text(track, "CodecID") not in {"161", "0x0161"}:
            _issue(result, "warn", "Версия WMA неизвестна: LG требует WMA V7+ и исключает WMA v1 / Speech.")
    if codec == "AC-4" and tv["id"] == "lg-uk6300":
        _issue(result, "warn", "AC-4 в руководстве доступен лишь некоторым моделям и не подтверждён спецификацией UK6300PLB; безопаснее AC-3/AAC LC.")
    for key, name in (("Channels", "число каналов"), ("SamplingRate", "частота дискретизации")):
        if not _number(track.get(key)):
            _issue(result, "warn", f"Неизвестно/некорректно {name} аудио; точная конфигурация не проверена.")
    if uhd and codec not in tv["uhd_audio"]:
        _issue(result, "warn", "Этот звук не подтверждён отдельной UHD-таблицей профиля; выбрать поддерживаемую UHD-аудиодорожку.")
    if "atmos" in profile.lower() and codec == "E-AC-3":
        _issue(result, "warn", "E-AC-3 допускается, но воспроизведение Atmos этой моделью не заявлено; возможен базовый Dolby Digital Plus без Atmos.")
    return result


def _subtitle(track, index, container, mode):
    # Informational only: subtitle typography must not turn a playable film yellow.
    result = _result(track, "Субтитры", index)
    result["status"] = "info"
    result["details"] = _joined(track, "Format", "CodecID") or "Формат неизвестен"
    fmt = result["details"].lower()
    if any(name in fmt for name in ("ass", "ssa", "sub station alpha")):
        result["issues"].append("ASS/SSA: оформление и вложенные шрифты зависят от плеера; на оценку видео/звука не влияют.")
    elif any(name in fmt for name in ("pgs", "hdmv", "vobsub")):
        result["issues"].append("Графические субтитры могут не отображаться; для SRT нужен OCR. На общую оценку не влияют.")
    return result


def assess(path: str, metadata: dict, mode: str = "usb", profile: dict | None = None) -> dict:
    """Return an independent, JSON-serializable report; never open or modify a file."""
    from tv_profiles import get_profile
    tv = profile if profile is not None else get_profile()
    report = {"path": str(path), "status": "ok", "summary": "", "container": "неизвестен", "video": [], "audio": [], "subtitles": [], "issues": []}
    report["model"] = tv["name"]
    report["firmware"] = tv["firmware"]
    report["sources"] = tv["sources"]
    if not tv["verified"]:
        _issue(report, "warn", "Пользовательский профиль не подтверждён документацией: его ограничения требуют проверки.")
    media = metadata.get("media") if isinstance(metadata, dict) else None
    tracks = media.get("track") if isinstance(media, dict) else None
    if not isinstance(tracks, list) or not tracks:
        _issue(report, "error", "Повреждённая/пустая структура MediaInfo: ожидался media.track со списком дорожек.")
        report["summary"] = "Ошибка метаданных: совместимость не определена."
        return report
    valid = []
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("@type"), str):
            _issue(report, "warn", "Некорректная запись дорожки MediaInfo пропущена; анализ неполон.")
        else:
            valid.append(track)
    general_tracks = [t for t in valid if t["@type"].lower() == "general"]
    general = general_tracks[0] if general_tracks else {}
    container, ext = _container(general, str(path))
    report["container"] = container.upper() if container else _text(general, "Format") or "неизвестен"
    if not container or container not in tv["containers"]:
        _issue(report, "warn", "Контейнер неизвестен или не перечислен в профиле ТВ; расширение само по себе не доказывает формат. Для совместимых потоков выполнить remux в контейнер из профиля.")
    elif ext not in EXTENSIONS:
        _issue(report, "warn", f"Расширение .{ext or '(нет)'} неизвестно анализатору; ТВ может не показать файл. Контейнер определён по содержимому.")
    elif EXTENSIONS[ext] != container:
        _issue(report, "warn", "Расширение не соответствует определённому контейнеру; исправить имя после проверки формата либо выполнить remux.")
    if ext == "m2ts" and tv["id"] == "lg-uk6300":
        _issue(report, "warn", "MPEG-TS распознан, но расширение .m2ts в таблице не указано (есть .mts/.ts); проверить обнаружение файла на ТВ.")
    if len(general_tracks) != 1:
        _issue(report, "warn", "Общая дорожка General отсутствует или неоднозначна; метаданные неполны.")
    if mode not in {"usb", "network"}:
        _issue(report, "warn", "Неизвестный режим воспроизведения; USB-исключения не применяются.")
    if mode == "network":
        _issue(report, "warn", "Сетевая доставка зависит от DLNA/SMB-сервера, передачи дорожек и скорости сети; таблица кодеков не гарантирует сетевое воспроизведение. Проверка не учитывает возможное серверное транскодирование.")
    if container == "rm" and tv["id"] == "lg-uk6300":
        _issue(report, "warn", "RealMedia доступен только в отдельных странах; региональная поддержка модели не подтверждена.")
    if _text(general, "IsTruncated").lower() in {"yes", "1", "true"}:
        _issue(report, "fail", "MediaInfo сообщает об усечённом файле; получить неповреждённую копию.")
    video_tracks = [t for t in valid if t["@type"].lower() == "video"]
    audio_tracks = [t for t in valid if t["@type"].lower() == "audio"]
    subtitle_tracks = [t for t in valid if t["@type"].lower() in {"text", "subtitle"}]
    uhd = False
    for index, track in enumerate(video_tracks, 1):
        result, track_uhd = _video(track, index, container, tv)
        report["video"].append(result)
        uhd |= track_uhd
    report["audio"] = [_audio(track, index, container, mode, uhd, tv) for index, track in enumerate(audio_tracks, 1)]
    report["subtitles"] = [_subtitle(track, index, container, mode) for index, track in enumerate(subtitle_tracks, 1)]
    if not video_tracks:
        _issue(report, "fail", "Видеодорожки не найдены; это не проверяемый видеофайл либо анализ не удался.")
    elif all(t["status"] in {"fail", "error"} for t in report["video"]):
        _issue(report, "fail", "Нет совместимой видеодорожки; необходима другая версия или перекодирование видео.")
    if not audio_tracks:
        _issue(report, "warn", "Аудиодорожки не найдены: видео может быть без звука либо анализ неполон.")
    elif all(t["status"] in {"fail", "error"} for t in report["audio"]):
        _issue(report, "fail", "Все аудиодорожки несовместимы: видео может показываться без звука. " + AUDIO_ADVICE)
    all_results = report["video"] + report["audio"]
    if any(t["status"] != "ok" for t in all_results):
        _issue(report, "warn", "Есть ограничения или неизвестные параметры дорожек; подробности указаны по каждой дорожке.")
    for key, name in (("video", "видео"), ("audio", "аудио")):
        results = report[key]
        if any(t["status"] == "fail" for t in results) and any(t["status"] in {"ok", "warn"} for t in results):
            _issue(report, "warn", f"Есть альтернативная {name}дорожка без установленного запрета. Выбрать её на ТВ; при невозможности переключения удалить несовместимые дорожки при remux. Дорожка с WARN всё ещё требует проверки.")
    if len(video_tracks) > 1:
        _issue(report, "warn", "Несколько видеодорожек: переключение видео ТВ не гарантировано; оставить нужную совместимую при remux.")
    report["summary"] = {
        "ok": "Соответствует проверенным документированным параметрам; воспроизведение не гарантируется.",
        "warn": "Требуется внимание: есть ограничения, неподтверждённые параметры или выбор дорожки.",
        "fail": "Обнаружена несовместимость: нужна другая версия, выбор/замена потоков или исправление файла.",
        "error": "Ошибка анализа: совместимость не определена.",
    }[report["status"]]
    return report
