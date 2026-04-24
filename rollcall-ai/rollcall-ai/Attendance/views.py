# Attendance/views.py

# --- Standard Python Imports ---
from __future__ import annotations
import os
import base64
import csv
import logging
import time
import json
import sys
import threading
from io import BytesIO
from contextlib import contextmanager
from typing import Optional, Tuple, List, Dict, Any
import traceback
import hashlib
from PIL import ImageOps, ImageFilter
import random
import pytz
from collections import defaultdict, OrderedDict
from datetime import datetime, date

# --- Third-Party Imports ---
import cv2
import numpy as np
from PIL import Image, ImageEnhance, UnidentifiedImageError

# Suppress noisy FFMPEG H.264 decode warnings from RTSP streams
# These are normal packet-loss artifacts, not real errors
os.environ['OPENCV_LOG_LEVEL'] = 'ERROR'
os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '-8'  # AV_LOG_QUIET

# --- Machine Learning Imports ---
try:
    from retinaface import RetinaFace
    RETINAFACE_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).critical(f"RetinaFace import failed: {e}. Detection will not work.")
    RETINAFACE_AVAILABLE = False
    RetinaFace = None

try:
    from insightface.app import FaceAnalysis
    from sklearn.metrics.pairwise import cosine_similarity
    INSIGHTFACE_AVAILABLE = True
    FaceAnalysisType = FaceAnalysis
except ImportError as e:
    logging.getLogger(__name__).critical(f"InsightFace or scikit-learn import failed: {e}. Recognition will not work.")
    INSIGHTFACE_AVAILABLE = False
    FaceAnalysis = None
    cosine_similarity = None
    FaceAnalysisType = type(None)

# --- Django Imports ---
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, StreamingHttpResponse, HttpResponse, HttpRequest, HttpResponseBadRequest, HttpResponseServerError, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

# --- App Model Imports ---
from .models import Student, Teacher, Course, Batch, Class, AttendanceRecord, Section, Enrollment, ClassroomOverride

# --- Globals & Setup ---
logger = logging.getLogger(__name__)
User = get_user_model()

# --- Configuration Constants ---
SIMILARITY_THRESHOLD: float = getattr(settings, 'ATTENDANCE_SIMILARITY_THRESHOLD', 0.4)
INSIGHTFACE_MODEL_NAME: str = 'buffalo_l'
IMG_WIDTH: int = getattr(settings, 'ATTENDANCE_CROP_WIDTH', 100)
IMG_HEIGHT: int = getattr(settings, 'ATTENDANCE_CROP_HEIGHT', 100)
MIN_BRIGHTNESS: float = getattr(settings, 'ATTENDANCE_MIN_BRIGHTNESS', 0.6)
MAX_BRIGHTNESS: float = getattr(settings, 'ATTENDANCE_MAX_BRIGHTNESS', 0.9)
STREAM_RETRY_DELAY: int = getattr(settings, 'ATTENDANCE_STREAM_RETRY_DELAY', 5)
STREAM_FRAME_DELAY: float = 1 / getattr(settings, 'ATTENDANCE_STREAM_FPS', 25)
STREAM_READ_ATTEMPTS: int = getattr(settings, 'ATTENDANCE_STREAM_READ_ATTEMPTS', 10)
MAX_UPLOAD_SIZE_MB: int = getattr(settings, 'ATTENDANCE_MAX_UPLOAD_MB', 60)
FACE_DETECTION_THRESHOLD: float = 0.5

# --- Accuracy Enhancement Constants ---
MULTI_FRAME_COUNT: int = getattr(settings, 'ATTENDANCE_MULTI_FRAME_COUNT', 3)
MULTI_FRAME_DELAY: float = 0.15  # seconds between frame captures
MIN_FACE_SIZE: int = getattr(settings, 'ATTENDANCE_MIN_FACE_SIZE', 30)  # min face width in pixels
FACE_QUALITY_SHARPNESS_THRESHOLD: float = 20.0  # Laplacian variance threshold
CLAHE_CLIP_LIMIT: float = 2.5
CLAHE_TILE_SIZE: int = 8
CROP_MARGIN_FACTOR: float = 0.35  # expand bbox by 35% for better InsightFace alignment

# --- Face Detector & Embedder Initialization ---
FACE_DETECTOR = None
FACE_EMBEDDER: Optional[FaceAnalysisType] = None # type: ignore
TARGET_EMBEDDINGS: Dict[int, np.ndarray] = {}
STUDENT_PK_TO_NAME: Dict[int, str] = {}

# Thread synchronization
_embeddings_lock = threading.Lock()
_models_lock = threading.Lock()
_models_ready = threading.Event()  # Set when models are loaded, warmed up, and embeddings loaded


def initialize_face_models():
    """Load model weights and prepare. Does NOT run warmup (that's separate)."""
    global FACE_EMBEDDER, FACE_DETECTOR
    
    if not (INSIGHTFACE_AVAILABLE and FaceAnalysis is not None):
        logger.error("Cannot initialize models: InsightFace is not available.")
        return False
    
    with _models_lock:
        if FACE_EMBEDDER is not None:
            return True
        try:
            model_root = os.path.expanduser("~/.insightface")
            os.makedirs(model_root, exist_ok=True)
            
            FACE_EMBEDDER = FaceAnalysis(
                name=INSIGHTFACE_MODEL_NAME, root=model_root,
                providers=['CPUExecutionProvider'], download=False
            )
            try:
                FACE_EMBEDDER.prepare(ctx_id=0, det_size=(640, 640))
            except Exception:
                FACE_EMBEDDER.prepare(ctx_id=0)
            
            if RETINAFACE_AVAILABLE and RetinaFace is not None:
                FACE_DETECTOR = RetinaFace
            
            logger.info("Face models loaded")
            return True
        except Exception as e:
            logger.exception(f"[CRITICAL] Face model init failed: {e}")
            FACE_EMBEDDER = None
            FACE_DETECTOR = None
            return False


def _background_startup():
    """Runs in a daemon thread: init models, warmup ONNX, load embeddings.
    Sets _models_ready when done so attendance views can proceed immediately."""
    try:
        print("[Attendance] Background startup: loading models...")
        start = time.time()
        
        if not initialize_face_models():
            print("[Attendance] ERROR: Model initialization failed!")
            return
        
        print("[Attendance] Models loaded. Running ONNX warmup...")
        _run_warmup()
        
        print("[Attendance] Loading student embeddings...")
        load_embeddings_from_db()
        
        elapsed = time.time() - start
        print(f"[Attendance] Ready! Startup took {elapsed:.1f}s. {len(TARGET_EMBEDDINGS)} student embeddings loaded.")
    except Exception as e:
        print(f"[Attendance] Background startup error: {e}")
        logger.error(f"Background startup error: {e}")
    finally:
        _models_ready.set()


def _wait_for_models(timeout: float = 45.0) -> bool:
    """Ensure models are initialized, warmed up, and embeddings loaded.
    Blocks until ready. Only returns False if InsightFace is truly broken/missing.
    
    The detection overlay UI already shows a spinner, so blocking here is fine --
    the user sees 'Processing Attendance...' not a frozen page."""
    
    # Fast path: already ready
    if _models_ready.is_set() and FACE_EMBEDDER is not None:
        return True
    
    # Give background thread time to finish (it's probably almost done)
    if not _models_ready.is_set():
        logger.info("Waiting for background model initialization...")
        _models_ready.wait(timeout=timeout)
    
    # If background thread succeeded, we're done
    if FACE_EMBEDDER is not None:
        # Embeddings might still be loading -- ensure they're loaded
        if not TARGET_EMBEDDINGS:
            load_embeddings_from_db()
        return True
    
    # Background thread failed or never started. Initialize inline.
    logger.warning("Background init incomplete. Initializing models inline...")
    try:
        if not initialize_face_models():
            logger.error("Face model initialization failed -- InsightFace may not be installed")
            return False
        _run_warmup()
        load_embeddings_from_db()
        _models_ready.set()
        return FACE_EMBEDDER is not None
    except Exception as e:
        logger.error(f"Inline initialization failed: {e}")
        _models_ready.set()  # Unblock future callers
        return False


def _run_warmup():
    """Force ONNX JIT compilation of both detection and recognition models."""
    t0 = time.time()
    logger.info("Running ONNX warmup...")
    try:
        # 640x480 synthetic face image
        dummy = np.full((480, 640, 3), 200, dtype=np.uint8)
        cv2.ellipse(dummy, (320, 200), (90, 120), 0, 0, 360, (160, 180, 210), -1)
        cv2.ellipse(dummy, (290, 180), (18, 12), 0, 0, 360, (240, 240, 240), -1)
        cv2.circle(dummy, (290, 180), 8, (40, 40, 40), -1)
        cv2.ellipse(dummy, (350, 180), (18, 12), 0, 0, 360, (240, 240, 240), -1)
        cv2.circle(dummy, (350, 180), 8, (40, 40, 40), -1)
        cv2.ellipse(dummy, (320, 252), (25, 10), 0, 0, 360, (120, 130, 160), -1)
        cv2.ellipse(dummy, (290, 162), (22, 5), -5, 0, 180, (80, 80, 80), 2)
        cv2.ellipse(dummy, (350, 162), (22, 5), 5, 0, 180, (80, 80, 80), 2)
        dummy = cv2.GaussianBlur(dummy, (5, 5), 1.5)
        
        # Warmup InsightFace detection + recognition
        result = FACE_EMBEDDER.get(dummy)
        if not result:
            # Detection missed synthetic face -- warm up recognition model directly
            for model in FACE_EMBEDDER.models:
                name = getattr(model, 'taskname', '') or str(type(model).__name__)
                if any(k in name.lower() for k in ('rec', 'recognition', 'arcface', 'w600k')):
                    if hasattr(model, 'session') and model.session:
                        inp = model.session.get_inputs()[0].name
                        model.session.run(None, {inp: np.random.randn(1, 3, 112, 112).astype(np.float32)})
                        break
        
        # Warmup RetinaFace (separate ONNX model)
        if FACE_DETECTOR:
            try:
                FACE_DETECTOR.detect_faces(img_path=dummy, threshold=0.5)
            except Exception:
                pass
        
        logger.info(f"ONNX warmup done in {time.time() - t0:.1f}s")
    except Exception as e:
        logger.warning(f"Warmup error (non-fatal): {e}")

def load_embeddings_from_db():
    """Loads or reloads embeddings from the Student model."""
    global TARGET_EMBEDDINGS, STUDENT_PK_TO_NAME
    logger.info("Attempting to load/reload target student embeddings from database...")
    
    # Initialize models if not already done
    if FACE_EMBEDDER is None:
        if not initialize_face_models():
            logger.error("Failed to initialize face models")
            return
    
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Attendance_student';")
            if not cursor.fetchone():
                logger.warning("Student table does not exist yet. Skipping embedding load.")
                return
    except Exception as e:
        logger.warning(f"Could not check if Student table exists: {e}")
        return
    
    # Build new dicts, then swap atomically
    new_embeddings = {}
    new_names = {}
    
    try:
        students_with_embeddings = Student.objects.filter(
            face_embedding_file__isnull=False, 
            is_active=True
        ).exclude(face_embedding_file__exact='')
        
        if not students_with_embeddings.exists():
            logger.warning("No active students found with embedding files.")
            with _embeddings_lock:
                TARGET_EMBEDDINGS = {}
                STUDENT_PK_TO_NAME = {}
            return
        
        loaded_count = 0
        skipped_count = 0
        
        for student in students_with_embeddings:
            try:
                embedding_path = student.get_embedding_path()
                if not embedding_path:
                    skipped_count += 1
                    continue
                    
                if not os.path.exists(embedding_path):
                    logger.warning(f"Embedding file not found for student {student.pk} at: {embedding_path}")
                    skipped_count += 1
                    continue
                
                embedding = np.load(embedding_path)
                
                if embedding is None or not isinstance(embedding, np.ndarray) or embedding.size == 0:
                    skipped_count += 1
                    continue
                
                if embedding.ndim == 1:
                    embedding = embedding.reshape(1, -1)
                elif embedding.ndim > 2:
                    embedding = embedding.squeeze()
                    if embedding.ndim == 1:
                        embedding = embedding.reshape(1, -1)
                    elif embedding.ndim != 2:
                        skipped_count += 1
                        continue
                
                if embedding.shape[1] != 512:
                    logger.error(f"Invalid embedding dimension for student {student.pk}: {embedding.shape}")
                    skipped_count += 1
                    continue
                
                # L2-normalize the stored embedding for proper cosine similarity
                norm = np.linalg.norm(embedding, axis=1, keepdims=True)
                norm = np.maximum(norm, 1e-10)
                embedding = embedding / norm
                    
                new_embeddings[student.pk] = embedding
                new_names[student.pk] = student.get_full_name()
                loaded_count += 1
                
            except Exception as e:
                logger.error(f"Error loading embedding for student {student.pk}: {e}")
                skipped_count += 1
        
        # FIX S2: Atomic swap under lock
        with _embeddings_lock:
            TARGET_EMBEDDINGS = new_embeddings
            STUDENT_PK_TO_NAME = new_names
        
        logger.info(f"Embedding loading complete: {loaded_count} loaded, {skipped_count} skipped.")
        
        if loaded_count == 0:
            logger.warning("WARNING: No embeddings were successfully loaded!")
            
    except Exception as e:
        logger.warning(f"Could not load embeddings from database: {e}")

# --- Utility & Core Face Processing Functions ---
def adjust_brightness(image: Image.Image, box: np.ndarray) -> Image.Image:
    """Adjusts brightness based on vertical position of the face box."""
    try:
        if not hasattr(box, '__iter__') or len(box) < 4:
            return image
        x1, y1, x2, y2 = map(int, box[0:4])
        if not (0 <= y1 < y2 <= image.height and 0 <= x1 < x2 <= image.width):
            return image

        face_y_center = (y1 + y2) // 2
        img_height = image.height
        brightness_factor = MAX_BRIGHTNESS - (MAX_BRIGHTNESS - MIN_BRIGHTNESS) * (face_y_center / img_height)
        brightness_factor = max(MIN_BRIGHTNESS, min(brightness_factor, MAX_BRIGHTNESS))

        enhancer = ImageEnhance.Brightness(image)
        return enhancer.enhance(brightness_factor)
    except Exception as e:
        logger.warning(f"Brightness adjust failed for box {box}: {e}", exc_info=False)
        return image

def crop_face_region(image: Image.Image, box: np.ndarray) -> Optional[Image.Image]:
    """Crops a fixed-size square region, clamps, and resizes."""
    try:
        if not hasattr(box, '__iter__') or len(box) < 4:
            return None

        x1, y1, x2, y2 = map(int, box[0:4])
        img_width, img_height = image.size

        if x1 >= img_width or y1 >= img_height or x2 <= 0 or y2 <= 0 or x1 >= x2 or y1 >= y2:
            return None

        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        half_target_w = IMG_WIDTH // 2
        half_target_h = IMG_HEIGHT // 2

        ideal_left = center_x - half_target_w
        ideal_upper = center_y - half_target_h
        ideal_right = center_x + (IMG_WIDTH - half_target_w)
        ideal_lower = center_y + (IMG_HEIGHT - half_target_h)

        left = max(0, ideal_left)
        upper = max(0, ideal_upper)
        right = min(img_width, ideal_right)
        lower = min(img_height, ideal_lower)

        if left >= right or upper >= lower:
            return None

        cropped = image.crop((left, upper, right, lower))

        if cropped.width != IMG_WIDTH or cropped.height != IMG_HEIGHT:
            try:
                cropped = cropped.resize((IMG_WIDTH, IMG_HEIGHT), Image.Resampling.LANCZOS)
            except ValueError:
                return None

        if cropped.width == 0 or cropped.height == 0:
            return None

        return cropped
    except Exception as e:
        logger.exception(f"Crop face region failed for box {box}: {e}")
        return None

def encode_pil_image_base64(pil_image: Image.Image) -> Optional[str]:
    """Encodes a PIL Image into a base64 string."""
    try:
        buffer = BytesIO()
        pil_image.save(buffer, format="JPEG", quality=85)
        img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{img_str}"
    except Exception as e:
        logger.error(f"Base64 encoding failed: {e}")
        return None

@contextmanager
def capture_rtsp(url: Optional[str], timeout_sec: int = 8):
    """Context manager for OpenCV VideoCapture with enforced timeout."""
    cap = None
    if not url:
        logger.error("RTSP URL not provided.")
        yield None
        return
    
    logger.debug(f"Attempting to open RTSP stream: {url}")
    try:
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_sec * 1000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_sec * 1000)
        
        # Add RTSP transport + timeout options directly in the URL for FFMPEG
        open_url = url
        if '?' in url:
            # Append RTSP timeout option via FFMPEG format
            pass  # Can't modify RTSP URLs reliably
        
        # Set FFMPEG-level timeout via environment (microseconds)
        os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = f'timeout;{timeout_sec * 1000000}|stimeout;{timeout_sec * 1000000}'
        
        success = cap.open(open_url, cv2.CAP_FFMPEG)
        
        if not success or not cap.isOpened():
            logger.error(f"Failed to open RTSP stream: {url}")
            if cap:
                cap.release()
            yield None
        else:
            logger.info(f"RTSP stream opened successfully: {url}")
            yield cap
    except Exception as e:
        logger.exception(f"RTSP Init Exception for URL {url}: {e}")
        yield None
    finally:
        if cap is not None:
            try:
                if cap.isOpened():
                    cap.release()
            except Exception as release_err:
                logger.error(f"Error releasing RTSP capture: {release_err}")

def generate_error_frame() -> Optional[bytes]:
    """Generates a placeholder frame for stream failure."""
    try:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        text = "Stream Unavailable"
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size = cv2.getTextSize(text, font, 1, 2)[0]
        text_x = (frame.shape[1] - text_size[0]) // 2
        text_y = (frame.shape[0] + text_size[1]) // 2
        cv2.putText(frame, text, (text_x, text_y), font, 1, (0, 0, 255), 2)
        ret, buf = cv2.imencode('.jpg', frame)
        return buf.tobytes() if ret else None
    except Exception as e:
        logger.error(f"Failed to generate error frame: {e}")
        return None

def _enhance_crop_clahe(cv2_bgr_crop: np.ndarray) -> np.ndarray:
    """Apply CLAHE to a face CROP for per-face lighting normalization."""
    try:
        if cv2_bgr_crop.size == 0:
            return cv2_bgr_crop
        lab = cv2.cvtColor(cv2_bgr_crop, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=(CLAHE_TILE_SIZE, CLAHE_TILE_SIZE))
        l_enhanced = clahe.apply(l_channel)
        enhanced_lab = cv2.merge([l_enhanced, a_channel, b_channel])
        return cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
    except Exception:
        return cv2_bgr_crop


def _ensure_min_size(cv2_bgr: np.ndarray, min_dim: int = 150) -> np.ndarray:
    """Upscale a crop to at least min_dim x min_dim using cubic interpolation.
    InsightFace's recognition model expects 112x112 aligned faces -- feeding it
    a 70x70 crop means the internal alignment produces a blurry 112x112.
    Upscaling to 150+ before get() gives the aligner more pixels to work with."""
    h, w = cv2_bgr.shape[:2]
    if w >= min_dim and h >= min_dim:
        return cv2_bgr
    scale = max(min_dim / w, min_dim / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    return cv2.resize(cv2_bgr, (new_w, new_h), interpolation=cv2.INTER_CUBIC)


def _crop_with_margin(cv2_bgr: np.ndarray, bbox: np.ndarray, margin_factor: float = CROP_MARGIN_FACTOR) -> Optional[np.ndarray]:
    """Crop face region with generous margin so InsightFace can re-detect + align."""
    try:
        x1, y1, x2, y2 = map(int, bbox[:4])
        img_h, img_w = cv2_bgr.shape[:2]
        face_w = x2 - x1
        face_h = y2 - y1
        if face_w <= 0 or face_h <= 0:
            return None
        margin_x = int(face_w * margin_factor)
        margin_y = int(face_h * margin_factor)
        crop_x1 = max(0, x1 - margin_x)
        crop_y1 = max(0, y1 - margin_y)
        crop_x2 = min(img_w, x2 + margin_x)
        crop_y2 = min(img_h, y2 + margin_y)
        if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
            return None
        crop = cv2_bgr[crop_y1:crop_y2, crop_x1:crop_x2]
        # Ensure minimum size for good embedding quality
        crop = _ensure_min_size(crop, 150)
        return crop
    except Exception as e:
        logger.debug(f"Margin crop failed: {e}")
        return None


def _l2_normalize(embedding: np.ndarray) -> np.ndarray:
    """L2-normalize an embedding vector (or batch). Essential for cosine similarity accuracy."""
    if embedding.ndim == 1:
        norm = np.linalg.norm(embedding)
        return embedding / norm if norm > 0 else embedding
    # 2D: normalize each row
    norms = np.linalg.norm(embedding, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)
    return embedding / norms


def _extract_embedding_with_tta(cv2_crop: np.ndarray) -> Optional[np.ndarray]:
    """Extract embedding from a crop using Test-Time Augmentation (TTA).
    
    Process:
    1. Run InsightFace get() on the original crop -> embedding_original
    2. Run InsightFace get() on horizontally flipped crop -> embedding_flipped  
    3. Average the two embeddings
    4. L2-normalize the result
    
    The flip-average technique is standard in face recognition competitions and
    improves accuracy by 2-3% because it makes the embedding more robust to 
    slight left-right pose asymmetries. It effectively doubles the 'views' of 
    each face without needing another camera angle.
    
    When multiple faces are found in the crop, picks the one closest to crop center.
    """
    try:
        if cv2_crop is None or cv2_crop.size == 0:
            return None
        
        # Ensure minimum size
        cv2_crop = _ensure_min_size(cv2_crop, 150)
        
        # --- Original embedding ---
        emb_original = _get_center_face_embedding(cv2_crop)
        
        # --- Flipped embedding (TTA) ---
        flipped_crop = cv2.flip(cv2_crop, 1)  # horizontal flip
        emb_flipped = _get_center_face_embedding(flipped_crop)
        
        # --- Fuse ---
        if emb_original is not None and emb_flipped is not None:
            # Average and normalize -- this is the standard TTA fusion
            fused = (emb_original + emb_flipped) / 2.0
            fused = _l2_normalize(fused)
            return fused
        elif emb_original is not None:
            return _l2_normalize(emb_original)
        elif emb_flipped is not None:
            return _l2_normalize(emb_flipped)
        
        return None
    except Exception as e:
        logger.debug(f"TTA embedding extraction failed: {e}")
        return None


def _get_center_face_embedding(cv2_crop: np.ndarray) -> Optional[np.ndarray]:
    """Run InsightFace get() and return the embedding of the face closest to crop center."""
    try:
        faces = FACE_EMBEDDER.get(cv2_crop)
        if not faces or len(faces) == 0:
            return None
        
        if len(faces) == 1:
            f = faces[0]
            if hasattr(f, 'embedding') and f.embedding is not None and isinstance(f.embedding, np.ndarray) and f.embedding.size > 0:
                return f.embedding.reshape(1, -1)
            return None
        
        # Multiple faces: pick closest to center
        crop_h, crop_w = cv2_crop.shape[:2]
        cx, cy = crop_w / 2.0, crop_h / 2.0
        
        best_face = None
        best_dist = float('inf')
        
        for f in faces:
            if not hasattr(f, 'embedding') or f.embedding is None:
                continue
            if not isinstance(f.embedding, np.ndarray) or f.embedding.size == 0:
                continue
            fb = f.bbox
            fx = (fb[0] + fb[2]) / 2.0
            fy = (fb[1] + fb[3]) / 2.0
            dist = (fx - cx) ** 2 + (fy - cy) ** 2
            if dist < best_dist:
                best_dist = dist
                best_face = f
        
        if best_face is not None:
            return best_face.embedding.reshape(1, -1)
        return None
    except Exception:
        return None


def _process_detected_faces(pil_image: Image.Image) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Dual-path face processing: 2 InsightFace calls per face, take max similarity.
    
    Path A: tight crop + brightness (matches stored .npy domain)
    Path B: margin crop + CLAHE (better alignment, catches what Path A misses)
    
    Both embeddings compared against stored references, MAX wins.
    No TTA, no Path C = 2 calls per face instead of 6.
    """
    recognized_faces: List[Dict[str, Any]] = []
    unidentified_faces: List[str] = []

    with _embeddings_lock:
        local_embeddings = dict(TARGET_EMBEDDINGS)
        local_names = dict(STUDENT_PK_TO_NAME)

    if not all([RETINAFACE_AVAILABLE, INSIGHTFACE_AVAILABLE, FACE_EMBEDDER, local_embeddings]):
        logger.error("Face processing prerequisites not met.")
        return [], []

    try:
        cv2_image_bgr = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
        
        # Step 1: RetinaFace detection
        start_time = time.time()
        try:
            faces_data = RetinaFace.detect_faces(img_path=cv2_image_bgr, threshold=FACE_DETECTION_THRESHOLD)
        except Exception as detect_err:
            logger.exception(f"RetinaFace detection failed: {detect_err}")
            return [], []
        
        num_detected = len(faces_data) if isinstance(faces_data, dict) else 0
        logger.info(f"RetinaFace: {num_detected} faces in {time.time() - start_time:.2f}s")
        
        if not faces_data or not isinstance(faces_data, dict):
            return [], []
        
        # Step 2: Extract embeddings via dual path (2 calls per face)
        face_embeddings_data = []  # (face_idx, [embeddings], face_base64, bbox)
        embed_start = time.time()
        
        for i, (face_key, face_info) in enumerate(faces_data.items()):
            try:
                bbox = np.array(face_info.get('facial_area', []))
                if bbox.size < 4:
                    continue
                
                confidence = face_info.get('score', 0)
                if confidence < FACE_DETECTION_THRESHOLD:
                    continue
                
                face_w = int(bbox[2] - bbox[0])
                face_h = int(bbox[3] - bbox[1])
                if face_w < 15 or face_h < 15:
                    continue
                
                embeddings = []  # collect valid embeddings for this face
                
                # PATH A: Original method (matches .npy reference domain)
                try:
                    adjusted_pil = adjust_brightness(pil_image, bbox)
                    tight_crop = crop_face_region(adjusted_pil, bbox)
                    if tight_crop:
                        tight_bgr = cv2.cvtColor(np.array(tight_crop), cv2.COLOR_RGB2BGR)
                        tight_bgr = _ensure_min_size(tight_bgr, 150)
                        emb_a = _get_center_face_embedding(tight_bgr)
                        if emb_a is not None:
                            embeddings.append(_l2_normalize(emb_a))
                except Exception:
                    pass
                
                # PATH B: Enhanced method (better alignment)
                try:
                    margin_crop = _crop_with_margin(cv2_image_bgr, bbox)
                    if margin_crop is not None:
                        enhanced = _enhance_crop_clahe(margin_crop)
                        emb_b = _get_center_face_embedding(enhanced)
                        if emb_b is not None:
                            embeddings.append(_l2_normalize(emb_b))
                        elif margin_crop is not None:
                            emb_b2 = _get_center_face_embedding(margin_crop)
                            if emb_b2 is not None:
                                embeddings.append(_l2_normalize(emb_b2))
                except Exception:
                    pass
                
                if not embeddings:
                    display_crop = crop_face_region(pil_image, bbox)
                    if display_crop:
                        b64 = encode_pil_image_base64(display_crop)
                        if b64:
                            unidentified_faces.append(b64)
                    continue
                
                # Validate dimensions
                expected_dim = next(iter(local_embeddings.values())).shape[1] if local_embeddings else 512
                embeddings = [e for e in embeddings if e.shape[1] == expected_dim]
                if not embeddings:
                    continue
                
                # Display crop for UI
                display_crop = crop_face_region(pil_image, bbox)
                face_base64 = encode_pil_image_base64(display_crop) if display_crop else None
                if not face_base64:
                    continue
                
                face_embeddings_data.append((i, embeddings, face_base64, bbox))
                    
            except Exception as e:
                logger.error(f"Error processing face {i+1}: {e}")
                continue
        
        logger.info(f"Embeddings: {len(face_embeddings_data)}/{num_detected} in {time.time() - embed_start:.2f}s")
        
        # Step 3: Matching -- compare ALL embeddings per face, take MAX similarity.
        # Greedy 1-to-1 assignment: each face is claimed by at most one student,
        # and each student is claimed by at most one face within this frame.
        if face_embeddings_data:
            similarity_scores = []
            face_bbox_lookup: Dict[int, Any] = {
                face_idx: bbox for face_idx, _, _, bbox in face_embeddings_data
            }

            for face_idx, emb_list, face_base64, bbox in face_embeddings_data:
                for student_pk, target_embedding in local_embeddings.items():
                    best_sim = max(
                        cosine_similarity(emb, target_embedding)[0][0]
                        for emb in emb_list
                    )
                    if best_sim > SIMILARITY_THRESHOLD:
                        similarity_scores.append(
                            (best_sim, face_idx, student_pk, face_base64)
                        )

            similarity_scores.sort(key=lambda x: x[0], reverse=True)

            assigned_faces = set()
            assigned_students = set()

            for similarity, face_idx, student_pk, face_base64 in similarity_scores:
                # Enforce strict 1:1: skip if this face OR this student is already taken.
                if face_idx in assigned_faces or student_pk in assigned_students:
                    continue
                assigned_faces.add(face_idx)
                assigned_students.add(student_pk)
                bbox = face_bbox_lookup.get(face_idx)
                recognized_faces.append({
                    "student_pk": student_pk,
                    "name": local_names.get(student_pk, 'Unknown'),
                    "image": face_base64,
                    "score": float(similarity),
                    # bbox kept for downstream multi-frame deduplication; safe to
                    # serialize because JsonResponse handles plain lists.
                    "bbox": [int(v) for v in bbox[:4]] if bbox is not None else None,
                })

            for face_idx, _, face_base64, _ in face_embeddings_data:
                if face_idx not in assigned_faces:
                    unidentified_faces.append(face_base64)
        
        logger.info(f"Result: {len(recognized_faces)} recognized, {len(unidentified_faces)} unidentified in {time.time() - start_time:.2f}s")
                
    except Exception as e:
        logger.exception(f"Error in face processing pipeline: {e}")
    
    return recognized_faces, unidentified_faces


def _bbox_iou(a: Optional[List[int]], b: Optional[List[int]]) -> float:
    """Intersection-over-Union of two [x1,y1,x2,y2] bboxes. Returns 0 if either missing."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ax1, ay1, ax2, ay2 = a[0], a[1], a[2], a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[2], b[3]
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _process_multi_frame(frames: List[np.ndarray], pil_display: Image.Image) -> Tuple[List[Dict[str, Any]], List[str]]:
    """MULTI-FRAME FUSION: Process N frames independently, then enforce 1:1.

    Two-stage deduplication:

    1. **Spatial dedup (per physical face):** group recognitions across frames
       by bbox IoU > 0.4 — these are the same physical person seen in multiple
       frames. Within the group, the highest-scoring (student_pk, score) wins.
       This stops the same face from being labelled as Student A in frame 1 and
       Student B in frame 2.

    2. **Identity dedup (per student):** if two distinct face clusters both
       claim the same student, the lower-scoring one is demoted to unidentified.
       This stops two different physical faces from both being marked as
       Student A.

    Net invariant: every entry returned in `recognized_faces` corresponds to
    exactly one physical face *and* exactly one student.
    """
    if not all([RETINAFACE_AVAILABLE, INSIGHTFACE_AVAILABLE, FACE_EMBEDDER, TARGET_EMBEDDINGS]):
        return [], []

    all_recognitions: List[Dict[str, Any]] = []  # flat list across all frames
    all_unidentified_by_frame: Dict[int, List[str]] = {}
    max_det_count = 0
    max_det_frame = 0

    for frame_idx, frame_bgr in enumerate(frames):
        try:
            pil_frame = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            recognized, unidentified = _process_detected_faces(pil_frame)

            det_count = len(recognized) + len(unidentified)
            logger.info(
                f"Multi-frame {frame_idx+1}/{len(frames)}: "
                f"{len(recognized)} recognized, {len(unidentified)} unidentified"
            )

            if det_count > max_det_count:
                max_det_count = det_count
                max_det_frame = frame_idx

            all_unidentified_by_frame[frame_idx] = unidentified

            for face in recognized:
                # `bbox` was added by _process_detected_faces; default to None for safety.
                face_with_frame = dict(face)
                face_with_frame['_frame_idx'] = frame_idx
                all_recognitions.append(face_with_frame)

        except Exception as e:
            logger.error(f"Multi-frame {frame_idx+1} error: {e}")
            continue

    # --- Stage 1: cluster by spatial IoU (same physical face across frames) ---
    IOU_THRESHOLD = 0.4
    clusters: List[List[Dict[str, Any]]] = []  # each cluster = [recognitions of one physical face]
    for rec in all_recognitions:
        bbox = rec.get('bbox')
        placed = False
        for cluster in clusters:
            # Compare against the highest-scoring member of the cluster (anchor).
            anchor = cluster[0]
            if _bbox_iou(bbox, anchor.get('bbox')) >= IOU_THRESHOLD:
                cluster.append(rec)
                placed = True
                break
        if not placed:
            clusters.append([rec])

    # Within each cluster, take the single highest-scoring recognition.
    # That is the system's best opinion of who this physical face is.
    cluster_winners: List[Dict[str, Any]] = []
    for cluster in clusters:
        winner = max(cluster, key=lambda r: r.get('score', 0))
        cluster_winners.append(winner)

    # --- Stage 2: dedup by student_pk (one student -> one face) ---
    # If two physical faces both claim the same student, only the higher-scoring
    # one keeps the identification; the loser becomes unidentified.
    cluster_winners.sort(key=lambda r: r.get('score', 0), reverse=True)
    claimed_students: set = set()
    recognized_faces: List[Dict[str, Any]] = []
    demoted_unidentified: List[str] = []
    for rec in cluster_winners:
        pk = rec.get('student_pk')
        if pk is None or pk in claimed_students:
            # Lost the duel for this student -> show as unidentified instead.
            img = rec.get('image')
            if img:
                demoted_unidentified.append(img)
            continue
        claimed_students.add(pk)
        # Strip internal-only fields before returning to the client.
        clean = {k: v for k, v in rec.items() if not k.startswith('_')}
        recognized_faces.append(clean)

    # Use unidentified faces from the frame with most detections, plus any
    # faces demoted by Stage 2.
    final_unidentified = list(all_unidentified_by_frame.get(max_det_frame, []))
    final_unidentified.extend(demoted_unidentified)

    logger.info(
        f"Multi-frame fusion: {len(recognized_faces)} unique students from "
        f"{len(clusters)} physical face clusters across {len(frames)} frames "
        f"({len(demoted_unidentified)} duplicates demoted)"
    )

    return recognized_faces, final_unidentified

@login_required
@require_POST
def save_training_image(request: HttpRequest) -> JsonResponse:
    """Save unidentified images that are manually assigned to student folders with augmentations."""
    try:
        data = json.loads(request.body)
        student_pk = data.get('student_pk')
        image_data = data.get('image_data')
        class_id = data.get('class_id')
        create_augmentations = data.get('create_augmentations', True)
        
        if not all([student_pk, image_data, class_id]):
            return JsonResponse({
                'success': False, 
                'message': 'Missing required parameters'
            }, status=400)
        
        try:
            student = Student.objects.get(pk=student_pk)
        except Student.DoesNotExist:
            return JsonResponse({
                'success': False,
                'message': f'Student with pk {student_pk} not found'
            }, status=404)
        
        try:
            class_instance = get_object_or_404(Class, pk=class_id, instructors=request.user.teacher)
        except (Class.DoesNotExist, Teacher.DoesNotExist):
            return JsonResponse({
                'success': False,
                'message': 'Unauthorized access to class'
            }, status=403)
        
        if not student.images_folder_path:
            base_folder = os.path.join(settings.MEDIA_ROOT, 'student_images')
            student_folder = os.path.join(base_folder, student.registration_id)
            
            if not os.path.exists(student_folder):
                os.makedirs(student_folder, exist_ok=True)
            
            student.images_folder_path = student_folder
            student.save()
            logger.info(f"Created images folder for student {student.registration_id}: {student_folder}")
        
        if not os.path.exists(student.images_folder_path):
            os.makedirs(student.images_folder_path, exist_ok=True)
        
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        
        try:
            image_bytes = base64.b64decode(image_data)
            image = Image.open(BytesIO(image_bytes))
            
            if image.mode != 'RGB':
                image = image.convert('RGB')
                
        except Exception as e:
            logger.error(f"Error decoding image: {e}")
            return JsonResponse({
                'success': False,
                'message': 'Invalid image data'
            }, status=400)
        
        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
        image_hash = hashlib.md5(image_bytes).hexdigest()[:8]
        
        base_filename = f"{timestamp}_training_{image_hash}"
        
        original_path = os.path.join(student.images_folder_path, f"{base_filename}_original.jpg")
        image.save(original_path, 'JPEG', quality=95)
        logger.info(f"Saved original training image: {original_path}")
        
        saved_files = [original_path]
        
        if create_augmentations:
            augmented_images = create_image_augmentations(image)
            
            for idx, (aug_image, aug_type) in enumerate(augmented_images, 1):
                aug_filename = f"{base_filename}_aug{idx}_{aug_type}.jpg"
                aug_path = os.path.join(student.images_folder_path, aug_filename)
                aug_image.save(aug_path, 'JPEG', quality=95)
                saved_files.append(aug_path)
        
        logger.info(
            f"Saved training image for student {student.registration_id} with {len(saved_files)-1} augmentations"
        )
        
        metadata = {
            'student_pk': student_pk,
            'student_registration_id': student.registration_id,
            'student_name': student.get_full_name(),
            'source': 'manual_assignment_from_unidentified',
            'class_id': class_id,
            'timestamp': timestamp,
            'marked_by': request.user.username,
            'files': [os.path.basename(f) for f in saved_files]
        }
        
        metadata_path = os.path.join(student.images_folder_path, f"{base_filename}_metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        return JsonResponse({
            'success': True,
            'message': f'Saved {len(saved_files)} training images',
            'files_saved': len(saved_files),
            'folder': student.images_folder_path
        })
        
    except Exception as e:
        logger.exception(f"Error saving training image: {e}")
        return JsonResponse({
            'success': False,
            'message': f'Server error: {str(e)}'
        }, status=500)


def create_image_augmentations(original_image: Image.Image) -> List[Tuple[Image.Image, str]]:
    """Create 6 augmented versions of an image with minor variations."""
    augmented_images = []
    
    try:
        angle = random.uniform(-5, 5)
        rotated = original_image.rotate(angle, fillcolor='white', expand=False)
        augmented_images.append((rotated, 'rotate'))
        
        brightness_factor = random.uniform(0.9, 1.1)
        brightened = ImageEnhance.Brightness(original_image).enhance(brightness_factor)
        augmented_images.append((brightened, 'brightness'))
        
        contrast_factor = random.uniform(0.9, 1.1)
        contrasted = ImageEnhance.Contrast(original_image).enhance(contrast_factor)
        augmented_images.append((contrasted, 'contrast'))
        
        color_factor = random.uniform(0.9, 1.1)
        colored = ImageEnhance.Color(original_image).enhance(color_factor)
        augmented_images.append((colored, 'color'))
        
        sharpness_factor = random.uniform(0.8, 1.2)
        sharpened = ImageEnhance.Sharpness(original_image).enhance(sharpness_factor)
        augmented_images.append((sharpened, 'sharpness'))
        
        blurred = original_image.filter(ImageFilter.GaussianBlur(radius=0.5))
        augmented_images.append((blurred, 'blur'))
        
    except Exception as e:
        logger.error(f"Error creating augmentations: {e}")
        for i in range(6):
            augmented_images.append((original_image.copy(), f'copy{i+1}'))
    
    return augmented_images



# --- Authentication Views ---
def login_view(request: HttpRequest) -> HttpResponse:
    """Handles user login."""
    if request.user.is_authenticated:
        return redirect("Attendance:instructor_dashboard")
        
    if request.method == 'POST':
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            if hasattr(user, 'teacher'):
                login(request, user)
                return redirect("Attendance:instructor_dashboard")
            else:
                messages.error(request, "This account is not configured as a teacher profile.")
        else:
            messages.error(request, "Invalid username or password.")
            
    return render(request, "Attendance/login.html")

@login_required
def logout_view(request: HttpRequest) -> HttpResponse:
    """Handles user logout."""
    logout(request)
    messages.info(request, "You have been successfully logged out.")
    return redirect("Attendance:login")

# --- Main Application Views ---
@login_required
def page1_view(request: HttpRequest) -> HttpResponse:
    """Instructor Dashboard: Displays a list of classes taught by the instructor."""
    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        messages.error(request, "Access denied. Your user profile is not configured as a teacher.")
        logout(request)
        return redirect("Attendance:login")

    # FIX P3: Prefetch related data to avoid N+1 queries
    classes_taught = Class.objects.filter(instructors=teacher, is_active=True).select_related(
        'course'
    ).prefetch_related(
        'batches', 'sections', 'classroom_overrides', 'enrollments'
    ).order_by('-semester', 'course__course_code')

    context = {
        'teacher': teacher,
        'classes_taught': classes_taught,
        'today': timezone.now().date(),  # FIX B4/U1: Pass today to template context
    }
    return render(request, "Attendance/instructor_dashboard.html", context)

@login_required
def attendance_view(request: HttpRequest) -> HttpResponse:
    """Attendance taking interface for a specific class with classroom override support."""
    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        messages.error(request, "Access denied. Instructor profile not found.")
        return redirect("Attendance:login")

    if not INSIGHTFACE_AVAILABLE:
        messages.error(request, "System error: Face recognition components are not available.")
        return render(request, "Attendance/error.html", {"error_message": "Attendance system components failed to load."})

    selected_class_id = request.GET.get('class_id')
    selected_class = None
    all_students_in_class = []
    has_stream = False
    active_override = None
    available_classrooms = []

    # FIX B4/U1: Compute today once
    today = timezone.now().date()

    if selected_class_id:
        try:
            selected_class = get_object_or_404(Class, pk=int(selected_class_id), instructors=teacher)
            
            active_override = ClassroomOverride.objects.filter(
                class_instance=selected_class,
                override_date=today,
                is_active=True
            ).first()
            
            available_classrooms = Class.objects.filter(
                rtsp_stream_url__isnull=False
            ).exclude(
                rtsp_stream_url=''
            ).values_list('classroom', 'rtsp_stream_url').distinct()
            
            enrolled_students = Student.objects.filter(
                enrollments__class_instance=selected_class,
                enrollments__is_active=True,
                is_active=True
            ).distinct().order_by('last_name', 'first_name')
            
            all_students_in_class = enrolled_students
            has_stream = selected_class.has_stream or (active_override and active_override.temporary_rtsp_url)
            
        except (ValueError, TypeError, Http404):
            messages.error(request, "The selected class was not found or you are not authorized to view it.")
            selected_class_id = None
    else:
        messages.warning(request, "Please select a class from your dashboard to start taking attendance.")

    context = {
        'selected_class_id': selected_class_id,
        'selected_class': selected_class,
        'all_students': all_students_in_class,
        'embeddings_loaded': len(TARGET_EMBEDDINGS),
        'similarity_threshold': SIMILARITY_THRESHOLD,
        'has_stream': has_stream,
        'active_override': active_override,
        'available_classrooms': available_classrooms,
        'today': today,  # FIX B4/U1: Pass today to template context
        'ATTENDANCE_MAX_UPLOAD_MB': MAX_UPLOAD_SIZE_MB,  # FIX U2: Pass actual setting to template
    }
    return render(request, "Attendance/pro_attend.html", context)



@require_POST
@login_required
def take_attendance(request: HttpRequest) -> JsonResponse:
    """API endpoint to process attendance from a live stream frame."""
    if not INSIGHTFACE_AVAILABLE:
        return JsonResponse({'error': 'Face recognition libraries are not installed on this server.'}, status=503)

    # Ensure models are initialized and warmed up (blocks on first call only)
    _wait_for_models()

    class_id = request.POST.get('class_id')
    if not class_id:
        return JsonResponse({'error': 'A Class ID must be provided.'}, status=400)

    try:
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=request.user.teacher)
    except (Class.DoesNotExist, ValueError, TypeError, Http404, Teacher.DoesNotExist):
        return JsonResponse({'error': 'Invalid or unauthorized Class ID.'}, status=403)

    today = timezone.now().date()
    active_override = ClassroomOverride.objects.filter(
        class_instance=class_instance,
        override_date=today,
        is_active=True
    ).first()

    if active_override and active_override.temporary_rtsp_url:
        rtsp_url = active_override.temporary_rtsp_url
        logger.info(f"Using override camera in {active_override.temporary_classroom} for attendance")
    elif class_instance.has_stream:
        rtsp_url = class_instance.rtsp_stream_url
    else:
        return JsonResponse({'error': 'No camera stream configured for this classroom.'}, status=400)

    if not TARGET_EMBEDDINGS:
        logger.warning("No target embeddings loaded, attempting to reload...")
        load_embeddings_from_db()
        if not TARGET_EMBEDDINGS:
            return JsonResponse({'error': 'No student embeddings available.'}, status=503)

    pil_image: Optional[Image.Image] = None
    captured_frames: List[np.ndarray] = []
    try:
        with capture_rtsp(rtsp_url) as cap:
            if cap is None:
                return JsonResponse({'error': 'Failed to connect to camera stream.'}, status=502)
            
            # MULTI-FRAME CAPTURE: Grab multiple frames for fusion
            # Skip initial frames (often buffered/stale)
            for _ in range(3):
                cap.read()
            
            for frame_num in range(MULTI_FRAME_COUNT):
                frame = None
                for attempt in range(3):
                    ret, temp_frame = cap.read()
                    if ret and temp_frame is not None:
                        frame = temp_frame
                        break
                    time.sleep(0.05)
                
                if frame is not None:
                    captured_frames.append(frame)
                
                if frame_num < MULTI_FRAME_COUNT - 1:
                    time.sleep(MULTI_FRAME_DELAY)
            
            if not captured_frames:
                return JsonResponse({'error': 'Failed to capture frames from camera stream.'}, status=502)
            
            # Use last captured frame for display reference
            pil_image = Image.fromarray(cv2.cvtColor(captured_frames[-1], cv2.COLOR_BGR2RGB))
            
            logger.info(f"Multi-frame capture: got {len(captured_frames)} frames")
            
    except Exception as e:
        logger.exception(f"Error capturing frames: {e}")
        return JsonResponse({'error': 'Camera stream error. Please try again.'}, status=502)

    try:
        # Use multi-frame fusion if we got multiple frames, else single-frame
        if len(captured_frames) >= 2:
            recognized, unidentified = _process_multi_frame(captured_frames, pil_image)
        else:
            recognized, unidentified = _process_detected_faces(pil_image)
        saved_count = 0
        already_marked = 0

        if recognized:
            current_date = timezone.now().date()
            current_time = timezone.now().time()
            with transaction.atomic():
                for face_data in recognized:
                    student_pk = face_data.get("student_pk")
                    if student_pk:
                        attendance, created = AttendanceRecord.objects.update_or_create(
                            student_id=student_pk,
                            class_instance=class_instance,
                            attendance_date=current_date,
                            defaults={
                                'status': AttendanceRecord.StatusChoices.PRESENT,
                                'marked_by': request.user,
                                'attendance_time': current_time,
                                'notes': f'Recorded in {active_override.temporary_classroom}' if active_override else ''
                            }
                        )
                        if created:
                            saved_count += 1
                        else:
                            already_marked += 1
        
        status_parts = []
        if len(recognized) > 0:
            status_parts.append(f"Recognized: {len(recognized)}")
        if saved_count > 0:
            status_parts.append(f"Newly marked present: {saved_count}")
        if already_marked > 0:
            status_parts.append(f"Already marked: {already_marked}")
        if len(unidentified) > 0:
            status_parts.append(f"Unidentified faces: {len(unidentified)}")
        
        status_msg = ". ".join(status_parts) if status_parts else "No faces detected in frame."
        logger.info(f"Attendance processing: {status_msg}")
        
    except Exception as e:
        logger.exception("Error during attendance processing: %s", e)
        return HttpResponseServerError('An error occurred while processing the attendance.')

    return JsonResponse({"recognized_faces": recognized, "unidentified_faces": unidentified, "status": status_msg})

# FIX B3: Removed @csrf_exempt -- JS already sends CSRF token
@require_POST
@login_required
def take_attendance_image(request: HttpRequest) -> JsonResponse:
    """API endpoint to process attendance from an uploaded image."""
    if not INSIGHTFACE_AVAILABLE:
        return JsonResponse({'error': 'Face recognition libraries are not installed on this server.'}, status=503)

    # Ensure models are initialized and warmed up (blocks on first call only)
    _wait_for_models()

    class_id = request.POST.get('class_id')
    if not class_id:
        return JsonResponse({'error': 'A Class ID must be provided.'}, status=400)

    try:
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=request.user.teacher)
    except (Class.DoesNotExist, ValueError, TypeError, Http404, Teacher.DoesNotExist):
        return JsonResponse({'error': 'Invalid or unauthorized Class ID.'}, status=403)

    image_file = request.FILES.get('image')
    if not image_file:
        return HttpResponseBadRequest("No image file uploaded.")
    
    if image_file.size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        return HttpResponseBadRequest(f"Image file too large. Maximum size is {MAX_UPLOAD_SIZE_MB}MB.")
    
    try:
        pil_image = Image.open(image_file).convert("RGB")
    except UnidentifiedImageError:
        return HttpResponseBadRequest("Invalid or corrupted image file.")

    try:
        recognized, unidentified = _process_detected_faces(pil_image)
        saved_count = 0
        already_marked = 0

        if recognized:
            current_date = timezone.now().date()
            current_time = timezone.now().time()
            with transaction.atomic():
                for face_data in recognized:
                    student_pk = face_data.get("student_pk")
                    if student_pk:
                        _, created = AttendanceRecord.objects.update_or_create(
                            student_id=student_pk,
                            class_instance=class_instance,
                            attendance_date=current_date,
                            defaults={
                                'status': AttendanceRecord.StatusChoices.PRESENT,
                                'marked_by': request.user,
                                'attendance_time': current_time
                            }
                        )
                        if created:
                            saved_count += 1
                        else:
                            already_marked += 1

        status_msg = (
            f"Processed. Recognized: {len(recognized)}, "
            f"Unidentified: {len(unidentified)}, Newly saved: {saved_count}, "
            f"Already marked: {already_marked}."
        )
        logger.info(status_msg)
    except Exception as e:
        logger.exception(f"Error during image attendance processing: {e}")
        return HttpResponseServerError("An error occurred while processing the image.")

    return JsonResponse({"recognized_faces": recognized, "unidentified_faces": unidentified, "status": status_msg})

# --- Utility endpoint to reload embeddings ---
@login_required
@require_POST
def reload_embeddings(request: HttpRequest) -> JsonResponse:
    """Force reload all embeddings from database."""
    try:
        load_embeddings_from_db()
        return JsonResponse({
            'success': True,
            'message': f'Successfully reloaded {len(TARGET_EMBEDDINGS)} embeddings',
            'count': len(TARGET_EMBEDDINGS)
        })
    except Exception as e:
        logger.exception(f"Error reloading embeddings: {e}")
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

# --- CSV and Other Views ---
@login_required
def download_attendance(request: HttpRequest) -> HttpResponse:
    """Generates and downloads a comprehensive CSV report for a specific class with Pakistan timezone."""
    
    pk_timezone = pytz.timezone('Asia/Karachi')
    
    class_id = request.GET.get('class_id')
    if not class_id:
        messages.error(request, "Please select a class to download the report.")
        return redirect(request.META.get('HTTP_REFERER', 'Attendance:instructor_dashboard'))

    try:
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=request.user.teacher)
    except (ValueError, Http404, Teacher.DoesNotExist):
        messages.error(request, "Invalid or unauthorized class selected for download.")
        return redirect(request.META.get('HTTP_REFERER', 'Attendance:instructor_dashboard'))

    attendance_records = AttendanceRecord.objects.filter(
        class_instance=class_instance
    ).select_related(
        'student', 'student__batch', 'student__section', 'marked_by'
    ).order_by('attendance_date', 'student__section__section_name', 'student__registration_id')

    enrolled_students = Student.objects.filter(
        enrollments__class_instance=class_instance,
        enrollments__is_active=True,
        is_active=True
    ).select_related('batch', 'section').order_by('section__section_name', 'registration_id')

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    
    current_time_pk = timezone.now().astimezone(pk_timezone)
    batch_codes = "_".join([b.batch_code for b in class_instance.batches.all()[:2]])
    if not batch_codes:
        batch_codes = "NoBatch"
    filename = f"attendance_{class_instance.course.course_code}_{batch_codes}_{current_time_pk.strftime('%Y%m%d_%H%M%S')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    response.write('\ufeff')
    
    writer = csv.writer(response)
    
    writer.writerow(['=' * 50])
    writer.writerow(['UNIVERSITY ATTENDANCE REPORT'])
    writer.writerow(['=' * 50])
    writer.writerow([])
    
    writer.writerow(['COURSE INFORMATION'])
    writer.writerow(['Course Code:', class_instance.course.course_code])
    writer.writerow(['Course Title:', class_instance.course.title])
    batch_names = ", ".join([b.batch_name for b in class_instance.batches.all()])
    if not batch_names:
        batch_names = "No batches assigned"
    writer.writerow(['Batch(es):', batch_names])   
    writer.writerow(['Semester:', class_instance.semester])
    writer.writerow(['Classroom:', class_instance.classroom or 'Not Specified'])
    writer.writerow(['Instructor:', request.user.get_full_name() or request.user.username])
    writer.writerow(['Report Generated:', current_time_pk.strftime('%Y-%m-%d %I:%M:%S %p PKT')])
    writer.writerow([]) 
    
    attendance_dates = sorted(set(attendance_records.values_list('attendance_date', flat=True)))
    
    writer.writerow(['SUMMARY STATISTICS'])
    writer.writerow(['Total Enrolled Students:', enrolled_students.count()])
    writer.writerow(['Total Class Sessions:', len(attendance_dates)])
    
    if attendance_dates:
        writer.writerow(['First Session:', attendance_dates[0].strftime('%Y-%m-%d')])
        writer.writerow(['Last Session:', attendance_dates[-1].strftime('%Y-%m-%d')])
    
    section_stats = defaultdict(lambda: {'total': 0, 'present_total': 0})
    for student in enrolled_students:
        section_name = student.section.section_name if student.section else 'No Section'
        section_stats[section_name]['total'] += 1
    
    writer.writerow([])
    writer.writerow(['Section-wise Enrollment:'])
    for section, stats in sorted(section_stats.items()):
        writer.writerow([f'  {section}:', stats['total'], 'students'])
    
    writer.writerow([])
    writer.writerow(['=' * 50])
    writer.writerow(['DETAILED ATTENDANCE RECORDS'])
    writer.writerow(['=' * 50])
    writer.writerow([])
    
    attendance_matrix = OrderedDict()
    student_data = OrderedDict()
    
    for student in enrolled_students:
        key = student.registration_id
        student_data[key] = {
            'registration_id': student.registration_id,
            'name': student.get_full_name(),
            'section': student.section.section_name if student.section else 'N/A',
            'batch': student.batch.batch_code if student.batch else 'N/A',
            'email': student.email or 'N/A'
        }
        attendance_matrix[key] = OrderedDict()
        for att_date in attendance_dates:
            attendance_matrix[key][att_date] = {
                'status': 'A',
                'time': '',
                'marked_by': '',
                'notes': ''
            }
    
    for record in attendance_records:
        key = record.student.registration_id
        if key in attendance_matrix:
            if record.attendance_time:
                # FIX B5: Use timezone-aware datetime properly
                try:
                    dt = datetime.combine(record.attendance_date, record.attendance_time)
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                    dt_pk = dt.astimezone(pk_timezone)
                    time_str = dt_pk.strftime('%I:%M:%S %p')
                except Exception:
                    time_str = str(record.attendance_time)
            else:
                time_str = ''
            
            attendance_matrix[key][record.attendance_date] = {
                'status': 'P' if record.status == AttendanceRecord.StatusChoices.PRESENT else 'A',
                'time': time_str,
                'marked_by': record.marked_by.username if record.marked_by else 'System',
                'notes': record.notes or ''
            }
    
    headers = ['Registration ID', 'Student Name', 'Section', 'Batch', 'Email']
    
    for att_date in attendance_dates:
        headers.append(att_date.strftime('%Y-%m-%d'))
    
    headers.extend(['Total Present', 'Total Absent', 'Attendance %'])
    
    writer.writerow(headers)
    
    for reg_id, dates in attendance_matrix.items():
        row = [
            student_data[reg_id]['registration_id'],
            student_data[reg_id]['name'],
            student_data[reg_id]['section'],
            student_data[reg_id]['batch'],
            student_data[reg_id]['email']
        ]
        
        present_count = 0
        absent_count = 0
        
        for att_date in attendance_dates:
            if att_date in dates:
                status = dates[att_date]['status']
                row.append(status)
                if status == 'P':
                    present_count += 1
                else:
                    absent_count += 1
            else:
                row.append('A')
                absent_count += 1
        
        total_sessions = len(attendance_dates)
        attendance_percentage = (present_count / total_sessions * 100) if total_sessions > 0 else 0
        
        row.extend([
            present_count,
            absent_count,
            f"{attendance_percentage:.1f}%"
        ])
        
        writer.writerow(row)
    
    writer.writerow([])
    writer.writerow(['=' * 50])
    writer.writerow(['LEGEND'])
    writer.writerow(['P = Present'])
    writer.writerow(['A = Absent'])
    writer.writerow(['=' * 50])
    
    writer.writerow([])
    writer.writerow(['SESSION-WISE SUMMARY'])
    writer.writerow(['Date', 'Day', 'Total Present', 'Total Absent', 'Attendance Rate', 'Marked By'])
    
    for att_date in attendance_dates:
        present_on_date = sum(1 for dates in attendance_matrix.values() 
                             if att_date in dates and dates[att_date]['status'] == 'P')
        total_students = len(attendance_matrix)
        absent_on_date = total_students - present_on_date
        attendance_rate = (present_on_date / total_students * 100) if total_students > 0 else 0
        
        # FIX B1: Handle empty markers list safely
        markers = [dates[att_date]['marked_by'] for dates in attendance_matrix.values() 
                  if att_date in dates and dates[att_date]['marked_by']]
        if markers:
            most_common_marker = max(set(markers), key=markers.count)
        else:
            most_common_marker = 'N/A'
        
        writer.writerow([
            att_date.strftime('%Y-%m-%d'),
            att_date.strftime('%A'),
            present_on_date,
            absent_on_date,
            f"{attendance_rate:.1f}%",
            most_common_marker
        ])
    
    include_time_log = request.GET.get('include_time_log', 'false').lower() == 'true'
    
    if include_time_log and attendance_records.exists():
        writer.writerow([])
        writer.writerow(['=' * 50])
        writer.writerow(['DETAILED TIME LOG'])
        writer.writerow(['=' * 50])
        writer.writerow(['Date', 'Time (PKT)', 'Registration ID', 'Student Name', 'Section', 'Status', 'Marked By', 'Notes'])
        
        for record in attendance_records:
            if record.attendance_time:
                try:
                    dt = datetime.combine(record.attendance_date, record.attendance_time)
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                    dt_pk = dt.astimezone(pk_timezone)
                    time_str = dt_pk.strftime('%I:%M:%S %p')
                except Exception:
                    time_str = 'N/A'
            else:
                time_str = 'N/A'
            
            writer.writerow([
                record.attendance_date.strftime('%Y-%m-%d'),
                time_str,
                record.student.registration_id,
                record.student.get_full_name(),
                record.student.section.section_name if record.student.section else 'N/A',
                'Present' if record.status == AttendanceRecord.StatusChoices.PRESENT else 'Absent',
                record.marked_by.username if record.marked_by else 'System',
                record.notes or ''
            ])
    
    writer.writerow([])
    writer.writerow(['=' * 50])
    writer.writerow(['END OF REPORT'])
    writer.writerow([f'Generated by Pro Attendance System on {current_time_pk.strftime("%Y-%m-%d %I:%M:%S %p PKT")}'])
    writer.writerow(['=' * 50])
    
    return response

@require_POST
@login_required
def upload_csv(request: HttpRequest) -> JsonResponse:
    """Handles CSV upload for Student roster with section support."""
    csv_file = request.FILES.get('csvFile')
    if not csv_file:
        return JsonResponse({'success': False, 'message': 'No CSV file provided.'}, status=400)
    
    try:
        file_content = csv_file.read()
        decoded_file = None
        
        for encoding in ['utf-8-sig', 'utf-8', 'latin-1', 'iso-8859-1']:
            try:
                decoded_file = file_content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        
        if not decoded_file:
            return JsonResponse({'success': False, 'message': 'Unable to decode CSV file.'}, status=400)
        
        reader = csv.reader(decoded_file.splitlines())
        header = next(reader, None)
        
        if not header:
            return JsonResponse({'success': False, 'message': 'CSV file is empty or has no header row.'}, status=400)
        
        if len(header) < 8:
            return JsonResponse({'success': False, 'message': 'CSV must have at least 8 columns.'}, status=400)
        
        created_count = 0
        updated_count = 0
        sections_created = 0
        error_rows = []
        
        with transaction.atomic():
            for row_num, row in enumerate(reader, start=2):
                try:
                    if len(row) < 8:
                        error_rows.append(f"Row {row_num}: Insufficient columns")
                        continue
                    
                    reg_id = row[0].strip()
                    first_name = row[1].strip()
                    last_name = row[2].strip()
                    batch_code = row[3].strip()
                    section_name = row[4].strip() or None
                    email = row[5].strip() or None
                    embedding_file = row[6].strip() or None
                    images_folder_path = row[7].strip() or None
                    
                    if not all([reg_id, first_name, last_name, batch_code]):
                        error_rows.append(f"Row {row_num}: Missing required fields")
                        continue
                    
                    batch, _ = Batch.objects.get_or_create(
                        batch_code=batch_code,
                        defaults={
                            'batch_name': f'Batch {batch_code}',
                            'program': 'Computer Science',
                            'degree_level': "Bachelor's",
                            'start_year': 2020,
                            'end_year': 2024,
                        }
                    )
                    
                    section = None
                    if section_name:
                        section, section_created = Section.objects.get_or_create(
                            batch=batch,
                            section_name=section_name,
                            defaults={'is_active': True}
                        )
                        if section_created:
                            sections_created += 1
                    
                    student, created = Student.objects.update_or_create(
                        registration_id=reg_id,
                        defaults={
                            'first_name': first_name,
                            'last_name': last_name,
                            'batch': batch,
                            'section': section,
                            'email': email,
                            'face_embedding_file': embedding_file,
                            'images_folder_path': images_folder_path,
                            'is_active': True
                        }
                    )
                    
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1
                        
                except Exception as e:
                    error_rows.append(f"Row {row_num}: {str(e)}")
                    if len(error_rows) > 10:
                        error_rows.append("... and more errors")
                        break
        
        load_embeddings_from_db()
        
        msg_parts = []
        if created_count > 0:
            msg_parts.append(f"{created_count} students created")
        if updated_count > 0:
            msg_parts.append(f"{updated_count} students updated")
        if sections_created > 0:
            msg_parts.append(f"{sections_created} sections created")
        if error_rows:
            msg_parts.append(f"{len(error_rows)} errors")
            
        message = f'CSV processed: {", ".join(msg_parts)}.'
        if error_rows and len(error_rows) <= 10:
            message += f' Errors: {"; ".join(error_rows)}'
            
        return JsonResponse({
            'success': len(error_rows) == 0,
            'message': message,
            'created': created_count,
            'updated': updated_count,
            'sections_created': sections_created,
            'errors': len(error_rows)
        })
        
    except Exception as e:
        logger.error(f"CSV upload failed: {e}")
        logger.error(traceback.format_exc())
        return JsonResponse({'success': False, 'message': f'An error occurred: {str(e)}'}, status=500)


@login_required
@require_POST
def set_classroom_override(request: HttpRequest) -> JsonResponse:
    """Set or clear a temporary classroom override for a class."""
    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        return JsonResponse({'error': 'Teacher profile not found.'}, status=403)
    
    class_id = request.POST.get('class_id')
    action = request.POST.get('action', 'set')

    if not class_id:
        return JsonResponse({'error': 'A Class ID must be provided.'}, status=400)

    try:
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=teacher)
    except (Class.DoesNotExist, ValueError, TypeError, Http404):
        return JsonResponse({'error': 'Invalid or unauthorized class.'}, status=403)
    
    # FIX B2: Use override_date from POST if provided, fallback to today
    override_date_str = request.POST.get('override_date')
    if override_date_str:
        try:
            override_date = datetime.strptime(override_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            override_date = timezone.now().date()
    else:
        override_date = timezone.now().date()
    
    if action == 'clear':
        ClassroomOverride.objects.filter(
            class_instance=class_instance,
            override_date=override_date
        ).update(is_active=False)
        
        return JsonResponse({
            'success': True,
            'message': 'Classroom override cleared. Using default classroom.'
        })
    
    elif action == 'set':
        temporary_classroom = request.POST.get('temporary_classroom')
        temporary_rtsp_url = request.POST.get('temporary_rtsp_url')
        reason = request.POST.get('reason', 'Classroom/camera unavailable')
        
        if not temporary_classroom:
            return JsonResponse({'error': 'Temporary classroom is required.'}, status=400)
        
        override, created = ClassroomOverride.objects.update_or_create(
            class_instance=class_instance,
            override_date=override_date,
            defaults={
                'original_classroom': class_instance.classroom or 'Not specified',
                'temporary_classroom': temporary_classroom,
                'temporary_rtsp_url': temporary_rtsp_url,
                'reason': reason,
                'created_by': request.user,
                'is_active': True
            }
        )
        
        action_msg = 'created' if created else 'updated'
        return JsonResponse({
            'success': True,
            'message': f'Classroom override {action_msg} for {override_date}. Using {temporary_classroom}.',
            'override_id': override.pk
        })
    
    return JsonResponse({'error': 'Invalid action.'}, status=400)


@login_required
@require_http_methods(["GET", "POST"])
def enrollment_api(request: HttpRequest, class_id: int) -> JsonResponse:
    """API endpoint to manage enrollments for a class"""
    try:
        class_instance = get_object_or_404(Class, pk=class_id, instructors=request.user.teacher)
    except (Class.DoesNotExist, Teacher.DoesNotExist):
        return JsonResponse({'error': 'Class not found or permission denied.'}, status=404)
    
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            student_ids = data.get('student_ids', [])
            enrollment_type = data.get('enrollment_type', 'REG')
            action = data.get('action', 'enroll')
            
            if action == 'enroll':
                enrolled = []
                for student_id in student_ids:
                    try:
                        student = Student.objects.get(pk=student_id)
                        enrollment, created = Enrollment.objects.get_or_create(
                            student=student,
                            class_instance=class_instance,
                            defaults={
                                'enrollment_type': enrollment_type,
                                'is_active': True
                            }
                        )
                        if created:
                            enrolled.append(student.registration_id)
                    except Student.DoesNotExist:
                        continue
                
                return JsonResponse({
                    'success': True,
                    'message': f'Enrolled {len(enrolled)} students',
                    'enrolled': enrolled
                })
            
            elif action == 'unenroll':
                Enrollment.objects.filter(
                    student_id__in=student_ids,
                    class_instance=class_instance
                ).update(is_active=False)
                
                return JsonResponse({
                    'success': True,
                    'message': f'Unenrolled {len(student_ids)} students'
                })
                
        except Exception as e:
            logger.exception(f"Error managing enrollment: {e}")
            return JsonResponse({'error': 'Server error'}, status=500)
    
    enrollments = Enrollment.objects.filter(
        class_instance=class_instance,
        is_active=True
    ).select_related('student')
    
    data = [{
        'student_id': e.student.pk,
        'registration_id': e.student.registration_id,
        'name': e.student.get_full_name(),
        'section': e.student.section.section_name if e.student.section else None,
        'enrollment_type': e.get_enrollment_type_display(),
        'enrollment_date': e.enrollment_date.strftime('%Y-%m-%d')
    } for e in enrollments]
    
    return JsonResponse({'enrollments': data})

@login_required
def video_feed(request: HttpRequest) -> StreamingHttpResponse:
    """Serves the MJPEG video stream for a specific class."""
    class_id = request.GET.get('class_id')
    if not class_id:
        return HttpResponse("Class ID required", status=400)
    
    try:
        class_instance = get_object_or_404(Class, pk=class_id, instructors=request.user.teacher)
        
        today = timezone.now().date()
        active_override = ClassroomOverride.objects.filter(
            class_instance=class_instance,
            override_date=today,
            is_active=True
        ).first()
        
        if active_override and active_override.temporary_rtsp_url:
            rtsp_url = active_override.temporary_rtsp_url
        elif class_instance.has_stream:
            rtsp_url = class_instance.rtsp_stream_url
        else:
            return HttpResponse("No stream configured for this classroom", status=503)
        
    except (Class.DoesNotExist, Teacher.DoesNotExist):
        return HttpResponse("Unauthorized", status=403)
    
    def gen_frames():
        consecutive_failures = 0
        max_consecutive_failures = 2  # Fail fast -- don't hang for minutes
        
        while consecutive_failures < max_consecutive_failures:
            try:
                with capture_rtsp(rtsp_url, timeout_sec=5) as cap:
                    if not cap:
                        consecutive_failures += 1
                        # Show error frame immediately on first failure
                        error_frame = generate_error_frame()
                        if error_frame:
                            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
                        time.sleep(min(STREAM_RETRY_DELAY, 3))
                        continue
                    
                    consecutive_failures = 0
                    frame_count = 0
                    read_failures = 0
                    max_read_failures = 10
                    
                    while read_failures < max_read_failures:
                        try:
                            ret, frame = cap.read()
                            if not ret or frame is None:
                                read_failures += 1
                                time.sleep(0.1)
                                continue
                            
                            read_failures = 0
                            frame_count += 1
                            if frame_count % 2 == 0:
                                continue
                            
                            frame = cv2.resize(frame, (640, 480))
                            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
                            time.sleep(STREAM_FRAME_DELAY)
                            
                        except Exception as frame_err:
                            logger.error(f"Error processing frame: {frame_err}")
                            read_failures += 1
                    
                    if read_failures >= max_read_failures:
                        consecutive_failures += 1
                        
            except Exception as e:
                logger.exception(f"Stream generator error: {e}")
                consecutive_failures += 1
                time.sleep(STREAM_RETRY_DELAY)
        
        error_frame = generate_error_frame()
        if error_frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
    
    return StreamingHttpResponse(gen_frames(), content_type='multipart/x-mixed-replace; boundary=frame')

# --- API Views for Course and Class Management ---
@login_required
@require_http_methods(["GET", "POST"])
def course_api_list(request: HttpRequest) -> JsonResponse:
    """API: List (GET) and create (POST) Course templates."""
    if not hasattr(request.user, 'teacher'):
        return JsonResponse({'error': 'Instructor profile required.'}, status=403)

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            title = data.get('title', '').strip()
            course_code = data.get('course_code', '').strip()
            if not title or not course_code:
                return JsonResponse({'error': 'Title and Course Code are required.'}, status=400)

            new_course, created = Course.objects.get_or_create(
                course_code=course_code,
                defaults={'title': title, 'description': data.get('description', '')}
            )
            if not created:
                return JsonResponse({'error': f'Course with code {course_code} already exists.'}, status=409)
            
            return JsonResponse({'id': new_course.pk, 'title': new_course.title, 'course_code': new_course.course_code}, status=201)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON.'}, status=400)
        except Exception as e:
            logger.exception(f"Error creating course: {e}")
            return JsonResponse({'error': 'Server error creating course.'}, status=500)
    
    courses = Course.objects.all().order_by('course_code')
    data = [{'id': c.pk, 'title': c.title, 'course_code': c.course_code} for c in courses]
    return JsonResponse(data, safe=False)

@login_required
@require_http_methods(["DELETE"])
def class_api_detail(request: HttpRequest, pk: int) -> JsonResponse:
    """API: Delete (DELETE) a specific Class instance."""
    try:
        class_instance = get_object_or_404(Class, pk=pk, instructors=request.user.teacher)
        class_instance.delete()
        return HttpResponse(status=204)
    except (Http404, Teacher.DoesNotExist):
        return JsonResponse({'error': 'Class not found or permission denied.'}, status=404)
    except Exception as e:
        logger.exception(f"API Class Delete Error for pk={pk}: {e}")
        return JsonResponse({'error': 'Server error while deleting class.'}, status=500)

# ═══════════════════════════════════════════════════════════════════
# FACE PIPELINE VIEWS (Modular Add-on)
# ═══════════════════════════════════════════════════════════════════

from .models import Identity, FaceSample, ExtractionSession

@login_required
def face_manager_view(request: HttpRequest) -> HttpResponse:
    """Face Pipeline Management UI -- extraction, labeling, correction."""
    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        return redirect("Attendance:login")
    
    class_id = request.GET.get('class_id')
    selected_class = None
    identities = []
    enrolled_students = []
    sessions = []
    
    if class_id:
        try:
            selected_class = get_object_or_404(Class, pk=int(class_id), instructors=teacher)
            identities = Identity.objects.filter(
                class_instance=selected_class, is_active=True
            ).prefetch_related('samples').order_by('auto_label')
            
            enrolled_students = Student.objects.filter(
                enrollments__class_instance=selected_class,
                enrollments__is_active=True, is_active=True
            ).distinct().order_by('last_name', 'first_name')
            
            sessions = ExtractionSession.objects.filter(
                class_instance=selected_class
            ).order_by('-created_at')[:20]
        except (ValueError, Http404):
            messages.error(request, "Class not found.")
    
    # Get all classes for selection
    classes = Class.objects.filter(instructors=teacher, is_active=True).select_related('course')
    
    context = {
        'selected_class': selected_class,
        'selected_class_id': class_id,
        'identities': identities,
        'enrolled_students': enrolled_students,
        'sessions': sessions,
        'classes': classes,
        'MEDIA_URL': settings.MEDIA_URL,
    }
    return render(request, "Attendance/face_manager.html", context)


@login_required
@require_POST
def pipeline_extract(request: HttpRequest) -> JsonResponse:
    """API: Extract faces from stream or uploaded image."""
    from . import pipeline as pl
    
    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    class_id = request.POST.get('class_id')
    source_type = request.POST.get('source_type', 'upload').lower().strip()
    
    if not class_id:
        return JsonResponse({'error': 'class_id required'}, status=400)

    try:
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=teacher)
    except (Http404, ValueError, TypeError):
        return JsonResponse({'error': 'Class not found'}, status=404)

    # Create session record
    session = ExtractionSession.objects.create(
        class_instance=class_instance,
        source_type='STREAM' if source_type == 'stream' else 'UPLOAD',
        created_by=request.user,
        status='processing',
    )
    
    try:
        if source_type == 'stream':
            # Get RTSP URL
            today = timezone.now().date()
            override = ClassroomOverride.objects.filter(
                class_instance=class_instance, override_date=today, is_active=True
            ).first()
            
            rtsp_url = None
            if override and override.temporary_rtsp_url:
                rtsp_url = override.temporary_rtsp_url
            elif class_instance.has_stream:
                rtsp_url = class_instance.rtsp_stream_url
            
            if not rtsp_url:
                session.status = 'failed'
                session.notes = 'No stream configured'
                session.save()
                return JsonResponse({'error': 'No stream configured'}, status=400)
            
            faces = pl.extract_faces_from_stream(rtsp_url, num_frames=10)
            session.frames_captured = 10
            
        elif source_type == 'upload':
            image_file = request.FILES.get('image')
            if not image_file:
                session.status = 'failed'
                session.notes = 'No image provided'
                session.save()
                return JsonResponse({'error': 'No image file'}, status=400)
            
            from PIL import UnidentifiedImageError as UIE
            try:
                pil_image = Image.open(image_file).convert('RGB')
            except (UIE, Exception):
                session.status = 'failed'
                session.save()
                return JsonResponse({'error': 'Invalid image'}, status=400)
            
            faces = pl.extract_faces_from_image(pil_image)
            session.frames_captured = 1
        else:
            return JsonResponse({'error': 'Invalid source_type'}, status=400)
        
        session.faces_extracted = len(faces)
        
        if not faces:
            session.status = 'completed'
            session.notes = 'No faces detected'
            session.save()
            return JsonResponse({
                'success': True,
                'faces_extracted': 0,
                'identities_created': 0,
                'message': 'No faces detected in the input.'
            })
        
        # Initialize identities
        result = pl.initialize_identities(int(class_id), faces, session_id=session.pk)
        
        session.status = 'completed'
        session.notes = f"{result['identities_created']} identities created"
        session.save()
        
        return JsonResponse({
            'success': True,
            'session_id': session.pk,
            'faces_extracted': len(faces),
            'identities_created': result['identities_created'],
            'samples_saved': result['samples_saved'],
            'errors': result['errors'],
            'message': f"Extracted {len(faces)} faces, created {result['identities_created']} identities."
        })
        
    except Exception as e:
        logger.exception(f"Pipeline extraction error: {e}")
        session.status = 'failed'
        session.notes = str(e)
        session.save()
        return JsonResponse({'error': f'Extraction failed: {str(e)}'}, status=500)


@login_required
@require_POST
def pipeline_label(request: HttpRequest) -> JsonResponse:
    """API: Label an identity with a student."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    identity_id = data.get('identity_id')
    student_id = data.get('student_id')
    label_text = data.get('label', '')
    
    if not identity_id:
        return JsonResponse({'error': 'identity_id required'}, status=400)
    
    success = pl.label_identity(int(identity_id), student_id=int(student_id) if student_id else None, label=label_text)
    
    if success:
        # Reload embeddings so attendance system picks up the change
        load_embeddings_from_db()
        return JsonResponse({'success': True, 'message': 'Identity labeled successfully'})
    else:
        return JsonResponse({'error': 'Labeling failed'}, status=400)


@login_required
@require_POST
def pipeline_move_sample(request: HttpRequest) -> JsonResponse:
    """API: Move a face sample to a different identity."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    sample_id = data.get('sample_id')
    target_identity_id = data.get('target_identity_id')
    
    if not sample_id or not target_identity_id:
        return JsonResponse({'error': 'sample_id and target_identity_id required'}, status=400)
    
    success = pl.move_sample(int(sample_id), int(target_identity_id))
    return JsonResponse({'success': success})


@login_required
@require_POST
def pipeline_invalidate_sample(request: HttpRequest) -> JsonResponse:
    """API: Mark a sample as invalid."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    sample_id = data.get('sample_id')
    if not sample_id:
        return JsonResponse({'error': 'sample_id required'}, status=400)
    
    success = pl.invalidate_sample(int(sample_id))
    return JsonResponse({'success': success})


@login_required
@require_POST
def pipeline_add_sample(request: HttpRequest) -> JsonResponse:
    """API: Add a new face sample to an identity."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    identity_id = data.get('identity_id')
    image_data = data.get('image_data')
    
    if not identity_id or not image_data:
        return JsonResponse({'error': 'identity_id and image_data required'}, status=400)
    
    success = pl.add_sample_to_identity(int(identity_id), image_data, source='manual_ui')
    return JsonResponse({'success': success})


@login_required
@require_POST
def pipeline_enroll_student(request: HttpRequest) -> JsonResponse:
    """API: Enroll a student by uploading their photo directly.
    For students never captured by CCTV."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    student_id = data.get('student_id')
    class_id = data.get('class_id')
    image_data = data.get('image_data')
    
    if not all([student_id, class_id, image_data]):
        return JsonResponse({'error': 'student_id, class_id, and image_data required'}, status=400)
    
    result = pl.enroll_student_from_photo(int(student_id), int(class_id), image_data)
    return JsonResponse(result)


@login_required
@require_POST
def pipeline_manual_capture(request: HttpRequest) -> JsonResponse:
    """API: Process a manually drawn face selection from a stream frame."""
    from . import pipeline as pl
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    image_data = data.get('image_data')
    crop_box = data.get('crop_box')
    student_id = data.get('student_id')
    class_id = data.get('class_id')
    
    if not all([image_data, crop_box, student_id, class_id]):
        return JsonResponse({'error': 'image_data, crop_box, student_id, and class_id required'}, status=400)
    
    result = pl.process_manual_crop(image_data, crop_box, int(student_id), int(class_id))
    return JsonResponse(result)


@login_required
@require_POST
def pipeline_retrain(request: HttpRequest) -> JsonResponse:
    """API: Retrain embeddings for one identity or all flagged in a class.

    Requires either identity_id or class_id and validates the requesting teacher
    is an instructor for that class. Prevents unauthorized cross-class retraining.
    """
    from . import pipeline as pl

    try:
        teacher = request.user.teacher
    except Teacher.DoesNotExist:
        return JsonResponse({'error': 'Teacher profile required.'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    identity_id = data.get('identity_id')
    class_id = data.get('class_id')

    if not identity_id and not class_id:
        return JsonResponse({'error': 'identity_id or class_id required.'}, status=400)

    try:
        if identity_id:
            identity = get_object_or_404(
                Identity, pk=int(identity_id),
                class_instance__instructors=teacher,
            )
            result = pl.retrain_identity(identity.pk)
        else:
            class_instance = get_object_or_404(
                Class, pk=int(class_id), instructors=teacher,
            )
            result = pl.retrain_all_flagged(class_instance.pk)
    except (Http404, ValueError, TypeError):
        return JsonResponse({'error': 'Not found or unauthorized.'}, status=404)

    # Reload global embeddings
    load_embeddings_from_db()

    return JsonResponse(result)


@login_required
def pipeline_identities_api(request: HttpRequest) -> JsonResponse:
    """API: Get identities, samples, and students missing identities."""
    class_id = request.GET.get('class_id')
    if not class_id:
        return JsonResponse({'error': 'class_id required'}, status=400)
    
    try:
        teacher = request.user.teacher
        class_instance = get_object_or_404(Class, pk=int(class_id), instructors=teacher)
    except (Teacher.DoesNotExist, Http404, ValueError, TypeError):
        return JsonResponse({'error': 'Not found'}, status=404)
    
    identities = Identity.objects.filter(
        class_instance=class_instance, is_active=True
    ).prefetch_related('samples')
    
    # Students already linked to identities
    linked_student_ids = set(
        Identity.objects.filter(
            class_instance=class_instance, is_active=True, student__isnull=False
        ).values_list('student_id', flat=True)
    )
    
    # Enrolled students WITHOUT identities
    all_enrolled = Student.objects.filter(
        enrollments__class_instance=class_instance,
        enrollments__is_active=True, is_active=True
    ).distinct()
    
    missing_students = []
    for student in all_enrolled:
        if student.pk not in linked_student_ids:
            missing_students.append({
                'id': student.pk,
                'name': student.get_full_name(),
                'reg_id': student.registration_id,
                'has_embedding': bool(student.face_embedding_file),
            })
    
    data = []
    for identity in identities:
        samples = []
        for s in identity.samples.filter(is_valid=True).order_by('-quality_score')[:20]:
            samples.append({
                'id': s.pk,
                'image_url': s.image_url,
                'quality': s.quality_score,
                'created_at': s.created_at.isoformat(),
            })
        
        rep_image_url = ''
        if identity.representative_image:
            media_root_str = str(settings.MEDIA_ROOT)
            img_path = str(identity.representative_image)
            if img_path.startswith(media_root_str):
                relative = img_path[len(media_root_str):].replace(os.sep, '/').lstrip('/')
                rep_image_url = f"{settings.MEDIA_URL}{relative}"
        
        data.append({
            'id': identity.pk,
            'auto_label': identity.auto_label,
            'label': identity.label,
            'display_name': identity.display_name,
            'is_labeled': identity.is_labeled,
            'student_id': identity.student_id,
            'student_name': identity.student.get_full_name() if identity.student else None,
            'sample_count': identity.sample_count,
            'needs_retraining': identity.needs_retraining,
            'representative_image': rep_image_url,
            'samples': samples,
        })
    
    return JsonResponse({
        'identities': data,
        'missing_students': missing_students,
        'total_enrolled': all_enrolled.count(),
        'total_linked': len(linked_student_ids),
    })
