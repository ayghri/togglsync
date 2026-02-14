"""Management command to set up Google Calendar OAuth."""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from sync.models import GoogleCal
from sync.services import GoogleCalendarError, GoogleCalendarService


class Command(BaseCommand):
    help = 'Set up Google Calendar OAuth and list available calendars for a user'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            required=True,
            help='Username to set up Google Calendar for',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            dest='list_calendars',
            help='List available Google Calendars',
        )
        parser.add_argument(
            '--import',
            action='store_true',
            dest='import_calendars',
            help='Import all Google Calendars into the database',
        )
        parser.add_argument(
            '--set-default',
            type=str,
            dest='default_calendar',
            help='Set a calendar as default by its Google Calendar ID',
        )

    def handle(self, *args, **options):
        # Get the user
        username = options['user']
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'User not found: {username}')

        self.user = user

        # Initialize Google Calendar service (will trigger OAuth if needed)
        self.stdout.write(f'Initializing Google Calendar connection for {username}...')

        try:
            gcal = GoogleCalendarService(user=user)
            # Access credentials to verify connection
            _ = gcal.credentials
        except GoogleCalendarError as e:
            raise CommandError(
                f'Failed to connect to Google Calendar: {e}\n'
                'Make sure the user has connected their Google Calendar via the admin OAuth flow.'
            )

        self.stdout.write(self.style.SUCCESS('Successfully connected to Google Calendar'))

        if options.get('list_calendars'):
            self.list_calendars(gcal)

        if options.get('import_calendars'):
            self.import_calendars(gcal)

        if options.get('default_calendar'):
            self.set_default_calendar(options['default_calendar'])

        if not any([
            options.get('list_calendars'),
            options.get('import_calendars'),
            options.get('default_calendar'),
        ]):
            self.stdout.write('\nAvailable commands:')
            self.stdout.write('  --list       List Google Calendars')
            self.stdout.write('  --import     Import calendars to database')
            self.stdout.write('  --set-default <id>  Set default calendar')

    def list_calendars(self, gcal: GoogleCalendarService):
        """List available Google Calendars."""
        self.stdout.write('\nAvailable Google Calendars:')

        try:
            calendars = gcal.get_all_calendars()
        except GoogleCalendarError as e:
            raise CommandError(f'Failed to list calendars: {e}')

        for cal in calendars:
            primary = ' (PRIMARY)' if cal.get('primary') else ''
            access = cal.get('accessRole', 'unknown')

            # Check if already imported for this user
            imported = GoogleCal.objects.filter(
                user=self.user,
                google_calendar_id=cal['id']
            ).exists()
            imported_marker = ' [imported]' if imported else ''

            self.stdout.write(
                f"  {cal['summary']}{primary}{imported_marker}"
            )
            self.stdout.write(f"    ID: {cal['id']}")
            self.stdout.write(f"    Access: {access}")

    def import_calendars(self, gcal: GoogleCalendarService):
        """Import Google Calendars into the database for this user."""
        self.stdout.write('\nImporting calendars...')

        try:
            calendars = gcal.get_all_calendars()
        except GoogleCalendarError as e:
            raise CommandError(f'Failed to list calendars: {e}')

        imported = 0
        skipped = 0

        for cal in calendars:
            # Only import calendars with write access
            access = cal.get('accessRole', '')
            if access not in ('owner', 'writer'):
                self.stdout.write(
                    f"  Skipping {cal['summary']} (read-only access)"
                )
                skipped += 1
                continue

            # Check if user already has a default calendar
            has_default = GoogleCal.objects.filter(user=self.user, is_default=True).exists()

            calendar, created = GoogleCal.objects.get_or_create(
                user=self.user,
                google_calendar_id=cal['id'],
                defaults={
                    'name': cal['summary'],
                    'is_default': cal.get('primary', False) and not has_default,
                },
            )

            if created:
                self.stdout.write(f"  Imported: {cal['summary']}")
                imported += 1
            else:
                # Update name if changed
                if calendar.name != cal['summary']:
                    calendar.name = cal['summary']
                    calendar.save(update_fields=['name'])
                    self.stdout.write(f"  Updated: {cal['summary']}")
                else:
                    self.stdout.write(f"  Already exists: {cal['summary']}")
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(f'\nImported {imported} calendars, skipped {skipped}')
        )

        # Check for default calendar
        if not GoogleCal.objects.filter(user=self.user, is_default=True).exists():
            self.stdout.write(
                self.style.WARNING(
                    '\nNo default calendar set. Use --set-default <id> to set one.'
                )
            )

    def set_default_calendar(self, calendar_id: str):
        """Set a calendar as the default for this user."""
        try:
            calendar = GoogleCal.objects.get(user=self.user, google_calendar_id=calendar_id)
        except GoogleCal.DoesNotExist:
            # Try to find by name
            try:
                calendar = GoogleCal.objects.get(user=self.user, name__iexact=calendar_id)
            except GoogleCal.DoesNotExist:
                raise CommandError(
                    f'Calendar not found: {calendar_id}\n'
                    'Use --list to see available calendars, or --import first.'
                )

        calendar.is_default = True
        calendar.save()

        self.stdout.write(
            self.style.SUCCESS(f'Set "{calendar.name}" as default calendar for {self.user.username}')
        )
