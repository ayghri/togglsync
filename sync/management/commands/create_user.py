"""Management command to create a new user with appropriate permissions."""

from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create a new user with permissions to manage their own sync configuration'

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
            user.is_staff = True  # Required to access admin
            user.save()

            # Grant permissions for sync app models
            self._grant_sync_permissions(user)
            self.stdout.write(self.style.SUCCESS(f'User "{username}" created with sync permissions'))

        self.stdout.write(f'\nNext steps for {username}:')
        self.stdout.write('  1. Login at /admin/')
        self.stdout.write('  2. Add Toggl API token in "Toggl Configurations"')
        self.stdout.write('  3. Connect Google Calendar via OAuth link')
        self.stdout.write('  4. Import calendars and set a default')
        self.stdout.write('  5. Setup webhooks in "Toggl Workspaces"')

    def _grant_sync_permissions(self, user):
        """Grant user permissions to manage their sync configuration."""
        from sync.models import (
            TogglConfig,
            GoogleCloudAPI,
            GoogleCal,
            EntityToCalMapping,
            TogglTimeEntry,
            TogglOrganization,
            TogglWorkspace,
            TogglProject,
            TogglTag,
        )

        # Define permissions per model
        # Note: UserScopedAdmin enforces object-level access (users can only see their own data)
        # These are model-level permissions (required to see the model in admin menu)
        model_permissions = {
            # User manages these directly
            TogglConfig: ['add', 'change', 'view'],  # No delete - need to keep config
            GoogleCal: ['add', 'change', 'delete', 'view'],
            EntityToCalMapping: ['add', 'change', 'delete', 'view'],

            # Google Cloud API - add/change for custom creds, view always
            GoogleCloudAPI: ['add', 'change', 'view'],

            # Synced from Toggl - view only
            TogglOrganization: ['view'],
            TogglProject: ['view'],
            TogglTag: ['view'],

            # Workspaces - view + change (for webhook setup via admin actions)
            TogglWorkspace: ['change', 'view'],

            # Sync history - view only
            TogglTimeEntry: ['view'],
        }

        permissions_added = []

        for model, actions in model_permissions.items():
            content_type = ContentType.objects.get_for_model(model)
            for action in actions:
                codename = f'{action}_{model._meta.model_name}'
                try:
                    perm = Permission.objects.get(
                        content_type=content_type,
                        codename=codename,
                    )
                    user.user_permissions.add(perm)
                    permissions_added.append(f'{action} {model._meta.model_name}')
                except Permission.DoesNotExist:
                    self.stdout.write(
                        self.style.WARNING(f'  Permission not found: {codename}')
                    )

        self.stdout.write(f'  Granted {len(permissions_added)} permissions')
