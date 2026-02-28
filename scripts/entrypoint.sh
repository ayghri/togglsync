#!/bin/sh
set -e

# Run migrations
python manage.py makemigrations
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Create or update admin user if DJANGO_ADMIN_PASSWORD is set
if [ -n "$DJANGO_ADMIN_PASSWORD" ]; then
    python -c "
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
username = os.environ.get('DJANGO_ADMIN_USER', 'admin')
password = os.environ['DJANGO_ADMIN_PASSWORD']
user, created = User.objects.get_or_create(username=username, defaults={'is_superuser': True, 'is_staff': True})
user.set_password(password)
user.is_superuser = True
user.is_staff = True
user.save()
print(f\"Admin user '{username}' {'created' if created else 'password updated'}\")
"
fi

# Start background worker
python manage.py qcluster &

# Start web server
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 config.wsgi:application
