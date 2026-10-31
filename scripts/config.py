# Все допущения и настройки проекта. Меняем их здесь, а не в коде скриптов.

# --- Территория ---

# Название района для поиска границы в Nominatim (геокодер OpenStreetMap)
PLACE_NAME = "Адмиралтейский район, Санкт-Петербург, Россия"

# Запас вокруг района, в метрах. Он нужен, чтобы граница не искажала расчёты:
# ближайшая школа может стоять за каналом, а тень может падать от дома в соседнем районе.
# 1500 м — это примерно 15–20 минут пешком.
BUFFER_M = 1500

# --- Граф улиц ---

# Тип сети в osmnx: "walk" — дороги и дорожки, по которым может пройти пешеход
NETWORK_TYPE = "walk"

# --- Что скачиваем ---

# Колонки зданий: высота и этажность для 3D, название и адрес для проверки
BUILDING_COLUMNS = [
    "building",
    "building:levels",
    "height",
    "building:min_level",
    "min_height",
    "roof:levels",
    "roof:height",
    "name",
    "addr:street",
    "addr:housenumber",
]

# Колонки объектов: тип, название, адрес и данные о том, кто управляет объектом и открыт ли он
PLACE_COLUMNS = [
    "name",
    "amenity",
    "leisure",
    "healthcare",
    "operator",
    "operator:type",
    "access",
    "addr:street",
    "addr:housenumber",
]

# Слои данных: подпись для сводки, теги OSM для запроса и колонки, которые сохраняем.
# Имя слоя — это и имя файла: data/<имя>.geojson
LAYERS = {
    "buildings": {
        "label": "Здания",
        "tags": {"building": True},
        "columns": BUILDING_COLUMNS,
    },
    "schools": {
        "label": "Школы",
        "tags": {"amenity": "school"},
        "columns": PLACE_COLUMNS,
    },
    "kindergartens": {
        "label": "Детские сады",
        "tags": {"amenity": "kindergarten"},
        "columns": PLACE_COLUMNS,
    },
    "clinics": {
        "label": "Поликлиники",
        "tags": {"amenity": "clinic", "healthcare": "clinic"},
        "columns": PLACE_COLUMNS,
    },
    "parks": {
        "label": "Парки",
        "tags": {"leisure": "park"},
        "columns": PLACE_COLUMNS,
    },
    "playgrounds": {
        "label": "Детские площадки",
        "tags": {"leisure": "playground"},
        "columns": PLACE_COLUMNS,
    },
}

# --- Сервер и повторы при ошибках ---

# Сервер Overpass, откуда osmnx берёт данные OSM
OVERPASS_URL = "https://overpass-api.de/api"

# Сколько секунд ждать ответа сервера на один запрос
REQUEST_TIMEOUT_S = 180

# Сколько раз пробовать скачать слой, прежде чем сдаться
MAX_ATTEMPTS = 6

# Пауза перед повтором: начинаем с первой и удваиваем, но не дольше максимальной
RETRY_FIRST_PAUSE_S = 30
RETRY_MAX_PAUSE_S = 600
