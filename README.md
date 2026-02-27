# TogglSync

Automatically sync your Toggl Track time entries to Google Calendar events in real-time using webhooks.

## Implemented features

- Time entries synced to Google Calendar in real-time via Toggl webhooks and Django Q background tasks
- Color mapping: Assign Google Calendar event colors based on project, tag, workspace, or organization (priority order)
- Periodic validation: Checks synced events against Google Calendar and re-syncs discrepancies
- Multi-user support: Each user manages their own Toggl API token

## Quick Start

> **For deployment instructions, troubleshooting, and development setup, see [DEPLOY.md](./DEPLOY.md)**

### Requirements

- Docker/Podman with compose
- A publicly accessible HTTPS domain (for webhooks and OAuth)
- Google Cloud project with Calendar API enabled

### 1. Deploy the Application

```bash
# Clone repository
git clone https://github.com/yourusername/togglsync.git
cd togglsync

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Start with Docker
docker-compose up -d

# Create admin user
docker-compose exec togglsync python manage.py create_user admin --superuser
```

### 2. Access the Admin Interface

Go to `https://your-domain.com/` and login with your admin credentials.

I recommend using non-admin user to sync things, make sure the added user has permissions to modify Sync related models.

### 3. Configure Your Account

#### Connect Google Calendar

1. Go to `https://your-domain.com/`
2. Click **Connect Google Calendar**
3. Complete the OAuth flow
4. Click **Import calendars from Google**

#### Add Toggl API Token

1. Go to **User Credentials** → **Add**
2. Enter your API token from [Toggl Profile](https://track.toggl.com/profile)
3. Save, then click **Sync metadata from Toggl**

#### Set Default Calendar

1. Go to **Calendars**
2. Select a calendar → **Set as default calendar**

#### Enable Webhooks

1. Go to **Toggl Workspaces**
2. Select workspaces → **Setup webhook for selected workspaces**

#### Create Color Mappings (Optional)

Assign Google Calendar event colors based on project, tag, workspace, or organization. Priority: tag > project > workspace > organization.

1. Go to **Color Mappings** → **Add**
2. Select entity type, entity, and color
3. Set process order (lower = higher priority among same type)


## License

MIT License
