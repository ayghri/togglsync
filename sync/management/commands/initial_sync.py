"""Management command to sync existing Toggl time entries to Google Calendar."""

from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sync.models import GoogleCal, TogglTimeEntry, TogglConfig
from sync.services import CalendarResolver, GoogleCalendarService, TogglAPIError, TogglService


class Command(BaseCommand):
    help = 'Sync existing Toggl time entries to Google Calendar for a user'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            required=True,
            help='Username to sync entries for',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days to sync (default: 30)',
        )
        parser.add_argument(
            '--start-date',
            type=str,
            help='Start date in YYYY-MM-DD format (overrides --days)',
        )
        parser.add_argument(
            '--end-date',
            type=str,
            help='End date in YYYY-MM-DD format (default: today)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be synced without actually syncing',
        )
        parser.add_argument(
            '--api-token',
            type=str,
            help='Toggl API token (default: from user config in database)',
        )

    def handle(self, *args, **options):
        # Get the user
        username = options['user']
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'User not found: {username}')

        self.user = user

        # Check for default calendar
        default_calendar = GoogleCal.get_default_for_user(user)
        if not default_calendar:
            raise CommandError(
                f'No default calendar configured for user {username}. '
                'Please create a calendar and set it as default in the admin.'
            )

        # Get config
        toggl_config = TogglConfig.get_for_user(user)
        api_token = (
            options.get('api_token')
            or (toggl_config.api_token if toggl_config else None)
        )

        if not api_token:
            raise CommandError(
                f'Toggl API token is required. Configure it for user {username} '
                'in the admin or provide --api-token'
            )

        # Determine date range
        end_date = options.get('end_date')
        if end_date:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            end_date = timezone.now().date()

        start_date = options.get('start_date')
        if start_date:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        else:
            start_date = end_date - timedelta(days=options['days'])

        self.stdout.write(f'Syncing entries for {username} from {start_date} to {end_date}')

        dry_run = options.get('dry_run', False)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no changes will be made'))

        # Fetch time entries
        toggl = TogglService(api_token)
        try:
            entries = toggl.get_time_entries(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        except TogglAPIError as e:
            raise CommandError(f'Failed to fetch time entries: {e}')

        self.stdout.write(f'Found {len(entries)} time entries')

        # Filter out already synced and running entries
        entries_to_sync = []
        for entry in entries:
            entry_id = entry.get('id')
            if not entry_id:
                continue

            # Skip running entries
            if not entry.get('stop'):
                continue

            # Skip already synced
            if TogglTimeEntry.objects.filter(user=user, toggl_id=entry_id).exists():
                continue

            entries_to_sync.append(entry)

        self.stdout.write(f'{len(entries_to_sync)} entries to sync (excluding already synced)')

        if not entries_to_sync:
            self.stdout.write(self.style.SUCCESS('No new entries to sync'))
            return

        if dry_run:
            for entry in entries_to_sync:
                self.stdout.write(
                    f"  [{entry['id']}] {entry.get('description', '(no description)')} "
                    f"({entry.get('start', 'N/A')})"
                )
            return

        # Sync entries
        gcal = GoogleCalendarService(user=user)
        resolver = CalendarResolver(user)
        synced_count = 0
        error_count = 0

        for entry in entries_to_sync:
            try:
                self.sync_entry(entry, gcal, resolver)
                synced_count += 1
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"Failed to sync entry {entry['id']}: {e}")
                )
                error_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Synced {synced_count} entries, {error_count} errors'
            )
        )

    def sync_entry(
        self,
        entry: dict,
        gcal: GoogleCalendarService,
        resolver: CalendarResolver,
    ):
        """Sync a single time entry to Google Calendar."""
        entry_id = entry['id']

        # Resolve calendar
        calendar = resolver.resolve(entry)
        if not calendar:
            self.stdout.write(
                self.style.WARNING(f'No calendar for entry {entry_id}, skipping')
            )
            return

        # Parse times
        start_str = entry.get('start', '').replace('Z', '+00:00')
        end_str = entry.get('stop', '').replace('Z', '+00:00')

        if not start_str or not end_str:
            return

        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)

        description = entry.get('description', '')
        summary = description or '(No description)'

        # Build event description
        event_description = self.build_description(entry)

        # Create Google Calendar event
        event = gcal.create_event(
            calendar_id=calendar.google_calendar_id,
            summary=summary,
            start=start,
            end=end,
            description=event_description,
            event_id=str(entry_id),
        )

        # Store synced entry
        TogglTimeEntry.objects.create(
            user=self.user,
            toggl_entry_id=entry_id,
            google_event_id=event['id'],
            calendar=calendar,
            description=description,
            start_time=start,
            end_time=end,
            project_id=entry.get('project_id'),
            tag_ids=entry.get('tag_ids', []),
        )

        self.stdout.write(f'  Synced entry {entry_id}: {summary[:40]}')

    def build_description(self, entry: dict) -> str:
        """Build event description from time entry data."""
        from sync.models import TogglProject, TogglTag

        lines = [f"Toggl Entry: {entry['id']}"]

        project_id = entry.get('project_id')
        if project_id:
            project = TogglProject.objects.filter(user=self.user, toggl_id=project_id).first()
            if project:
                lines.append(f'Project: {project.name}')

        tag_ids = entry.get('tag_ids', [])
        if tag_ids:
            tags = TogglTag.objects.filter(user=self.user, toggl_id__in=tag_ids)
            tag_names = [t.name for t in tags]
            if tag_names:
                lines.append(f'Tags: {", ".join(tag_names)}')

        if entry.get('billable'):
            lines.append('Billable: Yes')

        return '\n'.join(lines)
