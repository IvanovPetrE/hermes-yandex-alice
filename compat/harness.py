"""Обвязка тестов совместимости (этап 6, срез «б») — только для `compat/`, в `yandex-alice/` не переносится.

Здесь разрешена тестовая обвязка Hermes (`gateway.run`, `tests.fakes.*`), которой нет в `public-api.md`: она поднимает
настоящий шлюз. Верхний уровень модуля Hermes не импортирует — импорты внутри функций, чтобы модуль грузился и без Hermes.
Запуск — только через `run.sh` (временные HOME/HERMES_HOME, `env -i`).
"""
from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "yandex-alice"
PLUGIN_NAME = "yandex-alice"


def temp_home() -> Path:
    """HERMES_HOME прогона; отказ, если он не во временном каталоге `hya-compat-*` (живой дом трогать нельзя)."""
    home = Path(os.environ["HERMES_HOME"]).resolve()
    if not any(p.name.startswith("hya-compat-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-compat-* temp dir")
    return home


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def install_plugin(home: Path) -> Path:
    """Копия плагина в `<home>/plugins/yandex-alice` — как ставит пользователь."""
    dest = home / "plugins" / PLUGIN_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(PLUGIN_SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest
