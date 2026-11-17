# Уведомления о сторонних материалах

## alice-voice

Сценарии негативных HTTP-тестов и жизненного цикла listener'а в `tests/test_adapter.py` написаны по мотивам тестов
плагина **alice-voice** 0.3.2 (`plugins/alice-voice/test_bridge.py`, `test_sdk_http.py`, коммит `e38849e`):
неверный путь, чужой навык и пользователь, битые тело и поля, слишком длинный текст, слэш-команды и управляющие символы,
неверный метод, слишком большое тело, повтор того же запроса, отсутствие секретного пути в логах, запуск и остановка
listener'а. Код alice-voice не копировался: тесты написаны заново под модель жизненного цикла этого плагина (её ведёт
Hermes) и под его собственные тексты и коды ответов.

Лицензия alice-voice — MIT:

```
MIT License

Copyright (c) 2026 Ilya Balobanov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Тестовая зависимость aiohttp

`aiohttp` (Apache-2.0) ставится только для запуска тестов (`requirements-test.txt`) и в репозиторий не включена.
