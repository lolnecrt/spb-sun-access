# Скачивает из OpenStreetMap данные по району: здания, объекты и пешеходный граф.
# Запуск: .venv\Scripts\python.exe scripts\01_download.py
# Файлы, которые уже лежат в data/, скачиваться заново не будут.

import logging
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError

import config

# Пути считаем от расположения скрипта, чтобы он работал из любой папки
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BOUNDARY_FILE = DATA_DIR / "boundary.geojson"
GRAPH_FILE = DATA_DIR / "walk_graph.graphml"


class IncompleteResponseError(Exception):
    """Сервер ответил «успешно», но предупредил, что данные неполные."""


class NoFeaturesError(Exception):
    """Сервер ответил нормально, но таких объектов на территории нет."""


class DownloadFailedError(Exception):
    """Скачать не удалось даже после всех попыток."""


class OsmnxLogWatcher(logging.Handler):
    """Показывает предупреждения osmnx и замечает ответы с пометкой remark.

    Если Overpass не успевает выполнить запрос, он может ответить кодом 200
    и обрезанными данными, добавив к ним пометку remark. Исключения при этом нет,
    osmnx только пишет об этом в журнал, поэтому мы следим за журналом.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.remark = None

    def emit(self, record):
        # Ответ сервера может быть длинной HTML-страницей: показываем только начало
        message = " ".join(record.getMessage().split())[:200]
        print(f"  [osmnx] {message}")
        if "remarked" in message:
            self.remark = message


def setup_osmnx():
    """Настраивает osmnx и возвращает объект, который следит за её журналом."""
    ox.settings.cache_folder = DATA_DIR / "cache"
    ox.settings.overpass_url = config.OVERPASS_URL
    ox.settings.requests_timeout = config.REQUEST_TIMEOUT_S

    # Сохраняем тег foot. Встроенный фильтр access отключаем: он убрал бы и дорожки
    # с access=private и foot=yes. Что закрыто для пешехода, решает 03_access.py
    ox.settings.useful_tags_way = [*ox.settings.useful_tags_way, *config.GRAPH_EXTRA_TAGS]
    ox.settings.default_access = ""

    # Журнал osmnx нужен нам только в программе: наш обработчик занимает место
    # обработчика по умолчанию, поэтому отдельный файл журнала не создаётся
    watcher = OsmnxLogWatcher()
    logging.getLogger(ox.settings.log_name).addHandler(watcher)
    ox.settings.log_file = True
    return watcher


def with_retries(title, download, watcher):
    """Вызывает функцию скачивания и при ошибке сервера повторяет её с растущей паузой."""
    pause = config.RETRY_FIRST_PAUSE_S

    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        watcher.remark = None
        try:
            result = download()
            if watcher.remark:
                raise IncompleteResponseError(watcher.remark)
            return result
        except (
            requests.RequestException,
            ResponseStatusCodeError,
            InsufficientResponseError,
            IncompleteResponseError,
        ) as error:
            # osmnx выдаёт одно и то же исключение и на плохой ответ, и на пустой результат.
            # Пустой результат — не ошибка сервера, повторять запрос незачем
            if "No matching features" in str(error):
                raise NoFeaturesError from error

            short_error = " ".join(str(error).split())[:200]
            print(f"  {title}: попытка {attempt} из {config.MAX_ATTEMPTS} не удалась ({short_error})")
            if attempt == config.MAX_ATTEMPTS:
                raise DownloadFailedError(short_error) from error

            print(f"  Ждём {pause} с и пробуем снова...")
            time.sleep(pause)
            pause = min(pause * 2, config.RETRY_MAX_PAUSE_S)


def save_safely(path, write):
    """Пишет файл под временным именем и переименовывает его только в конце.

    Если запуск прервётся, в data/ не останется половины файла,
    которую следующий запуск принял бы за готовый результат.
    """
    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    temp_path.unlink(missing_ok=True)
    write(temp_path)
    os.replace(temp_path, path)


def save_geojson(gdf, path):
    save_safely(path, lambda temp_path: gdf.to_file(temp_path, driver="GeoJSON"))


def get_boundary(watcher):
    """Возвращает границу района из data/ или скачивает её."""
    if BOUNDARY_FILE.exists():
        print("Граница района: уже есть в data/, не скачиваем")
        return gpd.read_file(BOUNDARY_FILE)

    print("Граница района: скачиваем...")
    boundary = with_retries(
        "Граница района",
        lambda: ox.geocode_to_gdf(config.PLACE_NAME),
        watcher,
    )
    save_geojson(boundary, BOUNDARY_FILE)
    return boundary


def make_buffered_polygon(boundary):
    """Строит границу района с запасом. Метры считаем в проекции UTM: в градусах нельзя."""
    utm_crs = boundary.estimate_utm_crs()
    buffered = boundary.to_crs(utm_crs).buffer(config.BUFFER_M)
    return buffered.to_crs(boundary.crs).union_all()


def prepare_layer(gdf, name, columns, district):
    """Оставляет нужные объекты и колонки и отмечает, что лежит внутри района."""
    gdf = gdf.reset_index().rename(columns={"element": "osm_type", "id": "osm_id"})

    # Для зданий нужны только контуры. Значение building=no означает «это не здание»
    if name == "buildings":
        is_polygon = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        gdf = gdf[is_polygon & (gdf["building"] != "no")]

    kept_columns = [column for column in columns if column in gdf.columns]
    gdf = gdf[["osm_type", "osm_id", *kept_columns, "geometry"]].copy()
    gdf["in_district"] = gdf.intersects(district)
    return gdf


def download_layer(name, layer, search_area, district, watcher):
    """Скачивает один слой, если его ещё нет в data/. Возвращает False при неудаче."""
    path = DATA_DIR / f"{name}.geojson"
    label = layer["label"]

    if path.exists():
        print(f"{label}: уже есть в data/, не скачиваем")
        return True

    print(f"{label}: скачиваем...")
    try:
        gdf = with_retries(
            label,
            lambda: ox.features_from_polygon(search_area, layer["tags"]),
            watcher,
        )
    except NoFeaturesError:
        print(f"{label}: 0 объектов, файл не создаём")
        return True
    except DownloadFailedError:
        print(f"{label}: не удалось скачать")
        return False

    save_geojson(prepare_layer(gdf, name, layer["columns"], district), path)
    return True


def download_graph(search_area, watcher):
    """Скачивает пешеходный граф, если его ещё нет в data/. Возвращает False при неудаче."""
    if GRAPH_FILE.exists():
        print("Пешеходный граф: уже есть в data/, не скачиваем")
        return True

    print("Пешеходный граф: скачиваем...")
    try:
        graph = with_retries(
            "Пешеходный граф",
            lambda: ox.graph_from_polygon(search_area, network_type=config.NETWORK_TYPE, simplify=False),
            watcher,
        )
    except DownloadFailedError:
        print("Пешеходный граф: не удалось скачать")
        return False

    graph = ox.simplify_graph(graph, edge_attrs_differ=config.GRAPH_KEEP_SEPARATE_TAGS)
    save_safely(GRAPH_FILE, lambda temp_path: ox.save_graphml(graph, temp_path))
    return True


def tag_is_filled(gdf, column):
    """Показывает для каждой строки, заполнен ли тег. Колонки может не быть совсем."""
    if column in gdf.columns:
        return gdf[column].notna()
    return pd.Series(False, index=gdf.index)


def print_share(title, count, total):
    share = count / total if total else 0
    print(f"  {title}: {count} из {total} ({share:.1%})")


def print_summary():
    """Печатает сводку по файлам из data/, поэтому учитывает и скачанное раньше."""
    print()
    print("=== Сводка ===")
    print(f"Объектов OSM (в районе / вместе с запасом {config.BUFFER_M} м):")

    for name, layer in config.LAYERS.items():
        path = DATA_DIR / f"{name}.geojson"
        if not path.exists():
            print(f"  {layer['label']}: нет данных")
            continue
        gdf = gpd.read_file(path)
        print(f"  {layer['label']}: {int(gdf['in_district'].sum())} / {len(gdf)}")

    if GRAPH_FILE.exists():
        graph = ox.load_graphml(GRAPH_FILE)
        print(f"Пешеходный граф (с запасом): {len(graph.nodes)} узлов, {len(graph.edges)} рёбер")
    else:
        print("Пешеходный граф: нет данных")

    buildings_file = DATA_DIR / "buildings.geojson"
    if buildings_file.exists():
        buildings = gpd.read_file(buildings_file)
        buildings = buildings[buildings["in_district"]]
        has_levels = tag_is_filled(buildings, "building:levels")
        has_height = tag_is_filled(buildings, "height")
        total = len(buildings)

        print("Здания района, у которых заполнен тег:")
        print_share("этажность (building:levels)", int(has_levels.sum()), total)
        print_share("высота (height)", int(has_height.sum()), total)
        print_share("этажность или высота", int((has_levels | has_height).sum()), total)


def main():
    # Русский текст не должен ломаться в консоли Windows, а строки должны появляться сразу
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    DATA_DIR.mkdir(exist_ok=True)
    watcher = setup_osmnx()

    try:
        boundary = get_boundary(watcher)
    except DownloadFailedError:
        print("Не удалось получить границу района, без неё скачивать нечего.")
        sys.exit(1)

    district = boundary.union_all()
    search_area = make_buffered_polygon(boundary)

    failed = []
    for name, layer in config.LAYERS.items():
        if not download_layer(name, layer, search_area, district, watcher):
            failed.append(layer["label"])
    if not download_graph(search_area, watcher):
        failed.append("Пешеходный граф")

    print_summary()

    if failed:
        print()
        print(f"Не скачалось: {', '.join(failed)}.")
        print("Запустите скрипт ещё раз: он докачает только эти данные.")
        sys.exit(1)


if __name__ == "__main__":
    main()
