"""Test suite for the Attendance app.

Covers models, helper utilities, view authentication/authorization, and a
handful of regression tests for bugs called out in AUDIT_REPORT.md
(B1 empty-markers crash, B2 override_date honoured, B4 today in context,
H404 graceful handling, take_attendance_image counter accuracy).

Heavy ML pipelines (RetinaFace / InsightFace) are NOT exercised here -- those
require model weights and GPU/CPU compute that are inappropriate for unit
tests. Pipeline-related logic is exercised only at the URL/auth layer.
"""
from __future__ import annotations

import datetime
from io import BytesIO
from unittest.mock import patch

import numpy as np
from PIL import Image as PILImage

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AttendanceRecord,
    Batch,
    Class,
    ClassroomOverride,
    Course,
    Enrollment,
    Identity,
    Section,
    Student,
    Teacher,
)

User = get_user_model()


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

def _make_teacher(username="prof", email="prof@example.com"):
    user = User.objects.create_user(username=username, password="pw12345!", email=email)
    teacher = Teacher.objects.create(user=user)
    return user, teacher


def _make_class(teacher, course_code="CS101", classroom="R-101", with_stream=False):
    course = Course.objects.create(course_code=course_code, title="Test Course")
    batch = Batch.objects.create(
        batch_code=f"FA20-{course_code}",
        batch_name="Fall 2020",
        program="CS",
        degree_level="Bachelor's",
        start_year=2020,
        end_year=2024,
    )
    klass = Class.objects.create(
        course=course,
        semester="Fall 2024",
        academic_year="2024-25",
        classroom=classroom,
        rtsp_stream_url="rtsp://example.com/test" if with_stream else "",
        is_active=True,
    )
    klass.batches.add(batch)
    klass.instructors.add(teacher)
    return klass, batch


def _make_student(reg_id, batch, section=None, embedding_file=None, active=True):
    return Student.objects.create(
        registration_id=reg_id,
        first_name="First" + reg_id,
        last_name="Last" + reg_id,
        batch=batch,
        section=section,
        face_embedding_file=embedding_file,
        is_active=active,
    )


# ──────────────────────────────────────────────────────────────────────
# Model-layer tests
# ──────────────────────────────────────────────────────────────────────

class ModelTests(TestCase):
    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.klass, self.batch = _make_class(self.teacher)
        self.section = Section.objects.create(batch=self.batch, section_name="A")
        self.student = _make_student("S001", self.batch, section=self.section)

    def test_teacher_str_and_availability(self):
        self.assertIn(self.user.username, str(self.teacher))
        self.assertTrue(self.teacher.is_available)
        self.teacher.faculty_status = Teacher.FacultyStatusChoices.ON_LEAVE
        self.assertFalse(self.teacher.is_available)

    def test_student_full_name_and_embedding_path(self):
        self.assertEqual(
            self.student.get_full_name(),
            f"FirstS001 LastS001",
        )
        # No embedding file -> path is None
        self.assertIsNone(self.student.get_embedding_path())
        self.student.face_embedding_file = "test.npy"
        self.student.save()
        self.assertTrue(
            self.student.get_embedding_path().endswith("test.npy")
        )

    def test_section_unique_within_batch(self):
        Section.objects.create(batch=self.batch, section_name="B")
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            Section.objects.create(batch=self.batch, section_name="A")

    def test_class_has_stream_property(self):
        self.assertFalse(self.klass.has_stream)
        self.klass.rtsp_stream_url = "rtsp://example.com/cam"
        self.klass.save()
        self.assertTrue(self.klass.has_stream)

    def test_classroom_override_unique_per_date(self):
        today = timezone.now().date()
        ClassroomOverride.objects.create(
            class_instance=self.klass,
            original_classroom="R-101",
            temporary_classroom="R-202",
            override_date=today,
            reason="camera dead",
            created_by=self.user,
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            ClassroomOverride.objects.create(
                class_instance=self.klass,
                original_classroom="R-101",
                temporary_classroom="R-303",
                override_date=today,
                reason="dup",
                created_by=self.user,
            )

    def test_attendance_record_unique_per_day(self):
        today = timezone.now().date()
        AttendanceRecord.objects.create(
            student=self.student,
            class_instance=self.klass,
            attendance_date=today,
            status=AttendanceRecord.StatusChoices.PRESENT,
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            AttendanceRecord.objects.create(
                student=self.student,
                class_instance=self.klass,
                attendance_date=today,
                status=AttendanceRecord.StatusChoices.LATE,
            )

    def test_eligible_students(self):
        eligible = self.klass.get_all_eligible_students()
        self.assertIn(self.student, eligible)


# ──────────────────────────────────────────────────────────────────────
# Helper / utility tests (pure functions, no ML)
# ──────────────────────────────────────────────────────────────────────

class HelperTests(TestCase):
    def test_l2_normalize(self):
        from .views import _l2_normalize
        v = np.array([3.0, 4.0])
        out = _l2_normalize(v)
        self.assertAlmostEqual(float(np.linalg.norm(out)), 1.0, places=5)

        # 2D batch
        m = np.array([[3.0, 4.0], [0.0, 0.0]])
        out2 = _l2_normalize(m)
        self.assertAlmostEqual(float(np.linalg.norm(out2[0])), 1.0, places=5)
        # Zero row stays finite (no NaN)
        self.assertTrue(np.all(np.isfinite(out2)))

    def test_crop_face_region_clamps_bounds(self):
        from .views import crop_face_region
        img = PILImage.new("RGB", (200, 200), color=(128, 128, 128))
        # Bbox extends partly outside the image
        crop = crop_face_region(img, np.array([180, 180, 250, 250]))
        # Output may still be returned (clamped), but if it can't form a region, None.
        self.assertTrue(crop is None or crop.size[0] > 0)

    def test_crop_face_region_invalid_box(self):
        from .views import crop_face_region
        img = PILImage.new("RGB", (100, 100), color=(0, 0, 0))
        # Inverted box -> None
        self.assertIsNone(crop_face_region(img, np.array([90, 90, 10, 10])))
        # Too-short bbox -> None
        self.assertIsNone(crop_face_region(img, np.array([1, 2])))

    def test_encode_pil_image_base64_roundtrip(self):
        from .views import encode_pil_image_base64
        img = PILImage.new("RGB", (10, 10), color=(255, 0, 0))
        encoded = encode_pil_image_base64(img)
        self.assertTrue(encoded.startswith("data:image/jpeg;base64,"))

    def test_create_image_augmentations_count(self):
        from .views import create_image_augmentations
        img = PILImage.new("RGB", (50, 50), color=(100, 100, 100))
        out = create_image_augmentations(img)
        self.assertEqual(len(out), 6)
        for aug, kind in out:
            self.assertIsInstance(kind, str)
            self.assertIsInstance(aug, PILImage.Image)


# ──────────────────────────────────────────────────────────────────────
# View-layer auth & regression tests
# ──────────────────────────────────────────────────────────────────────

class AuthAndContextTests(TestCase):
    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.other_user, self.other_teacher = _make_teacher(
            username="other", email="other@example.com"
        )
        self.klass, self.batch = _make_class(self.teacher)
        self.client.force_login(self.user)

    def test_login_redirects_authenticated_user(self):
        resp = self.client.get(reverse("Attendance:login"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("Attendance:instructor_dashboard"), resp.url)

    def test_dashboard_includes_today_in_context(self):
        # Regression for B4/U1: 'today' must be in context
        resp = self.client.get(reverse("Attendance:instructor_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["today"], timezone.now().date())

    def test_attendance_view_includes_today_and_max_upload(self):
        # Regression for B4/U1 + U2
        resp = self.client.get(
            reverse("Attendance:pro_attend") + f"?class_id={self.klass.pk}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["today"], timezone.now().date())
        self.assertIn("ATTENDANCE_MAX_UPLOAD_MB", resp.context)

    def test_logout_redirects(self):
        resp = self.client.get(reverse("Attendance:logout"))
        self.assertEqual(resp.status_code, 302)


class HttpAuthorizationTests(TestCase):
    """Verify that endpoints reject unauthorized class access cleanly (not 404 HTML)."""

    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.other_user, self.other_teacher = _make_teacher(
            username="other", email="other@example.com"
        )
        self.klass, _ = _make_class(self.teacher)
        self.other_klass, _ = _make_class(
            self.other_teacher, course_code="CS999", classroom="X-999"
        )

    def test_take_attendance_rejects_unauth_class(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("Attendance:take_attendance"),
            {"class_id": self.other_klass.pk},
        )
        # Must be JSON with 403, not an HTML 404 page (Http404 leaking).
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_take_attendance_invalid_class_id(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("Attendance:take_attendance"),
            {"class_id": "not-a-number"},
        )
        # ValueError must be caught and yield JSON 403, not 500.
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_set_classroom_override_rejects_missing_class(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("Attendance:set_classroom_override"), {})
        self.assertEqual(resp.status_code, 400)

    def test_pipeline_retrain_requires_arg(self):
        # Regression: retrain endpoint previously processed all classes when called
        # with empty body. Now it must reject.
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("Attendance:pipeline_retrain"),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


# ──────────────────────────────────────────────────────────────────────
# Bug regression tests
# ──────────────────────────────────────────────────────────────────────

class CSVDownloadRegressionTests(TestCase):
    """B1: max(set([])) crash on dates with no marker entries."""

    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.klass, self.batch = _make_class(self.teacher)
        self.student = _make_student("S100", self.batch)
        Enrollment.objects.create(student=self.student, class_instance=self.klass)
        self.client.force_login(self.user)

    def test_download_with_no_records(self):
        # No AttendanceRecord rows -> session-wise loop should not crash.
        resp = self.client.get(
            reverse("Attendance:download_attendance") + f"?class_id={self.klass.pk}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])

    def test_download_with_record_no_marker(self):
        # An attendance record present but without `marked_by` (system marked)
        AttendanceRecord.objects.create(
            student=self.student,
            class_instance=self.klass,
            attendance_date=timezone.now().date(),
            status=AttendanceRecord.StatusChoices.PRESENT,
            marked_by=None,
        )
        resp = self.client.get(
            reverse("Attendance:download_attendance") + f"?class_id={self.klass.pk}"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8-sig", errors="ignore")
        # N/A appears as fallback for empty marker list
        self.assertIn("N/A", body)


class ClassroomOverrideRegressionTests(TestCase):
    """B2: override_date from POST is honoured rather than always today()."""

    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.klass, _ = _make_class(self.teacher)
        self.client.force_login(self.user)

    def test_future_override_date_honoured(self):
        future = (timezone.now().date() + datetime.timedelta(days=3)).isoformat()
        resp = self.client.post(
            reverse("Attendance:set_classroom_override"),
            {
                "class_id": self.klass.pk,
                "action": "set",
                "temporary_classroom": "R-555",
                "override_date": future,
                "reason": "AC repair",
            },
        )
        self.assertEqual(resp.status_code, 200)
        override = ClassroomOverride.objects.get(class_instance=self.klass)
        self.assertEqual(override.override_date.isoformat(), future)

    def test_invalid_date_falls_back_to_today(self):
        resp = self.client.post(
            reverse("Attendance:set_classroom_override"),
            {
                "class_id": self.klass.pk,
                "action": "set",
                "temporary_classroom": "R-777",
                "override_date": "not-a-date",
                "reason": "x",
            },
        )
        self.assertEqual(resp.status_code, 200)
        override = ClassroomOverride.objects.get(class_instance=self.klass)
        self.assertEqual(override.override_date, timezone.now().date())


class MultiFrameFusionDedupTests(TestCase):
    """Invariant: each physical face -> one student, each student -> one face."""

    def test_bbox_iou_basics(self):
        from .views import _bbox_iou
        a = [0, 0, 100, 100]
        b = [0, 0, 100, 100]
        self.assertAlmostEqual(_bbox_iou(a, b), 1.0, places=5)
        self.assertEqual(_bbox_iou(a, [200, 200, 300, 300]), 0.0)
        # 50% overlap on one axis -> known IoU
        self.assertGreater(_bbox_iou(a, [50, 0, 150, 100]), 0.3)
        # Empty / missing bboxes -> 0
        self.assertEqual(_bbox_iou(None, a), 0.0)
        self.assertEqual(_bbox_iou(a, [1, 2]), 0.0)

    @patch("Attendance.views.RETINAFACE_AVAILABLE", True)
    @patch("Attendance.views.INSIGHTFACE_AVAILABLE", True)
    @patch("Attendance.views.FACE_EMBEDDER", new=object())
    @patch("Attendance.views.TARGET_EMBEDDINGS", new={1: np.zeros((1, 512))})
    @patch("Attendance.views._process_detected_faces")
    def test_same_face_across_frames_collapses_to_single_winner(self, mock_proc):
        """One physical face seen in three frames, identified differently each time,
        must collapse to ONE recognition (the highest score) -- not three."""
        from .views import _process_multi_frame
        # Three frames, same bbox (same physical face), different student matches.
        mock_proc.side_effect = [
            ([{"student_pk": 10, "name": "A", "image": "a", "score": 0.55,
               "bbox": [100, 100, 200, 200]}], []),
            ([{"student_pk": 20, "name": "B", "image": "b", "score": 0.85,
               "bbox": [102, 101, 199, 201]}], []),
            ([{"student_pk": 30, "name": "C", "image": "c", "score": 0.65,
               "bbox": [101, 100, 200, 200]}], []),
        ]
        # Three dummy frames -- shape doesn't matter, mock_proc ignores them.
        frames = [np.zeros((10, 10, 3), dtype=np.uint8) for _ in range(3)]
        recognized, unidentified = _process_multi_frame(
            frames, PILImage.new("RGB", (10, 10))
        )
        self.assertEqual(len(recognized), 1, "one physical face should yield one match")
        self.assertEqual(recognized[0]["student_pk"], 20)  # highest score wins
        # The other two recognitions are demoted to unidentified.
        self.assertEqual(len(unidentified), 2)

    @patch("Attendance.views.RETINAFACE_AVAILABLE", True)
    @patch("Attendance.views.INSIGHTFACE_AVAILABLE", True)
    @patch("Attendance.views.FACE_EMBEDDER", new=object())
    @patch("Attendance.views.TARGET_EMBEDDINGS", new={1: np.zeros((1, 512))})
    @patch("Attendance.views._process_detected_faces")
    def test_two_faces_claiming_same_student_demoted(self, mock_proc):
        """Two distinct physical faces both matching Student A: only the highest
        scoring one keeps the identification, the other is demoted."""
        from .views import _process_multi_frame
        mock_proc.side_effect = [
            ([
                {"student_pk": 10, "name": "A", "image": "left", "score": 0.7,
                 "bbox": [0, 0, 100, 100]},
                {"student_pk": 10, "name": "A", "image": "right", "score": 0.95,
                 "bbox": [400, 0, 500, 100]},
            ], []),
        ]
        frames = [np.zeros((10, 10, 3), dtype=np.uint8)]
        recognized, unidentified = _process_multi_frame(
            frames, PILImage.new("RGB", (10, 10))
        )
        self.assertEqual(len(recognized), 1)
        self.assertEqual(recognized[0]["image"], "right")  # higher score retained
        self.assertIn("left", unidentified)               # other face demoted

    @patch("Attendance.views.RETINAFACE_AVAILABLE", True)
    @patch("Attendance.views.INSIGHTFACE_AVAILABLE", True)
    @patch("Attendance.views.FACE_EMBEDDER", new=object())
    @patch("Attendance.views.TARGET_EMBEDDINGS", new={1: np.zeros((1, 512))})
    @patch("Attendance.views._process_detected_faces")
    def test_distinct_faces_distinct_students_all_kept(self, mock_proc):
        """Two clearly different physical faces matching different students:
        both must be kept (no false dedup)."""
        from .views import _process_multi_frame
        mock_proc.side_effect = [
            ([
                {"student_pk": 10, "name": "A", "image": "a", "score": 0.8,
                 "bbox": [0, 0, 100, 100]},
                {"student_pk": 20, "name": "B", "image": "b", "score": 0.75,
                 "bbox": [400, 0, 500, 100]},
            ], []),
            ([
                {"student_pk": 10, "name": "A", "image": "a2", "score": 0.7,
                 "bbox": [2, 1, 100, 100]},
                {"student_pk": 20, "name": "B", "image": "b2", "score": 0.85,
                 "bbox": [402, 1, 500, 100]},
            ], []),
        ]
        frames = [np.zeros((10, 10, 3), dtype=np.uint8) for _ in range(2)]
        recognized, _ = _process_multi_frame(
            frames, PILImage.new("RGB", (10, 10))
        )
        pks = sorted(r["student_pk"] for r in recognized)
        self.assertEqual(pks, [10, 20])


class TakeAttendanceImageCounterRegressionTests(TestCase):
    """saved_count must reflect newly-created records, not all matches."""

    def setUp(self):
        self.user, self.teacher = _make_teacher()
        self.klass, self.batch = _make_class(self.teacher)
        self.student = _make_student("S200", self.batch)
        Enrollment.objects.create(student=self.student, class_instance=self.klass)
        self.client.force_login(self.user)

    def _fake_image(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = BytesIO()
        PILImage.new("RGB", (40, 40), color=(20, 20, 20)).save(buf, format="JPEG")
        return SimpleUploadedFile("face.jpg", buf.getvalue(), content_type="image/jpeg")

    @patch("Attendance.views._wait_for_models", return_value=True)
    @patch("Attendance.views._process_detected_faces")
    def test_saved_count_distinguishes_already_marked(self, mock_proc, _ready):
        mock_proc.return_value = (
            [{"student_pk": self.student.pk, "name": self.student.get_full_name(),
              "image": "data:image/jpeg;base64,xxx", "score": 0.9}],
            [],
        )
        url = reverse("Attendance:take_attendance_image")
        # First request: should create -> saved_count=1
        r1 = self.client.post(url, {"class_id": self.klass.pk, "image": self._fake_image()})
        self.assertEqual(r1.status_code, 200)
        body1 = r1.json()
        self.assertIn("Newly saved: 1", body1["status"])
        self.assertIn("Already marked: 0", body1["status"])

        # Second request same day: existing record -> already_marked=1, saved=0
        r2 = self.client.post(url, {"class_id": self.klass.pk, "image": self._fake_image()})
        self.assertEqual(r2.status_code, 200)
        body2 = r2.json()
        self.assertIn("Newly saved: 0", body2["status"])
        self.assertIn("Already marked: 1", body2["status"])
