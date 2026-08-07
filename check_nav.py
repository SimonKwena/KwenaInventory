import django
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'gearroom.settings'
django.setup()
from django.conf import settings
settings.ALLOWED_HOSTS.append('testserver')
from django.test import Client
from django.contrib.auth import get_user_model
User = get_user_model()
user, created = User.objects.get_or_create(username='testuser2', defaults={'email': 'test2@example.com'})
user.set_password('testpass')
user.save()
c = Client()
c.login(username='testuser2', password='testpass')
resp = c.get('/inventory/')
print('Status:', resp.status_code)
content = resp.content.decode()
start = content.find('id="account-toggle"')
if start != -1:
    print('Button HTML:', content[start-200:start+300])
else:
    print('Button not found')
