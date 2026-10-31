# Размечает data/schools.geojson: добавляет каждой записи поля school_kind и is_private.
# Запуск: .venv\Scripts\python.exe scripts\02_classify_schools.py
# Интернет не нужен. Повторный запуск просто перезапишет эти два поля.

import json
import os
import re
import sys
from pathlib import Path

import config

SCHOOLS_FILE = Path(__file__).resolve().parent.parent / "data" / "schools.geojson"
KINDS = ["general", "art_music", "sport", "religious", "other"]


def normalize_name(name):
    """Приводит название к одному виду: в OSM «№ 564» записан и с обычным, и с неразрывным пробелом."""
    if not name:
        return ""
    return name.replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е").lower()


def get_school_kind(properties):
    """Определяет вид школы: ручное решение, затем тег, затем название. Иначе возвращает None."""
    key = f"{properties['osm_type']}/{properties['osm_id']}"
    if key in config.SCHOOL_KIND_DISPUTED:
        return config.SCHOOL_KIND_DISPUTED[key][0]

    if properties.get("operator:type") == config.SCHOOL_RELIGIOUS_OPERATOR_TYPE:
        return "religious"

    name = normalize_name(properties.get("name"))
    for kind, pattern in config.SCHOOL_KIND_RULES:
        if re.search(pattern, name):
            return kind
    return None


def get_is_private(properties):
    """Определяет, частная ли школа: сначала по тегу, потом по названию, иначе None (null в файле)."""
    operator_type = properties.get("operator:type")
    if operator_type in config.SCHOOL_PRIVATE_BY_OPERATOR_TYPE:
        return config.SCHOOL_PRIVATE_BY_OPERATOR_TYPE[operator_type]

    name = normalize_name(properties.get("name"))
    if re.search(config.SCHOOL_PRIVATE_NAME_PATTERN, name):
        return True
    if re.search(config.SCHOOL_PUBLIC_NAME_PATTERN, name):
        return False
    return None


def save_geojson(data, path):
    """Пишет файл по одной записи в строке, через временное имя, чтобы не оставить половину файла."""
    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    header = {key: value for key, value in data.items() if key != "features"}
    features = [json.dumps(feature, ensure_ascii=False) for feature in data["features"]]
    # Заголовок дописываем без закрывающей скобки, затем добавляем список записей
    text = json.dumps(header, ensure_ascii=False)[:-1]
    text += ',\n"features": [\n' + ",\n".join(features) + "\n]\n}\n"
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def print_tables(records):
    """Печатает таблицы: виды школ и частные/государственные среди общеобразовательных."""
    print(f"{'Вид':<12}{'В районе':>10}{'В запасе':>10}{'Всего':>8}")
    for kind in KINDS + ["ИТОГО"]:
        rows = [r for r in records if kind in (r["school_kind"], "ИТОГО")]
        inside = sum(1 for r in rows if r["in_district"])
        print(f"{kind:<12}{inside:>10}{len(rows) - inside:>10}{len(rows):>8}")

    print()
    print("Частные и государственные среди general:")
    print(f"{'is_private':<12}{'В районе':>10}{'В запасе':>10}{'Всего':>8}")
    general = [r for r in records if r["school_kind"] == "general"]
    for value in [False, True, None]:
        rows = [r for r in general if r["is_private"] is value]
        inside = sum(1 for r in rows if r["in_district"])
        print(f"{str(value):<12}{inside:>10}{len(rows) - inside:>10}{len(rows):>8}")


def main():
    # Русский текст не должен ломаться в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")

    data = json.loads(SCHOOLS_FILE.read_text(encoding="utf-8"))
    records = [feature["properties"] for feature in data["features"]]

    unsorted_records = []
    for properties in records:
        kind = get_school_kind(properties)
        if kind is None:
            unsorted_records.append(properties)
            kind = config.SCHOOL_KIND_DEFAULT
        properties["school_kind"] = kind
        properties["is_private"] = get_is_private(properties)

    save_geojson(data, SCHOOLS_FILE)
    print_tables(records)

    keys = {f"{r['osm_type']}/{r['osm_id']}" for r in records}
    print()
    print("Спорные случаи (решение вручную, менять в config.py):")
    for key, (kind, reason) in config.SCHOOL_KIND_DISPUTED.items():
        mark = "" if key in keys else "  [ЗАПИСИ НЕТ В ДАННЫХ]"
        print(f"  {kind:<10} {key:<20} {reason}{mark}")

    # Такие записи появятся, например, после новой выгрузки: их надо разобрать вручную
    print()
    print(f"Не подошло ни одно правило и нет ручного решения: {len(unsorted_records)}")
    for r in unsorted_records:
        print(f"  {r['osm_type']}/{r['osm_id']}: {r.get('name')}")

    print()
    print("is_private неизвестен (null):")
    for r in records:
        if r["is_private"] is None:
            print(f"  {r['school_kind']:<10} {r['osm_type']}/{r['osm_id']}: {r.get('name')}")


if __name__ == "__main__":
    main()
