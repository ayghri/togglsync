import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SyncConfig(AppConfig):
    name = 'sync'

    def ready(self):
        """Import signals and register periodic schedules."""
        import sync.signals  # noqa

        from django.db.models.signals import post_migrate
        post_migrate.connect(self._setup_schedules, sender=self)

    @staticmethod
    def _setup_schedules(sender, **kwargs):
        """Set up django-q periodic schedules after migrations complete."""
        from sync.tasks import ensure_periodic_schedules
        try:
            ensure_periodic_schedules()
        except Exception as e:
            logger.warning(f"Could not set up periodic schedules: {e}")
