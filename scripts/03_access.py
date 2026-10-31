# Считает время пешком от каждого жилого дома района до ближайших объектов каждого типа.
# Запуск: .venv\Scripts\python.exe scripts\03_access.py
# Интернет не нужен. Результат: data/access.geojson, data/access_hex.geojson, docs/check_inferred.csv.

import math
import os
import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import Polygon

import config

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ACCESS_FILE = DATA_DIR / "access.geojson"
HEX_FILE = DATA_DIR / "access_hex.geojson"
CHECK_FILE = ROOT / "docs" / "check_inferred.csv"

# Временный узел графа, к которому подключаем все объекты одного типа
VIRTUAL_NODE = "объекты"

# Имя типа объекта: подпись для отчёта. Столбец времени называется t_<имя>
TARGET_LABELS = {
    "school": "Школа (general)",
    "kindergarten": "Детский сад",
    "clinic_adult": "Поликлиника взрослая",
    "clinic_child": "Поликлиника детская",
    "park": "Парк",
    "playground": "Детская площадка",
    "school_public": "Контроль: государственная школа",
    "clinic_any": "Контроль: любая запись clinics",
}


def osm_keys(gdf):
    """Возвращает ключи записей в формате «тип/номер», как в config.py."""
    return gdf["osm_type"] + "/" + gdf["osm_id"].astype(str)


def read_layer(name, crs):
    return gpd.read_file(DATA_DIR / f"{name}.geojson").to_crs(crs)


def passes_inferred_rule(district, facilities):
    """Проверяет признаки правила inferred. Сам тег здесь не смотрим, чтобы проверить правило на всех."""
    # Учреждение занимает здание, только если точка внутри здания лежит внутри контура учреждения.
    # Точка учреждения внутри дома ничего не значит: это может быть клиника на первом этаже
    polygons = facilities[facilities.geom_type != "Point"][["geometry"]]
    inner_points = gpd.GeoDataFrame(geometry=district.representative_point(), crs=district.crs)
    occupied = gpd.sjoin(inner_points, polygons, predicate="within").index.unique()

    # Литеру и корпус отбрасываем: «203 литЕД» и «203 к2» относятся к одному комплексу
    numbers = district["addr:housenumber"].str.extract(r"^([\d/-]+)")[0]
    addresses = pd.Series(list(zip(district["addr:street"], numbers)), index=district.index)
    in_complex = addresses.isin(config.EXCLUDED_COMPLEXES)
    print(f"Корпуса закрытых комплексов (config.py), не считаем домами: {int(in_complex.sum())}")

    return (
        district["addr:housenumber"].notna()
        & district["name"].isna()
        & (district["area_m2"] >= config.INFERRED_MIN_AREA_M2)
        & (district["levels"].isna() | (district["levels"] >= config.INFERRED_MIN_LEVELS))
        & ~district.index.isin(occupied)
        & ~in_complex
    )


def select_homes(buildings, facilities):
    """Отбирает жилые дома района и записывает в residential_source, как именно мы это узнали."""
    district = buildings[buildings["in_district"]].copy()
    district["area_m2"] = district.area
    district["levels"] = pd.to_numeric(district["building:levels"], errors="coerce")

    passes_rule = passes_inferred_rule(district, facilities)
    has_tag = district["building"].isin(config.RESIDENTIAL_TAGS)
    is_inferred = (district["building"] == "yes") & passes_rule

    print(f"Жилые дома: {int(has_tag.sum())} tag + {int(is_inferred.sum())} inferred")
    # Проверяем правило на зданиях, вид которых известен по тегу
    is_nonresidential = district["building"].isin(config.NONRESIDENTIAL_TAGS)
    print(f"Правило пропускает явно жилых: {passes_rule[has_tag].mean():.1%} из {int(has_tag.sum())}")
    print(f"Правило пропускает явно нежилых: {passes_rule[is_nonresidential].mean():.1%} из {int(is_nonresidential.sum())}")

    district["residential_source"] = None
    district.loc[has_tag, "residential_source"] = "tag"
    district.loc[is_inferred, "residential_source"] = "inferred"
    return district[has_tag | is_inferred].copy()


def load_polyclinics(clinics, buildings, crs):
    """Собирает поликлиники из списка в config.py: запись может быть в любом из двух слоёв."""
    records = pd.concat([clinics, buildings], ignore_index=True)
    records["key"] = osm_keys(records)
    chosen = records[records["key"].isin(config.POLYCLINICS)].drop_duplicates("key").copy()
    chosen["kind"] = chosen["key"].map(lambda key: config.POLYCLINICS[key][0])

    missing = set(config.POLYCLINICS) - set(chosen["key"])
    if missing:
        raise ValueError(f"Этих поликлиник из config.py нет в данных: {sorted(missing)}")
    return gpd.GeoDataFrame(chosen, geometry="geometry", crs=crs)


def is_open(gdf):
    """Отбрасывает объекты, закрытые для обычного прохожего."""
    return ~gdf["access"].isin(config.EXCLUDED_ACCESS)


def load_targets(buildings, crs):
    """Готовит объекты каждого типа: кого считаем школой, парком и так далее."""
    schools = read_layer("schools", crs)
    general = schools[schools["school_kind"] == "general"]
    clinics = read_layer("clinics", crs)
    parks = read_layer("parks", crs)
    playgrounds = read_layer("playgrounds", crs)
    polyclinics = load_polyclinics(clinics, buildings, crs)

    return {
        "school": general,
        "kindergarten": read_layer("kindergartens", crs),
        "clinic_adult": polyclinics[polyclinics["kind"] == "adult"],
        "clinic_child": polyclinics[polyclinics["kind"] == "child"],
        "park": parks[is_open(parks) & (parks.area >= config.PARK_MIN_AREA_M2)],
        "playground": playgrounds[is_open(playgrounds)],
        # is_private в файле может читаться как 0/1, поэтому сравниваем с нулём
        "school_public": general[general["is_private"] == 0],
        "clinic_any": clinics,
    }


def perimeter_points(geometry):
    """Возвращает точки объекта: сама точка или точки по внешней границе контура."""
    if geometry.geom_type == "Point":
        return [geometry]

    parts = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
    points = []
    for part in parts:
        ring = part.exterior
        for distance in np.arange(0, ring.length, config.PERIMETER_STEP_M):
            points.append(ring.interpolate(distance))
    return points


def snap_to_nodes(points, nodes):
    """Находит для точек ближайшие узлы графа и расстояние до них по прямой."""
    positions, distances = nodes.sindex.nearest(points, return_distance=True, return_all=False)
    return nodes.index[positions[1]].to_numpy(), distances


def distances_to_nearest(reverse_graph, target, nodes):
    """Считает для всех узлов графа расстояние до ближайшего объекта.

    Все точки объектов подключаем к одному временному узлу: длина связи равна отрезку
    по прямой от точки до графа. Один проход Дейкстры от этого узла по графу с
    развёрнутыми рёбрами даёт сразу все узлы. Это намного быстрее, чем считать каждую пару.
    """
    points = [point for geometry in target.geometry for point in perimeter_points(geometry)]
    entry_nodes, legs = snap_to_nodes(points, nodes)

    # К одному узлу могут вести несколько точек: важна самая близкая
    shortest_legs = pd.Series(legs).groupby(entry_nodes).min()
    reverse_graph.add_weighted_edges_from(
        ((VIRTUAL_NODE, node, leg) for node, leg in shortest_legs.items()), weight="length"
    )
    result = nx.single_source_dijkstra_path_length(reverse_graph, VIRTUAL_NODE, weight="length")
    reverse_graph.remove_node(VIRTUAL_NODE)
    return result, gpd.GeoSeries(points, crs=target.crs)


def build_reverse_graph(graph):
    """Переводит граф в простой: между парой узлов оставляет кратчайшее ребро и разворачивает его."""
    reverse_graph = nx.DiGraph()
    for start, end, data in graph.edges(data=True):
        length = data["length"]
        if not reverse_graph.has_edge(end, start) or reverse_graph[end][start]["length"] > length:
            reverse_graph.add_edge(end, start, length=length)
    return reverse_graph


def add_walk_times(homes, targets, graph, nodes):
    """Добавляет домам столбцы t_<тип> с минутами и проверяет каждый результат."""
    meters_per_minute = config.WALK_SPEED_KMH * 1000 / 60
    reverse_graph = build_reverse_graph(graph)

    points = homes.representative_point()
    home_nodes, home_legs = snap_to_nodes(points, nodes)
    homes["access_leg_m"] = home_legs

    for name, target in targets.items():
        to_target, target_points = distances_to_nearest(reverse_graph, target, nodes)
        meters = home_legs + np.array([to_target[node] for node in home_nodes])
        homes[f"t_{name}"] = meters / meters_per_minute

        # Путь не может быть короче прямой до ближайшего объекта. Запас 1 % нужен потому,
        # что длины рёбер считаны по сфере, а прямая в проекции UTM
        _, straight = target_points.sindex.nearest(points, return_distance=True, return_all=False)
        if (meters < straight * 0.99).any():
            raise ValueError(f"{name}: маршрут короче прямой линии, в расчёте ошибка")
    return homes


def build_hexagons(homes):
    """Строит сетку гексагонов на территории домов и возвращает только занятые ячейки."""
    side = config.HEX_SIDE_M
    step_x = math.sqrt(3) * side
    step_y = 1.5 * side
    min_x, min_y, max_x, max_y = homes.total_bounds

    cells = []
    for row in range(int((max_y - min_y) / step_y) + 3):
        y = min_y - step_y + row * step_y
        shift = step_x / 2 if row % 2 else 0
        for column in range(int((max_x - min_x) / step_x) + 3):
            x = min_x - step_x + column * step_x + shift
            corners = [
                (x + side * math.sin(math.radians(a)), y + side * math.cos(math.radians(a)))
                for a in range(0, 360, 60)
            ]
            cells.append(Polygon(corners))
    return gpd.GeoDataFrame({"hex_id": range(len(cells))}, geometry=cells, crs=homes.crs)


def summarize(group, names, suffix=""):
    """Считает для группы домов медиану времени и долю домов, откуда идти дольше порога."""
    row = {}
    for name in names:
        times = group[f"t_{name}"]
        row[f"median_{name}{suffix}"] = round(times.median(), 1) if len(times) else None
        row[f"share_over{config.LONG_WALK_MIN}_{name}{suffix}"] = (
            round((times > config.LONG_WALK_MIN).mean(), 3) if len(times) else None
        )
    return row


def make_hex_summary(homes, names):
    """Сводит дома в гексагоны: отдельно все дома и отдельно только уровень tag."""
    hexagons = build_hexagons(homes)
    inner_points = gpd.GeoDataFrame(geometry=homes.representative_point(), crs=homes.crs)
    joined = gpd.sjoin(inner_points, hexagons, predicate="within")
    # Точка ровно на границе двух ячеек попала бы в обе: оставляем одну
    hex_of_home = joined[~joined.index.duplicated()]["hex_id"]

    rows = []
    for hex_id, group in homes.groupby(hex_of_home):
        tag_group = group[group["residential_source"] == "tag"]
        rows.append(
            {
                "hex_id": int(hex_id),
                "n_homes": len(group),
                "n_homes_tag": len(tag_group),
                "low_sample": len(group) < config.HEX_MIN_HOMES,
                "low_sample_tag": len(tag_group) < config.HEX_MIN_HOMES,
                **summarize(group, names),
                **summarize(tag_group, names, "_tag"),
            }
        )
    return hexagons.merge(pd.DataFrame(rows), on="hex_id")


def save_geojson(gdf, path):
    """Пишет файл под временным именем, чтобы при сбое не оставить половину файла."""
    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    temp_path.unlink(missing_ok=True)
    gdf.to_crs(4326).to_file(temp_path, driver="GeoJSON")
    os.replace(temp_path, path)


def save_homes(homes, names):
    """Сохраняет дома: контуры, адрес, источник признака «жилой» и время в минутах."""
    columns = {
        "osm_type": "osm_type",
        "osm_id": "osm_id",
        "addr:street": "street",
        "addr:housenumber": "housenumber",
        "building": "building",
        "levels": "levels",
        "residential_source": "residential_source",
        "access_leg_m": "access_leg_m",
        **{f"t_{name}": f"t_{name}" for name in names},
        "geometry": "geometry",
    }
    result = homes[list(columns)].rename(columns=columns)
    result = result.round({"access_leg_m": 1, **{f"t_{name}": 1 for name in names}})
    save_geojson(result, ACCESS_FILE)


def save_check_sample(homes):
    """Выгружает случайные дома inferred для ручной проверки. Готовый файл не перезаписывает."""
    if CHECK_FILE.exists():
        print(f"{CHECK_FILE.name}: уже есть, не перезаписываем, в нём могут быть ваши ответы")
        return

    inferred = homes[homes["residential_source"] == "inferred"]
    sample = inferred.sample(config.CHECK_SAMPLE_SIZE, random_state=config.CHECK_SAMPLE_SEED)
    points = sample.representative_point().to_crs(4326)

    table = pd.DataFrame(
        {
            "адрес": sample["addr:street"].fillna("улица не указана") + ", " + sample["addr:housenumber"],
            "этажей": sample["levels"],
            "площадь_м2": sample["area_m2"].round(0),
            "широта": points.y.round(6),
            "долгота": points.x.round(6),
            "ссылка_osm": "https://www.openstreetmap.org/" + sample["osm_type"] + "/" + sample["osm_id"].astype(str),
            "жилой да/нет": "",
        }
    )
    # utf-8-sig нужен, чтобы Excel правильно показал русские буквы
    table.to_csv(CHECK_FILE, index=False, encoding="utf-8-sig")
    print(f"{CHECK_FILE.name}: выгружено {len(table)} домов для ручной проверки")


def print_report(homes, targets, names):
    """Печатает состав объектов, сводку по району и сравнение со строгим вариантом."""
    print("Сколько объектов считаем (всего / в районе):")
    for name in names:
        target = targets[name]
        print(f"  {TARGET_LABELS[name]:<34}{len(target):>5} /{int(target['in_district'].sum()):>4}")

    long_walk = config.LONG_WALK_MIN
    tag_homes = homes[homes["residential_source"] == "tag"]
    print()
    print(f"Сводка по району: медиана (мин) и доля домов дольше {long_walk} мин")
    print(f"{'':<34}{'все дома':>18}{'только tag':>18}")
    for name in names:
        column = f"t_{name}"
        all_part = f"{homes[column].median():5.1f} {(homes[column] > long_walk).mean():6.1%}"
        tag_part = f"{tag_homes[column].median():5.1f} {(tag_homes[column] > long_walk).mean():6.1%}"
        print(f"{TARGET_LABELS[name]:<34}{all_part:>18}{tag_part:>18}")

    legs = homes["access_leg_m"]
    print()
    print(
        f"Отрезок от дома до графа: медиана {legs.median():.0f} м, "
        f"99-й процентиль {legs.quantile(0.99):.0f} м, максимум {legs.max():.0f} м"
    )


def main():
    # Русский текст не должен ломаться в консоли Windows, а строки должны появляться сразу
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    boundary = gpd.read_file(DATA_DIR / "boundary.geojson")
    crs = boundary.estimate_utm_crs()

    print("Читаем данные...")
    buildings = read_layer("buildings", crs)
    graph = ox.load_graphml(DATA_DIR / "walk_graph.graphml")
    nodes = ox.graph_to_gdfs(graph, edges=False).to_crs(crs)[["geometry"]]

    targets = load_targets(buildings, crs)
    # Здесь нужны все школы, а не только общеобразовательные: любая из них занимает здание
    facilities = pd.concat(
        [read_layer("schools", crs), targets["kindergarten"], targets["clinic_any"]], ignore_index=True
    )
    homes = select_homes(buildings, facilities)

    names = list(TARGET_LABELS)
    print("Считаем маршруты...")
    homes = add_walk_times(homes, targets, graph, nodes)

    time_columns = [f"t_{name}" for name in names]
    if homes[time_columns].isna().any().any() or (homes[time_columns] < 0).any().any():
        raise ValueError("Есть пустые или отрицательные времена")

    hexagons = make_hex_summary(homes, names)
    if hexagons["n_homes"].sum() != len(homes):
        raise ValueError("Сумма домов по гексагонам не совпадает с числом домов")

    save_homes(homes, names)
    save_geojson(hexagons, HEX_FILE)
    save_check_sample(homes)

    print()
    print_report(homes, targets, names)
    print()
    print(f"Гексагонов с домами: {len(hexagons)}, из них с малой выборкой: {int(hexagons['low_sample'].sum())}")
    print(f"Сохранено: {ACCESS_FILE.name}, {HEX_FILE.name}")


if __name__ == "__main__":
    main()
