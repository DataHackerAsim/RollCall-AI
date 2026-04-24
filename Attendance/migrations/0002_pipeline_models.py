# Attendance/migrations/0002_pipeline_models.py

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('Attendance', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExtractionSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_type', models.CharField(choices=[('STREAM', 'RTSP Stream'), ('UPLOAD', 'Uploaded Image')], max_length=6)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('frames_captured', models.PositiveIntegerField(default=0)),
                ('faces_extracted', models.PositiveIntegerField(default=0)),
                ('status', models.CharField(default='pending', help_text='pending / processing / completed / failed', max_length=20)),
                ('notes', models.TextField(blank=True)),
                ('class_instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='extraction_sessions', to='Attendance.class')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='Identity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(blank=True, default='', help_text='Human-assigned label (student name or temp identifier)', max_length=200)),
                ('auto_label', models.CharField(help_text="Auto-generated label like 'identity_001'", max_length=50)),
                ('embedding_file', models.CharField(blank=True, help_text='Path to the aggregated .npy embedding file', max_length=500)),
                ('representative_image', models.CharField(blank=True, help_text='Path to the best sample image for human reference', max_length=500)),
                ('sample_count', models.PositiveIntegerField(default=0)),
                ('is_labeled', models.BooleanField(default=False)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('needs_retraining', models.BooleanField(default=False, help_text='Flagged when samples change and embedding needs regeneration')),
                ('class_instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='identities', to='Attendance.class')),
                ('student', models.ForeignKey(blank=True, help_text='Linked after human labeling', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='identities', to='Attendance.student')),
            ],
            options={
                'ordering': ['class_instance', 'auto_label'],
                'unique_together': {('class_instance', 'auto_label')},
            },
        ),
        migrations.CreateModel(
            name='FaceSample',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image_path', models.CharField(help_text='Absolute path to face crop image', max_length=500)),
                ('embedding_path', models.CharField(blank=True, help_text='Path to individual .npy embedding', max_length=500)),
                ('quality_score', models.FloatField(default=0.0, help_text='Face quality score (0-100)')),
                ('is_valid', models.BooleanField(default=True, help_text='Marked False if human flags as bad sample')),
                ('source_frame', models.CharField(blank=True, help_text='Source frame filename', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('identity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='samples', to='Attendance.identity')),
                ('session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='Attendance.extractionsession')),
            ],
            options={
                'ordering': ['-quality_score', '-created_at'],
            },
        ),
    ]
