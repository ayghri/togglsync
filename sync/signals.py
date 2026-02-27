from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserCredentials


@receiver(post_save, sender=User)
def create_user_credentials(sender, instance, created, **kwargs):
    if created:
        UserCredentials.objects.create(user=instance)
