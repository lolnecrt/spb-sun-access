# Готовит данные для карты index.html: объекты со значками и настройки шкалы.
# Запуск: .venv\Scripts\python.exe scripts\04_map_data.py
# Интернет не нужен. Результат: data/map_objects.geojson, data/map_config.json.

import importlib.util
import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

import config

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OBJECTS_FILE = DATA_DIR / "map_objects.geojson"
CONFIG_FILE = DATA_DIR / "map_config.json"

# Типы объектов на карте и сколько их должно быть: те же числа стоят в таблице docs/step2_access.md.
# Если число разойдётся, значит значки перестали совпадать с расчётом времени
EXPECTED_COUNTS = {
    "school": 94,
    "kindergarten": 153,
    "clinic_adult": 10,
    "park": 238,
    "playground": 593,
}


def load_access_module():
    """Подключает 03_access.py. Обычный import не подходит: имя файла начинается с цифры."""
    spec = importlib.util.spec_from_file_location("access", ROOT / "scripts" / "03_access.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_objects(targets, crs):
    """Собирает объекты всех типов в одну таблицу. У контура значок ставим в точку внутри него."""
    parts = []
    for name in EXPECTED_COUNTS:
        target = targets[name]
        if len(target) != EXPECTED_COUNTS[name]:
            raise ValueError(f"{name}: объектов {len(target)}, ожидалось {EXPECTED_COUNTS[name]}")

        # У поликлиник название берём из списка в config.py: запись могла прийти из слоя зданий
        names = target["key"].map(lambda key: config.POLYCLINICS[key][1]) if name == "clinic_adult" else target["name"]
        parts.append(gpd.GeoDataFrame({"type": name, "name": names}, geometry=target.representative_point(), crs=crs))
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=crs)


def save_map_config():
    """Копирует настройки для страницы: браузер не умеет читать config.py."""
    if len(config.MAP_TIME_COLORS) != len(config.MAP_TIME_BREAKS_MIN) + 1:
        raise ValueError("Цветов на карте должно быть на один больше, чем границ классов")
    if config.LONG_WALK_MIN not in config.MAP_TIME_BREAKS_MIN:
        raise ValueError("Порог LONG_WALK_MIN должен быть одной из границ классов")

    # До этого времени запас данных вокруг района гарантирует, что ближайший объект не потерян за краем
    meters_per_minute = config.WALK_SPEED_KMH * 1000 / 60
    settings = {
        "walk_speed_kmh": config.WALK_SPEED_KMH,
        "long_walk_min": config.LONG_WALK_MIN,
        "reliable_limit_min": round(config.BUFFER_M / meters_per_minute),
        "park_min_area_m2": config.PARK_MIN_AREA_M2,
        "time_breaks_min": config.MAP_TIME_BREAKS_MIN,
        "time_colors": config.MAP_TIME_COLORS,
    }
    CONFIG_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    # Русский текст не должен ломаться в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")

    access = load_access_module()
    crs = gpd.read_file(DATA_DIR / "boundary.geojson").estimate_utm_crs()
    targets = access.load_targets(access.read_layer("buildings", crs), crs)

    objects = build_objects(targets, crs)
    access.save_geojson(objects, OBJECTS_FILE)
    save_map_config()

    print("Объекты на карте:")
    for name, count in objects["type"].value_counts().items():
        print(f"  {name:<14}{count:>5}")
    print(f"Сохранено: {OBJECTS_FILE.name}, {CONFIG_FILE.name}")


if __name__ == "__main__":
    main()
