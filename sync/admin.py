import json
import logging

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html
from google.oauth2.credentials import Credentials

from .models import UserCredentials
from .models import GoogleCal
from .models import EntityToCalMapping
from .models import TogglTimeEntry
from .models import TogglOrganization
from .models import TogglProject
from .models import TogglTag
from .models import TogglWorkspace
from .services import TogglAPIError, TogglService
from .tasks import (
    import_google_calendars_for_user,
    sync_toggl_metadata_for_user,
)

logger = logging.getLogger(__name__)


class UserScopedAdmin(admin.ModelAdmin):
    """
    Base admin that restricts users to only their own data.
    """

    def get_queryset(self, request):
        """Filter queryset to only show user's own records."""
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def has_view_permission(self, request, obj=None):
        # if request.user.is_superuser:
        #     return True
        if obj is not None:
            if obj.user_id != request.user.id:
                return False
        return super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        # if request.user.is_superuser:
        # return True
        if obj is not None:
            if obj.user_id != request.user.id:
                return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        """Block deleting other users' objects."""
        # and not request.user.is_superuser
        if obj is not None:
            if obj.user_id != request.user.id:
                return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        """Auto-assign user on create, prevent user change on edit."""
        if not change:  # Creating new object
            obj.user = request.user
        elif not request.user.is_superuser:
            # Prevent changing user field on existing objects
            obj.user = self.model.objects.get(pk=obj.pk).user
        super().save_model(request, obj, form, change)

    def get_exclude(self, request, obj=None):
        """Hide user field entirely from non-superusers."""
        exclude = list(super().get_exclude(request, obj) or [])
        if not request.user.is_superuser:
            if "user" not in exclude:
                exclude.append("user")
        return exclude

    def get_fieldsets(self, request, obj=None):
        """Remove user from fieldsets for non-superusers."""
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser:
            return fieldsets
        # Filter out 'user' from all fieldsets
        filtered = []
        for name, options in fieldsets:
            fields = options.get("fields", [])
            # Handle nested tuples/lists in fields
            new_fields = []
            for field in fields:
                if isinstance(field, (list, tuple)):
                    filtered_field = [f for f in field if f != "user"]
                    if filtered_field:
                        new_fields.append(
                            tuple(filtered_field)
                            if isinstance(field, tuple)
                            else filtered_field
                        )
                elif field != "user":
                    new_fields.append(field)
            if new_fields:
                filtered.append((name, {**options, "fields": new_fields}))
        return filtered

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Filter FK choices to user's own data."""
        if not request.user.is_superuser:
            # Filter any FK that points to a user-scoped model
            related_model = db_field.related_model
            if hasattr(related_model, "user"):
                kwargs["queryset"] = related_model.objects.filter(
                    user=request.user
                )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_list_display(self, request):
        """Add user column for superusers."""
        list_display = list(super().get_list_display(request))
        if request.user.is_superuser and "user" not in list_display:
            list_display.insert(1, "user")
        return list_display

    def get_list_filter(self, request):
        """Add user filter for superusers."""
        list_filter = list(super().get_list_filter(request))
        if request.user.is_superuser and "user" not in list_filter:
            list_filter = ["user"] + list_filter
        return list_filter


@admin.register(UserCredentials)
class UserCredsAdmin(UserScopedAdmin):
    change_list_template = "admin_user_credentials.html"
    list_display = ["__str__", "last_toggl_metadata_sync", "updated_at"]
    readonly_fields = [
        "google_oauth_display",
        "last_toggl_metadata_sync",
        "updated_at",
    ]
    actions = ["sync_metadata"]
    list_filter = []
    fieldsets = [
        (
            "Toggl API",
            {
                "fields": ["toggl_api_token", "timezone"],
            },
        ),
        (
            "Google OAuth",
            {
                "fields": ["google_oauth_display"],
            },
        ),
        (
            "Status",
            {
                "fields": ["last_toggl_metadata_sync", "updated_at"],
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="Google OAuth Credentials")
    def google_oauth_display(self, obj):
        """Display Google OAuth token and expiry."""
        if not obj.gauth_credentials_json:
            return "Not connected"

        try:
            cred_data = json.loads(obj.gauth_credentials_json)
            google_creds = Credentials.from_authorized_user_info(cred_data)

            # Mask the token
            token = google_creds.token or ""
            if len(token) > 16:
                masked_token = token[:8] + "****" + token[-8:]
            else:
                masked_token = "****"

            # Format expiry
            expiry = google_creds.expiry
            expiry_str = (
                expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else "Unknown"
            )

            return f"Token: {masked_token} | Expires: {expiry_str}"
        except Exception as e:
            return f"Error: {e}"

    def has_add_permission(self, request):
        # Allow add only if user doesn't have a config yet
        if request.user.is_superuser:
            return True
        return not UserCredentials.objects.filter(user=request.user).exists()

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.action(description="Sync metadata from Toggl")
    def sync_metadata(self, request, queryset):
        for config in queryset:
            sync_toggl_metadata_for_user(request, config.user)

    def changeform_view(
        self, request, object_id=None, form_url="", extra_context=None
    ):
        extra_context = extra_context or {}
        extra_context["show_save_and_continue"] = True
        return super().changeform_view(
            request, object_id, form_url, extra_context
        )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["toggl_profile_url"] = "https://track.toggl.com/profile"
        return super().changelist_view(request, extra_context)


# @admin.register(GoogleCloudAPI)
# class GoogleCloudAPIAdmin(UserScopedAdmin):
#     list_display = [
#         "__str__",
#         "has_custom_creds",
#         "is_connected",
#         "default_timezone",
#         "updated_at",
#     ]
#     list_filter = []
#     actions = ["import_calendars"]
#
#     @admin.display(description="Custom Creds", boolean=True)
#     def has_custom_creds(self, obj):
#         return obj.has_custom_credentials
#
#     @admin.display(description="Connected", boolean=True)
#     def is_connected(self, obj):
#         return obj.is_connected
#
#     def get_readonly_fields(self, request, obj=None):
#         """Make connection status readonly, credentials_json always hidden."""
#         if obj:
#             return [
#                 "connection_status",
#                 "client_secret_display",
#                 "created_at",
#                 "updated_at",
#             ]
#         return ["connection_status", "created_at", "updated_at"]
#
#     def get_fieldsets(self, request, obj=None):
#         """Show different fieldsets for create vs edit."""
#         if obj is None:
#             # Creating new - show custom credentials fields
#             return [
#                 (
#                     "Custom Google Cloud Credentials (Optional)",
#                     {
#                         "description": (
#                             "Provide your own Google Cloud OAuth credentials. "
#                             "Leave blank to use the default application credentials."
#                         ),
#                         "fields": ["client_id", "client_secret"],
#                     },
#                 ),
#                 (
#                     "Settings",
#                     {
#                         "fields": ["default_timezone"],
#                     },
#                 ),
#             ]
#         else:
#             # Editing existing
#             fieldsets = [
#                 (
#                     "Connection Status",
#                     {
#                         "fields": ["connection_status"],
#                     },
#                 ),
#                 (
#                     "Custom Google Cloud Credentials (Optional)",
#                     {
#                         "description": (
#                             "Provide your own Google Cloud OAuth credentials. "
#                             "Leave blank to use the default application credentials."
#                         ),
#                         "fields": ["client_id", "client_secret_display"],
#                     },
#                 ),
#                 (
#                     "Settings",
#                     {
#                         "fields": ["default_timezone"],
#                     },
#                 ),
#                 (
#                     "Timestamps",
#                     {
#                         "fields": ["created_at", "updated_at"],
#                         "classes": ["collapse"],
#                     },
#                 ),
#             ]
#             return fieldsets
#
#     def connection_status(self, obj):
#         """Display connection status without showing actual tokens."""
#         from django.utils.safestring import mark_safe
#
#         if obj.is_connected:
#             return mark_safe(
#                 '<span style="color: green;">✓ Connected to Google Calendar</span>'
#             )
#         return mark_safe(
#             '<span style="color: orange;">○ Not connected - click "Connect" to authenticate</span>'
#         )
#
#     connection_status.short_description = "Status"
#
#     def client_secret_display(self, obj):
#         """Display masked client secret."""
#         if obj.client_secret:
#             return "••••••••" + obj.client_secret[-4:]
#         return "(not set - using default)"
#
#     client_secret_display.short_description = "Client Secret"
#
#     def get_fields(self, request, obj=None):
#         """Override to use display field for client_secret when editing."""
#         if obj:
#             return [
#                 "connection_status",
#                 "client_id",
#                 "client_secret_display",
#                 "default_timezone",
#                 "created_at",
#                 "updated_at",
#             ]
#         return ["client_id", "client_secret", "default_timezone"]
#
#     def has_add_permission(self, request):
#         # Allow add so users can enter custom credentials before connecting
#         if request.user.is_superuser:
#             return True
#         return not GoogleCloudAPI.objects.filter(user=request.user).exists()
#
#     def has_delete_permission(self, request, obj=None):
#         return request.user.is_superuser
#
#     @admin.action(description="Import calendars from Google")
#     def import_calendars(self, request, queryset):
#         for config in queryset:
#             import_google_calendars_for_user(request, config.user)
#
#     def changelist_view(self, request, extra_context=None):
#         extra_context = extra_context or {}
#         config = GoogleCloudAPI.objects.filter(user=request.user).first()
#         if not config or not config.is_connected:
#             extra_context["connect_google_url"] = reverse(
#                 "sync:google_oauth_start"
#             )
#         return super().changelist_view(request, extra_context)
#
#     def change_view(self, request, object_id, form_url="", extra_context=None):
#         extra_context = extra_context or {}
#         config = GoogleCloudAPI.objects.filter(pk=object_id).first()
#         if config and not config.is_connected:
#             extra_context["connect_google_url"] = reverse(
#                 "sync:google_oauth_start"
#             )
#         return super().change_view(request, object_id, form_url, extra_context)


@admin.register(GoogleCal)
class CalendarAdmin(UserScopedAdmin):
    list_display = ["name", "calendar_id", "is_default", "created_at"]
    list_filter = ["is_default"]
    search_fields = ["name", "calendar_id"]
    readonly_fields = ["name", "calendar_id", "created_at"]
    actions = ["make_default", "import_from_google"]

    def get_readonly_fields(self, request, obj=None):
        """Make all fields except is_default readonly."""
        if obj:  # Editing an existing object
            return ["name", "calendar_id", "created_at"]
        return self.readonly_fields

    def has_add_permission(self, request):
        """Don't allow manual calendar creation - use import action instead."""
        return False

    # def has_delete_permission(self, request, obj=None):
    # """Only superusers can delete calendars."""
    # return request.user.is_superuser

    @admin.action(description="Set as default calendar")
    def make_default(self, request, queryset):
        # Filter to only user's calendars
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)
        if queryset.count() != 1:
            messages.error(request, "Please select exactly one calendar")
            return
        calendar = queryset.first()
        calendar.is_default = True
        calendar.save()
        messages.success(
            request, f'"{calendar.name}" is now the default calendar'
        )

    @admin.action(description="Import/refresh calendars from Google")
    def import_from_google(self, request, queryset):
        import_google_calendars_for_user(request, request.user)


class CalendarMappingForm(forms.ModelForm):
    """Custom form for CalendarMapping with entity dropdown."""

    entity = forms.ChoiceField(
        label="Entity",
        help_text="Select a project, tag, workspace, or organization to map",
    )

    class Meta:
        model = EntityToCalMapping
        fields = ["entity", "gcal", "color_name", "process_order"]
        widgets = {
            "color_name": forms.RadioSelect(),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Get user for building choices (not stored)
        user = user or getattr(self.instance, "user", None)

        if user:
            self._build_entity_choices(user)

        # Add color swatches to color choices using HTML
        color_field = self.fields.get("color_name")
        if color_field:
            from django.utils.safestring import mark_safe

            new_choices = []
            for (
                color_name,
                hex_color,
            ) in EntityToCalMapping.EVENT_COLORS.items():
                label = mark_safe(
                    f'<span style="display:inline-block;width:16px;height:16px;'
                    f"background-color:{hex_color};border:1px solid #999;"
                    f"border-radius:3px;margin-right:8px;vertical-align:middle;"
                    f'"></span>{color_name}'
                )
                new_choices.append((color_name, label))
            color_field.choices = new_choices

        # If editing existing mapping, set initial value
        if self.instance and self.instance.pk:
            self.fields["entity"].initial = (
                f"{self.instance.entity_type}:{self.instance.entity_id}"
            )

    def _build_entity_choices(self, user):
        """Build grouped choices from user's Toggl entities."""
        choices = [("", "-- Select an entity --")]

        # Projects
        projects = TogglProject.objects.filter(user=user, active=True).order_by(
            "name"
        )
        if projects:
            project_choices = [
                (f"project:{p.toggl_id}", p.name) for p in projects
            ]
            choices.append(("Projects", project_choices))

        # Tags
        tags = TogglTag.objects.filter(user=user).order_by("name")
        if tags:
            tag_choices = [(f"tag:{t.toggl_id}", t.name) for t in tags]
            choices.append(("Tags", tag_choices))

        # Workspaces
        workspaces = TogglWorkspace.objects.filter(user=user).order_by("name")
        if workspaces:
            ws_choices = [
                (f"workspace:{w.toggl_id}", w.name) for w in workspaces
            ]
            choices.append(("Workspaces", ws_choices))

        # Organizations
        orgs = TogglOrganization.objects.filter(user=user).order_by("name")
        if orgs:
            org_choices = [(f"organization:{o.toggl_id}", o.name) for o in orgs]
            choices.append(("Organizations", org_choices))

        self.fields["entity"].choices = choices

    def clean_entity(self):
        """Parse entity choice into type and ID."""
        value = self.cleaned_data.get("entity")
        if not value:
            raise forms.ValidationError("Please select an entity")

        try:
            entity_type, entity_id = value.split(":", 1)
            self.cleaned_data["_entity_type"] = entity_type
            self.cleaned_data["_entity_id"] = int(entity_id)
        except (ValueError, TypeError):
            raise forms.ValidationError("Invalid entity selection")

        return value

    def save(self, commit=True):
        """Set entity fields from the parsed choice."""
        instance = super().save(commit=False)

        entity_type = self.cleaned_data.get("_entity_type")
        entity_id = self.cleaned_data.get("_entity_id")

        instance.entity_type = entity_type
        instance.entity_id = entity_id

        # Get user from the selected calendar
        user = instance.gcal.user

        model_by_type = {
            "project": TogglProject,
            "tag": TogglTag,
            "workspace": TogglWorkspace,
            "organization": TogglOrganization,
        }

        Model = model_by_type.get(entity_type)
        entity = Model.objects.filter(user=user, toggl_id=entity_id).first()
        instance.entity_name = (
            entity.name if entity else f"{entity_type}:{entity_id}"
        )

        if commit:
            instance.save()
        return instance


@admin.register(EntityToCalMapping)
class CalendarMappingAdmin(UserScopedAdmin):
    form = CalendarMappingForm
    list_display = [
        "entity_type",
        "entity_name",
        "gcal",
        "color_display",
        "process_order",
    ]
    list_filter = ["entity_type", "gcal", "color_name"]
    search_fields = ["entity_name"]
    ordering = ["entity_type", "process_order", "entity_name"]
    actions = ["apply_mappings"]

    @admin.display(description="Color")
    def color_display(self, obj):
        """Display color as a visual swatch."""
        hex_color = obj.get_color_hex()
        if hex_color:
            return format_html(
                '<span style="display:inline-block;width:20px;height:20px;'
                "background-color:{};border:1px solid #ccc;border-radius:3px;"
                'vertical-align:middle;margin-right:5px;"></span> {}',
                hex_color,
                obj.color_name,
            )
        return "-"

    @admin.action(description="Apply mappings to past time entries")
    def apply_mappings(self, request, queryset):
        """
        Apply calendar and color mappings to existing synced time entries.

        Iterates through mappings from high to low process_order, so that
        high priority (high process_order) mappings apply last and override
        lower priority mappings.
        """
        from django_q.tasks import async_task

        user = request.user

        # Get all mappings for the user, ordered high to low process_order
        all_mappings = EntityToCalMapping.objects.filter(user=user).order_by(
            "-process_order"
        )

        if not all_mappings.exists():
            messages.warning(request, "No mappings configured")
            return

        total_tasks = 0

        # Process each mapping in order (high to low process_order)
        for mapping in all_mappings:
            # Find matching time entries using the model method
            matching_entries = mapping.find_matching_entries()

            if not matching_entries.exists():
                continue

            # Schedule async tasks to apply this mapping
            for entry in matching_entries:
                async_task(
                    "sync.tasks.apply_mapping_to_entry",
                    entry.id,
                    mapping.gcal.id,
                    mapping.get_color_id(),
                    task_name=f"apply_mapping_{entry.id}",
                )
                total_tasks += 1

        messages.success(
            request,
            f"Scheduled {total_tasks} tasks to apply mappings. "
            f"High priority (high process_order) mappings will apply last.",
        )

    def get_form(self, request, obj=None, **kwargs):
        """Pass the current user to the form."""
        Form = super().get_form(request, obj, **kwargs)

        class FormWithUser(Form):
            def __init__(self, *args, **form_kwargs):
                # Pass user as kwarg for building entity choices
                form_kwargs["user"] = request.user
                super().__init__(*args, **form_kwargs)

        return FormWithUser


# =============================================================================
# Toggl Metadata (with sync actions)
# =============================================================================


@admin.register(TogglOrganization)
class TogglOrganizationAdmin(UserScopedAdmin):
    list_display = ["name", "toggl_id", "updated_at"]
    search_fields = ["name"]
    readonly_fields = ["toggl_id", "name", "updated_at"]
    list_filter = []
    actions = ["refresh_from_api"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="Refresh all from Toggl API")
    def refresh_from_api(self, request, queryset):
        # Refresh for the requesting user
        sync_toggl_metadata_for_user(request, request.user)


@admin.register(TogglWorkspace)
class TogglWorkspaceAdmin(UserScopedAdmin):
    list_display = [
        "name",
        "toggl_id",
        "organization",
        "webhook_enabled",
        "webhook_token_short",
        "updated_at",
    ]
    list_filter = ["webhook_enabled"]
    search_fields = ["name"]
    readonly_fields = [
        "toggl_id",
        "name",
        "organization",
        "webhook_token",
        "updated_at",
    ]
    actions = ["refresh_from_api", "setup_webhook", "remove_webhook"]

    def webhook_token_short(self, obj):
        if obj.webhook_token:
            return obj.webhook_token[:8] + "..."
        return "-"

    webhook_token_short.short_description = "Webhook Token"

    def has_add_permission(self, request):
        return False

    @admin.action(description="Refresh all from Toggl API")
    def refresh_from_api(self, request, queryset):
        sync_toggl_metadata_for_user(request, request.user)

    @admin.action(description="Setup webhook for selected workspaces")
    def setup_webhook(self, request, queryset):
        # Filter to user's workspaces only
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)

        creds = request.user.credentials
        if not creds.toggl_api_token:
            messages.error(request, "Toggl API token not configured")
            return

        webhook_domain = settings.WEBHOOK_DOMAIN
        if not webhook_domain or webhook_domain == "localhost:8081":
            messages.error(request, "WEBHOOK_DOMAIN not configured in .env")
            return

        toggl = TogglService(creds.toggl_api_token)

        for workspace in queryset:
            # webhook_token is generated when workspace is synced
            if not workspace.webhook_token:
                messages.error(
                    request,
                    f'{workspace.name}: No webhook token. Run "Refresh all from Toggl API" first.',
                )
                continue

            callback_url = f"https://{webhook_domain}/webhook/toggl/{workspace.webhook_token}/"

            try:
                # First, check for existing webhooks (free users limited to 1)
                existing_webhooks = toggl.list_webhooks(workspace.toggl_id)

                if existing_webhooks:
                    # Check if any webhook already points to our callback URL
                    our_webhook = None
                    other_webhook = None

                    for wh in existing_webhooks:
                        if wh.get("url_callback") == callback_url:
                            our_webhook = wh
                            break
                        elif webhook_domain in wh.get("url_callback", ""):
                            # Points to our domain but different token - reuse it
                            our_webhook = wh
                            break
                        else:
                            other_webhook = wh

                    if our_webhook:
                        # Already set up, just update local state
                        workspace.webhook_subscription_id = our_webhook.get(
                            "subscription_id"
                        )
                        workspace.webhook_secret = our_webhook.get("secret")
                        workspace.webhook_enabled = our_webhook.get(
                            "enabled", False
                        )
                        workspace.save()

                        # Enable if disabled
                        if not workspace.webhook_enabled:
                            toggl.toggle_webhook(
                                workspace.toggl_id,
                                workspace.webhook_subscription_id,
                                enabled=True,
                            )
                            workspace.webhook_enabled = True
                            workspace.save()

                        messages.success(
                            request,
                            f"Webhook already exists for {workspace.name}, synced state",
                        )
                        continue

                    elif other_webhook:
                        # Another webhook exists (likely from another app or old setup)
                        # Free users can only have 1, so we need to update it
                        subscription_id = other_webhook.get("subscription_id")
                        result = toggl.update_webhook(
                            workspace.toggl_id,
                            subscription_id,
                            url_callback=callback_url,
                            description=f"togglsync-{workspace.user.username}-{workspace.toggl_id}",
                            enabled=True,
                        )
                        workspace.webhook_subscription_id = subscription_id
                        workspace.webhook_secret = result.get(
                            "secret"
                        ) or other_webhook.get("secret")
                        workspace.webhook_enabled = True
                        workspace.save()
                        messages.warning(
                            request,
                            f"{workspace.name}: Updated existing webhook (free plan limit)",
                        )
                        continue

                # No existing webhook, create new one
                result = toggl.create_webhook(
                    workspace_id=workspace.toggl_id,
                    callback_url=callback_url,
                    description=f"togglsync-{workspace.user.username}-{workspace.toggl_id}",
                )

                # Save the subscription details
                workspace.webhook_subscription_id = result.get(
                    "subscription_id"
                )
                workspace.webhook_secret = result.get("secret")
                workspace.webhook_enabled = True
                workspace.save()
                messages.success(
                    request, f"Webhook created for {workspace.name}"
                )

            except TogglAPIError as e:
                error_msg = str(e)
                if "limit" in error_msg.lower() or "402" in error_msg:
                    messages.error(
                        request,
                        f"{workspace.name}: Webhook limit reached. "
                        f'Run "Refresh all from Toggl API" to sync existing webhooks.',
                    )
                else:
                    messages.error(request, f"Failed for {workspace.name}: {e}")

    @admin.action(description="Remove webhook from selected workspaces")
    def remove_webhook(self, request, queryset):
        # Filter to user's workspaces only
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)

        creds = request.user.credentials
        if not creds.toggl_api_token:
            messages.error(request, "Toggl API token not configured")
            return

        toggl = TogglService(creds.toggl_api_token)

        for workspace in queryset.filter(webhook_subscription_id__isnull=False):
            try:
                toggl.delete_webhook(
                    workspace.toggl_id, workspace.webhook_subscription_id
                )
                workspace.webhook_subscription_id = None
                workspace.webhook_secret = None
                workspace.webhook_enabled = False
                # Keep webhook_token for potential re-use
                workspace.save()
                messages.success(
                    request, f"Webhook removed for {workspace.name}"
                )
            except TogglAPIError as e:
                messages.error(request, f"Failed for {workspace.name}: {e}")


@admin.register(TogglProject)
class TogglProjectAdmin(UserScopedAdmin):
    list_display = [
        "name",
        "toggl_id",
        "workspace",
        "active",
        "color",
        "updated_at",
    ]
    list_filter = ["active", "workspace"]
    search_fields = ["name"]
    readonly_fields = [
        "toggl_id",
        "name",
        "workspace",
        "color",
        "active",
        "updated_at",
    ]
    actions = ["refresh_from_api"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    # @admin.action(description="Refresh all from Toggl API")
    # def refresh_from_api(self, request, queryset):
    #     sync_toggl_metadata_for_user(request, request.user)


@admin.register(TogglTag)
class TagAdmin(UserScopedAdmin):
    list_display = ["name", "toggl_id", "workspace", "updated_at"]
    list_filter = ["workspace"]
    search_fields = ["name"]
    readonly_fields = ["toggl_id", "name", "workspace", "updated_at"]
    actions = ["refresh_from_api"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    # @admin.action(description="Refresh all from Toggl API")
    # def refresh_from_api(self, request, queryset):
    #     sync_toggl_metadata_for_user(request, request.user)


@admin.register(TogglTimeEntry)
class EntryAdmin(UserScopedAdmin):
    list_display = [
        "toggl_id",
        "short_description",
        "calendar",
        "synced_status",
        "start_time",
        "end_time",
        "updated_at",
    ]
    list_filter = ["calendar", "synced", "pending_deletion", "start_time"]
    search_fields = ["description", "toggl_id"]
    readonly_fields = [
        "toggl_id",
        "gcal_event_id",
        "calendar",
        "description",
        "start_time",
        "end_time",
        "project_id",
        "tag_ids",
        "synced",
        "pending_deletion",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "start_time"
    ordering = ["-start_time"]

    def short_description(self, obj):
        desc = obj.description or "(no description)"
        return desc[:50] + "..." if len(desc) > 50 else desc

    short_description.short_description = "Description"

    @admin.display(description="Status", ordering="synced")
    def synced_status(self, obj):
        """Display sync status."""
        if obj.pending_deletion:
            return "Pending deletion"
        elif obj.synced:
            return "Synced"
        else:
            return "Pending"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# Customize admin site
admin.site.site_header = "Toggl -> Google Calendar Sync"
admin.site.site_title = "TogglSync Admin"
admin.site.index_title = "Dashboard"
