from django.apps import AppConfig


class SyncConfig(AppConfig):
    name = 'sync'

    def ready(self):
        """Import signals when app is ready."""
        import sync.signals  # noqa
