import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SyncConfig(AppConfig):
    name = 'sync'

    def ready(self):
        import sync.signals  # noqa

        from django.db.models.signals import post_migrate
        post_migrate.connect(self._setup_schedules, sender=self)

    @staticmethod
    def _setup_schedules(sender, **kwargs):
        try:
            from django_q.models import Schedule

            from django.conf import settings
            Schedule.objects.update_or_create(
                name="validate_synced_events",
                defaults={
                    "func": "sync.tasks.validate_synced_events",
                    "schedule_type": Schedule.MINUTES,
                    "minutes": getattr(settings, 'SYNC_VALIDATE_INTERVAL', 10),
                },
            )

            # Clean up old schedules
            Schedule.objects.filter(name="process_unsynced_entries").delete()

            logger.info("Periodic schedules ensured")
        except Exception as e:
            logger.warning(f"Could not set up periodic schedules: {e}")
