# TogglSync

Automatically sync your Toggl Track time entries to Google Calendar events in real-time using webhooks.

## Implemented features

- Time entries appear in Google Calendar within 30 seconds (adjustable) via Toggl webhooks
- Calendar mapping: Route time entries to different calendars (with custom color) based on project, tag, workspace, or organization
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

#### Create Calendar Mappings (Optional)

Route entries to specific calendars based on project, tag, workspace, or organization:

1. Go to **Calendar Mappings** → **Add**
2. Select an entity and destination calendar
3. Optionally set a color


## License

MIT License
