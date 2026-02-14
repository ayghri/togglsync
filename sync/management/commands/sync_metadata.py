"""Management command to sync Toggl metadata (projects, tags, workspaces, orgs)."""

import secrets

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sync.models import (
    TogglConfig,
    TogglOrganization,
    TogglProject,
    TogglTag,
    TogglWorkspace,
)
from sync.services import TogglAPIError, TogglService


class Command(BaseCommand):
    help = 'Sync Toggl metadata (organizations, workspaces, projects, tags) for a user'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            required=True,
            help='Username to sync metadata for',
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

        # Get config from database
        toggl_config = TogglConfig.get_for_user(user)

        api_token = options.get('api_token') or (toggl_config.api_token if toggl_config else None)

        if not api_token:
            raise CommandError(
                f'Toggl API token is required. Configure it for user {username} '
                'in the admin or provide --api-token'
            )

        toggl = TogglService(api_token)
        self.user = user

        try:
            self.sync_organizations(toggl)
            self.sync_workspaces(toggl)
            self.sync_projects_and_tags(toggl)
        except TogglAPIError as e:
            raise CommandError(f'Failed to sync metadata: {e}')

        # Update last sync time
        if toggl_config:
            toggl_config.last_metadata_sync = timezone.now()
            toggl_config.save(update_fields=['last_metadata_sync'])

        self.stdout.write(self.style.SUCCESS(f'Metadata sync completed for {username}'))

    def sync_organizations(self, toggl: TogglService):
        """Sync organizations from Toggl."""
        self.stdout.write('Syncing organizations...')

        orgs = toggl.get_organizations()
        count = 0

        for org in orgs:
            TogglOrganization.objects.update_or_create(
                user=self.user,
                toggl_id=org['id'],
                defaults={'name': org['name']},
            )
            count += 1

        self.stdout.write(f'  Synced {count} organizations')

    def sync_workspaces(self, toggl: TogglService):
        """Sync workspaces from Toggl."""
        self.stdout.write('Syncing workspaces...')

        workspaces = toggl.get_workspaces()
        count = 0

        for ws in workspaces:
            # Look up organization object if organization_id is provided
            org = None
            if ws.get('organization_id'):
                from sync.models import TogglOrganization
                org = TogglOrganization.objects.filter(
                    user=self.user, toggl_id=ws['organization_id']
                ).first()

            workspace, created = TogglWorkspace.objects.update_or_create(
                user=self.user,
                toggl_id=ws['id'],
                defaults={
                    'name': ws['name'],
                    'organization': org,
                },
            )
            # Generate webhook token for new workspaces
            if not workspace.webhook_token:
                workspace.webhook_token = secrets.token_urlsafe(32)
                workspace.save(update_fields=['webhook_token'])
            count += 1

        self.stdout.write(f'  Synced {count} workspaces')

    def sync_projects_and_tags(self, toggl: TogglService):
        """Sync projects and tags for each workspace."""
        workspaces = TogglWorkspace.objects.filter(user=self.user)

        for workspace in workspaces:
            self.stdout.write(f'Syncing data for workspace: {workspace.name}')

            # Sync projects
            self.sync_projects(toggl, workspace.toggl_id)

            # Sync tags
            self.sync_tags(toggl, workspace.toggl_id)

    def sync_projects(self, toggl: TogglService, workspace_id: int):
        """Sync projects for a workspace."""
        from sync.models import TogglWorkspace

        # Look up workspace object
        workspace = TogglWorkspace.objects.filter(
            user=self.user, toggl_id=workspace_id
        ).first()
        if not workspace:
            self.stdout.write(
                self.style.WARNING(f'  Workspace {workspace_id} not found')
            )
            return

        try:
            projects = toggl.get_projects(workspace_id)
        except TogglAPIError as e:
            self.stdout.write(
                self.style.WARNING(f'  Failed to sync projects: {e}')
            )
            return

        count = 0
        for project in projects:
            TogglProject.objects.update_or_create(
                user=self.user,
                toggl_id=project['id'],
                defaults={
                    'workspace': workspace,
                    'name': project['name'],
                    'color': project.get('color'),
                    'active': project.get('active', True),
                },
            )
            count += 1

        self.stdout.write(f'  Synced {count} projects')

    def sync_tags(self, toggl: TogglService, workspace_id: int):
        """Sync tags for a workspace."""
        from sync.models import TogglWorkspace

        # Look up workspace object
        workspace = TogglWorkspace.objects.filter(
            user=self.user, toggl_id=workspace_id
        ).first()
        if not workspace:
            self.stdout.write(
                self.style.WARNING(f'  Workspace {workspace_id} not found')
            )
            return

        try:
            tags = toggl.get_tags(workspace_id)
        except TogglAPIError as e:
            self.stdout.write(
                self.style.WARNING(f'  Failed to sync tags: {e}')
            )
            return

        if not tags:
            self.stdout.write('  No tags found')
            return

        count = 0
        for tag in tags:
            TogglTag.objects.update_or_create(
                user=self.user,
                toggl_id=tag['id'],
                defaults={
                    'workspace': workspace,
                    'name': tag['name'],
                },
            )
            count += 1

        self.stdout.write(f'  Synced {count} tags')
