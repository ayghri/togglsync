"""Management command to create a new user with appropriate permissions."""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create a new user with staff access to manage their own sync configuration'

    def add_arguments(self, parser):
        parser.add_argument('username', type=str, help='Username for the new user')
        parser.add_argument('--email', type=str, default='', help='Email address')
        parser.add_argument('--password', type=str, help='Password (will prompt if not provided)')
        parser.add_argument(
            '--superuser',
            action='store_true',
            help='Create as superuser (full admin access)',
        )

    def handle(self, *args, **options):
        username = options['username']
        email = options.get('email', '')
        password = options.get('password')
        is_superuser = options.get('superuser', False)

        # Check if user exists
        if User.objects.filter(username=username).exists():
            raise CommandError(f'User "{username}" already exists')

        # Prompt for password if not provided
        if not password:
            import getpass
            password = getpass.getpass(f'Password for {username}: ')
            password2 = getpass.getpass('Confirm password: ')
            if password != password2:
                raise CommandError('Passwords do not match')

        if not password:
            raise CommandError('Password is required')

        # Create user
        if is_superuser:
            user = User.objects.create_superuser(username, email, password)
            self.stdout.write(self.style.SUCCESS(f'Superuser "{username}" created'))
        else:
            user = User.objects.create_user(username, email, password)
            user.is_staff = True
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Staff user "{username}" created'))

        self.stdout.write(f'\nNext steps for {username}:')
        self.stdout.write('  1. Login at /admin/')
        self.stdout.write('  2. Add Toggl API token in Credentials')
        self.stdout.write('  3. Connect Google Calendar via the dashboard')
        self.stdout.write('  4. Sync Toggl metadata')
        self.stdout.write('  5. Setup webhooks in Toggl Workspaces')
