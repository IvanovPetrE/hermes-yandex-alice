"""Совместимость: валидация манифеста и регистрация платформы в настоящем Hermes (этап 6, срез «б»).

Запуск — `run.sh <checkout> <python> P`. Без Hermes модуль падает при импорте (не пропускается).
"""
import unittest

import harness


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.home = harness.temp_home()
        cls.plugin_dir = harness.install_plugin(cls.home)

    def test_validate(self) -> None:
        from hermes_cli.plugin_validate import validate_plugin_dir

        report = validate_plugin_dir(self.plugin_dir)
        failed = [(name, detail) for name, ok, detail in report.checks if not ok]
        self.assertEqual(failed, [])
        self.assertEqual(report.exit_code, 0)
        # Предупреждения (private-import, security scan caution) тоже провал: HERMESYAND-19.
        self.assertEqual(report.warnings, [])

    def test_registration(self) -> None:
        (self.home / "config.yaml").write_text("plugins:\n  enabled:\n    - yandex-alice\n", encoding="utf-8")
        from gateway.platform_registry import platform_registry
        from hermes_cli.plugins import discover_plugins

        discover_plugins(force=True)
        entry = platform_registry.get("yandex_alice")
        self.assertIsNotNone(entry, "платформа yandex_alice не зарегистрирована")
        self.assertEqual(entry.source, "plugin")
        self.assertEqual(entry.display_tier, "minimal")
        self.assertEqual(entry.allowed_users_env, "ALICE_OWNER_USER_ID")


if __name__ == "__main__":
    unittest.main()
