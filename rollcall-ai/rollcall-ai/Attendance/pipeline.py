# Attendance/pipeline.py
"""
Face Pipeline Engine
====================
Modular add-on for automated face extraction, embedding generation,
identity initialization, and incremental retraining.

Does NOT import or modify any existing view functions.
"""

import os
import time
import logging
import hashlib
import numpy as np
import cv2
from io import BytesIO
from typing import List, Dict, Tuple, Optional, Any
from PIL import Image, ImageEnhance

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# ─── Lazy imports of ML models (shared with views.py globals) ───
def _get_models():
    """Retrieve initialized models from the views module (avoids re-initialization)."""
    from . import views as v
    if v.FACE_EMBEDDER is None:
        v.initialize_face_models()
    return v.FACE_EMBEDDER, v.FACE_DETECTOR, v.RetinaFace, v.RETINAFACE_AVAILABLE

# ─── Constants ───
PIPELINE_BASE = os.path.join(settings.MEDIA_ROOT, 'face_pipeline')
MIN_FACE_PX = 20  # minimum face width/height to keep
EXTRACTION_MARGIN = 0.4  # 40% margin around detected face for storage
EMBEDDING_DIM = 512
QUALITY_BLUR_THRESHOLD = 15.0  # Laplacian variance below this = blurry


# ═══════════════════════════════════════════════════════════════════
# § FACE EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def extract_faces_from_frame(cv2_bgr: np.ndarray, detection_threshold: float = 0.5) -> List[Dict[str, Any]]:
    """Detect and extract all faces from a single BGR frame.
    
    Returns list of dicts: {
        'crop_bgr': np.ndarray,     # face crop with margin (BGR)
        'crop_pil': PIL.Image,      # same as PIL RGB
        'bbox': [x1,y1,x2,y2],     # original detection bbox
        'confidence': float,
        'quality_score': float,      # 0-100
        'face_size': (w, h),
    }
    """
    embedder, detector, RetinaFace, retina_available = _get_models()
    
    if not retina_available or RetinaFace is None:
        logger.error("RetinaFace not available for extraction")
        return []
    
    results = []
    
    try:
        faces_data = RetinaFace.detect_faces(img_path=cv2_bgr, threshold=detection_threshold)
        
        if not faces_data or not isinstance(faces_data, dict):
            return []
        
        img_h, img_w = cv2_bgr.shape[:2]
        
        for face_key, face_info in faces_data.items():
            try:
                bbox = face_info.get('facial_area', [])
                if len(bbox) < 4:
                    continue
                
                x1, y1, x2, y2 = map(int, bbox[:4])
                face_w = x2 - x1
                face_h = y2 - y1
                
                if face_w < MIN_FACE_PX or face_h < MIN_FACE_PX:
                    continue
                
                confidence = face_info.get('score', 0)
                if confidence < detection_threshold:
                    continue
                
                # Crop with generous margin for storage (more context = better future embeddings)
                margin_x = int(face_w * EXTRACTION_MARGIN)
                margin_y = int(face_h * EXTRACTION_MARGIN)
                cx1 = max(0, x1 - margin_x)
                cy1 = max(0, y1 - margin_y)
                cx2 = min(img_w, x2 + margin_x)
                cy2 = min(img_h, y2 + margin_y)
                
                crop_bgr = cv2_bgr[cy1:cy2, cx1:cx2]
                if crop_bgr.size == 0:
                    continue
                
                # Quality score
                quality = _compute_quality(crop_bgr, confidence, face_w, face_h)
                
                crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
                crop_pil = Image.fromarray(crop_rgb)
                
                results.append({
                    'crop_bgr': crop_bgr.copy(),
                    'crop_pil': crop_pil,
                    'bbox': [x1, y1, x2, y2],
                    'confidence': float(confidence),
                    'quality_score': quality,
                    'face_size': (face_w, face_h),
                })
            except Exception as e:
                logger.debug(f"Face extraction error: {e}")
                continue
    
    except Exception as e:
        logger.error(f"Frame extraction failed: {e}")
    
    return results


def extract_faces_from_stream(rtsp_url: str, num_frames: int = 8, frame_delay: float = 0.2) -> List[Dict[str, Any]]:
    """Capture multiple frames from RTSP stream, extract faces from all, deduplicate."""
    from .views import capture_rtsp
    
    all_faces = []
    
    try:
        with capture_rtsp(rtsp_url) as cap:
            if cap is None:
                logger.error("Could not open stream for extraction")
                return []
            
            # Skip stale buffered frames
            for _ in range(5):
                cap.read()
            
            frames_captured = 0
            for frame_num in range(num_frames):
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.1)
                    continue
                
                frames_captured += 1
                faces = extract_faces_from_frame(frame)
                
                for face in faces:
                    face['source_frame'] = f'stream_frame_{frame_num:03d}'
                    all_faces.append(face)
                
                if frame_num < num_frames - 1:
                    time.sleep(frame_delay)
            
            logger.info(f"Stream extraction: {frames_captured} frames, {len(all_faces)} total face crops")
    
    except Exception as e:
        logger.error(f"Stream extraction failed: {e}")
    
    return all_faces


def extract_faces_from_image(pil_image: Image.Image) -> List[Dict[str, Any]]:
    """Extract all faces from an uploaded image."""
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')
    
    cv2_bgr = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
    faces = extract_faces_from_frame(cv2_bgr)
    
    for face in faces:
        face['source_frame'] = 'uploaded_image'
    
    return faces


def _compute_quality(crop_bgr: np.ndarray, det_confidence: float, face_w: int, face_h: int) -> float:
    """Quality score 0-100 combining confidence, size, and sharpness."""
    score = 0.0
    score += det_confidence * 35  # max 35
    
    size_score = min((face_w * face_h) / (100.0 * 100.0), 1.0)
    score += size_score * 30  # max 30
    
    try:
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        sharp_score = min(lap_var / 150.0, 1.0)
        score += sharp_score * 35  # max 35
    except Exception:
        score += 15  # assume mid-sharpness on error
    
    return round(score, 1)


# ═══════════════════════════════════════════════════════════════════
# § EMBEDDING GENERATION
# ═══════════════════════════════════════════════════════════════════

def generate_embedding(crop_bgr: np.ndarray) -> Optional[np.ndarray]:
    """Generate a single L2-normalized embedding from a face crop using TTA."""
    embedder = _get_models()[0]
    if embedder is None:
        return None
    
    try:
        # Ensure minimum size
        h, w = crop_bgr.shape[:2]
        if w < 150 or h < 150:
            scale = max(150.0 / w, 150.0 / h)
            crop_bgr = cv2.resize(crop_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        
        # Original
        emb_orig = _get_best_embedding(embedder, crop_bgr)
        
        # Flipped (TTA)
        flipped = cv2.flip(crop_bgr, 1)
        emb_flip = _get_best_embedding(embedder, flipped)
        
        if emb_orig is not None and emb_flip is not None:
            fused = (emb_orig + emb_flip) / 2.0
        elif emb_orig is not None:
            fused = emb_orig
        elif emb_flip is not None:
            fused = emb_flip
        else:
            return None
        
        # L2 normalize
        norm = np.linalg.norm(fused)
        if norm > 0:
            fused = fused / norm
        
        return fused.reshape(1, -1)
    
    except Exception as e:
        logger.debug(f"Embedding generation failed: {e}")
        return None


def _get_best_embedding(embedder, crop_bgr: np.ndarray) -> Optional[np.ndarray]:
    """Extract the center-most face embedding from a crop."""
    faces = embedder.get(crop_bgr)
    if not faces:
        return None
    
    if len(faces) == 1:
        f = faces[0]
        if hasattr(f, 'embedding') and f.embedding is not None:
            return f.embedding.flatten()
        return None
    
    # Pick center-most
    h, w = crop_bgr.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    best = None
    best_d = float('inf')
    for f in faces:
        if not hasattr(f, 'embedding') or f.embedding is None:
            continue
        fb = f.bbox
        fx = (fb[0] + fb[2]) / 2.0
        fy = (fb[1] + fb[3]) / 2.0
        d = (fx - cx) ** 2 + (fy - cy) ** 2
        if d < best_d:
            best_d = d
            best = f.embedding.flatten()
    return best


def aggregate_embeddings(embedding_list: List[np.ndarray]) -> Optional[np.ndarray]:
    """Compute mean embedding from multiple samples and L2-normalize.
    This is the standard approach for building a stable identity reference."""
    if not embedding_list:
        return None
    
    valid = [e for e in embedding_list if e is not None and e.size > 0]
    if not valid:
        return None
    
    # Stack and mean
    stacked = np.vstack([e.reshape(1, -1) for e in valid])
    mean_emb = np.mean(stacked, axis=0)
    
    # L2 normalize
    norm = np.linalg.norm(mean_emb)
    if norm > 0:
        mean_emb = mean_emb / norm
    
    return mean_emb.reshape(1, -1)


# ═══════════════════════════════════════════════════════════════════
# § IDENTITY INITIALIZATION (Day 1)
# ═══════════════════════════════════════════════════════════════════

def initialize_identities(class_id: int, faces: List[Dict[str, Any]], session_id: int = None) -> Dict[str, Any]:
    """Create Identity records from extracted faces.
    
    Groups similar faces using embedding similarity, creates one Identity per group,
    saves sample images and embeddings to disk.
    
    Returns: {'identities_created': int, 'samples_saved': int, 'errors': [str]}
    """
    from .models import Identity, FaceSample, ExtractionSession, Class
    
    try:
        class_instance = Class.objects.get(pk=class_id)
    except Class.DoesNotExist:
        return {'identities_created': 0, 'samples_saved': 0, 'errors': ['Class not found']}
    
    # Generate embeddings for all faces
    face_embeddings = []
    for face in faces:
        emb = generate_embedding(face['crop_bgr'])
        face_embeddings.append(emb)
    
    # Group faces by similarity (simple greedy clustering)
    groups = _cluster_faces(faces, face_embeddings, threshold=0.55)
    
    # Create Identity for each group
    base_dir = os.path.join(PIPELINE_BASE, f'class_{class_id}')
    os.makedirs(base_dir, exist_ok=True)
    
    # Find next identity number (use MAX not COUNT to avoid collision after deletions)
    from django.db.models import Max
    max_result = Identity.objects.filter(class_instance=class_instance).aggregate(
        max_num=Max('auto_label')
    )
    # Parse "identity_005" -> 5; default to 0 if none exist
    existing_max = 0
    if max_result['max_num']:
        try:
            existing_max = int(max_result['max_num'].split('_')[-1])
        except (ValueError, IndexError):
            existing_max = Identity.objects.filter(class_instance=class_instance).count()
    
    identities_created = 0
    samples_saved = 0
    errors = []
    
    session = None
    if session_id:
        try:
            session = ExtractionSession.objects.get(pk=session_id)
        except ExtractionSession.DoesNotExist:
            pass
    
    for group_idx, group_face_indices in enumerate(groups):
        try:
            identity_num = existing_max + group_idx + 1
            auto_label = f'identity_{identity_num:03d}'
            
            identity_dir = os.path.join(base_dir, auto_label)
            os.makedirs(identity_dir, exist_ok=True)
            
            identity = Identity.objects.create(
                class_instance=class_instance,
                auto_label=auto_label,
                is_labeled=False,
                is_active=True,
                needs_retraining=False,
            )
            
            # Save samples and embeddings
            group_embeddings = []
            best_quality = -1
            best_image_path = ''
            
            for face_idx in group_face_indices:
                face = faces[face_idx]
                emb = face_embeddings[face_idx]
                
                # Save crop image
                timestamp = int(time.time() * 1000)
                img_hash = hashlib.md5(face['crop_bgr'].tobytes()[:1000]).hexdigest()[:6]
                img_filename = f'sample_{timestamp}_{img_hash}.jpg'
                img_path = os.path.join(identity_dir, img_filename)
                
                face['crop_pil'].save(img_path, 'JPEG', quality=95)
                
                # Save individual embedding
                emb_path = ''
                if emb is not None:
                    emb_filename = f'emb_{timestamp}_{img_hash}.npy'
                    emb_path = os.path.join(identity_dir, emb_filename)
                    np.save(emb_path, emb)
                    group_embeddings.append(emb)
                
                # Create FaceSample record
                FaceSample.objects.create(
                    identity=identity,
                    session=session,
                    image_path=img_path,
                    embedding_path=emb_path,
                    quality_score=face['quality_score'],
                    source_frame=face.get('source_frame', ''),
                    is_valid=True,
                )
                samples_saved += 1
                
                # Track best quality for representative image
                if face['quality_score'] > best_quality:
                    best_quality = face['quality_score']
                    best_image_path = img_path
            
            # Generate aggregated embedding
            agg_emb = aggregate_embeddings(group_embeddings)
            agg_path = ''
            if agg_emb is not None:
                agg_path = os.path.join(identity_dir, 'embedding.npy')
                np.save(agg_path, agg_emb)
            
            identity.embedding_file = agg_path
            identity.representative_image = best_image_path
            identity.sample_count = len(group_face_indices)
            identity.save()
            
            identities_created += 1
            
        except Exception as e:
            errors.append(f"Group {group_idx}: {str(e)}")
            logger.error(f"Identity creation error: {e}")
    
    logger.info(f"Initialization: {identities_created} identities, {samples_saved} samples for class {class_id}")
    
    return {
        'identities_created': identities_created,
        'samples_saved': samples_saved,
        'errors': errors,
    }


def _cluster_faces(faces: List[Dict], embeddings: List[Optional[np.ndarray]], threshold: float = 0.55) -> List[List[int]]:
    """Simple greedy clustering: group faces by embedding similarity.
    Faces without embeddings each become their own group."""
    from sklearn.metrics.pairwise import cosine_similarity
    
    n = len(faces)
    assigned = [False] * n
    groups = []
    
    for i in range(n):
        if assigned[i]:
            continue
        
        group = [i]
        assigned[i] = True
        
        if embeddings[i] is None:
            groups.append(group)
            continue
        
        for j in range(i + 1, n):
            if assigned[j] or embeddings[j] is None:
                continue
            
            try:
                sim = cosine_similarity(
                    embeddings[i].reshape(1, -1),
                    embeddings[j].reshape(1, -1)
                )[0][0]
                
                if sim > threshold:
                    group.append(j)
                    assigned[j] = True
            except Exception:
                continue
        
        groups.append(group)
    
    return groups


# ═══════════════════════════════════════════════════════════════════
# § LABELING & STUDENT LINKING
# ═══════════════════════════════════════════════════════════════════

def label_identity(identity_id: int, student_id: int = None, label: str = '') -> bool:
    """Assign a student or label to an identity and link the embedding to the Student record."""
    from .models import Identity, Student
    
    try:
        identity = Identity.objects.get(pk=identity_id)
    except Identity.DoesNotExist:
        return False
    
    if student_id:
        try:
            student = Student.objects.get(pk=student_id)
            identity.student = student
            identity.label = student.get_full_name()
            identity.is_labeled = True
            identity.save()
            
            # Copy aggregated embedding to the Student record location
            if identity.embedding_file and os.path.exists(identity.embedding_file):
                models_dir = getattr(settings, 'ATTENDANCE_MODELS_DIR',
                                     os.path.join(settings.BASE_DIR, 'Attendance', 'Models'))
                os.makedirs(models_dir, exist_ok=True)
                
                emb_filename = f'{student.registration_id}_embeddings.npy'
                target_path = os.path.join(models_dir, emb_filename)
                
                # Copy embedding
                import shutil
                shutil.copy2(identity.embedding_file, target_path)
                
                # Update student record
                student.face_embedding_file = emb_filename
                
                # Set images folder
                student.images_folder_path = identity.folder_path
                student.save()
                
                logger.info(f"Linked identity {identity.auto_label} -> student {student.registration_id}")
            
            return True
        except Student.DoesNotExist:
            return False
    
    elif label:
        identity.label = label
        identity.is_labeled = True
        identity.save()
        return True
    
    return False


# ═══════════════════════════════════════════════════════════════════
# § RETRAINING
# ═══════════════════════════════════════════════════════════════════

def retrain_identity(identity_id: int) -> Dict[str, Any]:
    """Recompute the aggregated embedding for an identity from all valid samples."""
    from .models import Identity, FaceSample
    
    try:
        identity = Identity.objects.get(pk=identity_id)
    except Identity.DoesNotExist:
        return {'success': False, 'error': 'Identity not found'}
    
    samples = FaceSample.objects.filter(identity=identity, is_valid=True)
    
    embeddings = []
    regenerated = 0
    
    for sample in samples:
        emb = None
        
        # Try loading existing embedding
        if sample.embedding_path and os.path.exists(sample.embedding_path):
            try:
                emb = np.load(sample.embedding_path)
            except Exception:
                pass
        
        # Regenerate if missing
        if emb is None and sample.image_path and os.path.exists(sample.image_path):
            try:
                crop_bgr = cv2.imread(sample.image_path)
                if crop_bgr is not None:
                    emb = generate_embedding(crop_bgr)
                    if emb is not None:
                        emb_path = sample.image_path.rsplit('.', 1)[0] + '.npy'
                        np.save(emb_path, emb)
                        sample.embedding_path = emb_path
                        sample.save()
                        regenerated += 1
            except Exception as e:
                logger.debug(f"Sample embedding regen failed: {e}")
        
        if emb is not None:
            embeddings.append(emb)
    
    if not embeddings:
        return {'success': False, 'error': 'No valid embeddings could be generated'}
    
    # Aggregate
    agg_emb = aggregate_embeddings(embeddings)
    if agg_emb is None:
        return {'success': False, 'error': 'Aggregation failed'}
    
    # Save
    identity_dir = identity.folder_path
    os.makedirs(identity_dir, exist_ok=True)
    agg_path = os.path.join(identity_dir, 'embedding.npy')
    np.save(agg_path, agg_emb)
    
    identity.embedding_file = agg_path
    identity.sample_count = len(embeddings)
    identity.needs_retraining = False
    identity.save()
    
    # If linked to student, update their embedding too
    if identity.student:
        models_dir = getattr(settings, 'ATTENDANCE_MODELS_DIR',
                             os.path.join(settings.BASE_DIR, 'Attendance', 'Models'))
        os.makedirs(models_dir, exist_ok=True)
        emb_filename = f'{identity.student.registration_id}_embeddings.npy'
        target_path = os.path.join(models_dir, emb_filename)
        
        import shutil
        shutil.copy2(agg_path, target_path)
        
        identity.student.face_embedding_file = emb_filename
        identity.student.save()
        
        logger.info(f"Updated embedding for student {identity.student.registration_id} ({len(embeddings)} samples)")
    
    return {
        'success': True,
        'samples_used': len(embeddings),
        'regenerated': regenerated,
    }


def retrain_all_flagged(class_id: int = None) -> Dict[str, Any]:
    """Retrain all identities that are flagged for retraining."""
    from .models import Identity
    
    queryset = Identity.objects.filter(needs_retraining=True, is_active=True)
    if class_id:
        queryset = queryset.filter(class_instance_id=class_id)
    
    results = {'retrained': 0, 'failed': 0, 'errors': []}
    
    for identity in queryset:
        result = retrain_identity(identity.pk)
        if result.get('success'):
            results['retrained'] += 1
        else:
            results['failed'] += 1
            results['errors'].append(f"{identity.display_name}: {result.get('error', 'unknown')}")
    
    return results


# ═══════════════════════════════════════════════════════════════════
# § SAMPLE MANAGEMENT (for correction UI)
# ═══════════════════════════════════════════════════════════════════

def move_sample(sample_id: int, target_identity_id: int) -> bool:
    """Move a face sample from one identity to another. Flags both for retraining."""
    from .models import FaceSample, Identity
    
    try:
        sample = FaceSample.objects.get(pk=sample_id)
        target = Identity.objects.get(pk=target_identity_id)
    except (FaceSample.DoesNotExist, Identity.DoesNotExist):
        return False
    
    source = sample.identity
    
    # Move file to target directory
    target_dir = target.folder_path
    os.makedirs(target_dir, exist_ok=True)
    
    if sample.image_path and os.path.exists(sample.image_path):
        import shutil
        base_name = os.path.basename(sample.image_path)
        new_path = os.path.join(target_dir, base_name)
        # Handle filename collision by appending timestamp
        if os.path.exists(new_path):
            name, ext = os.path.splitext(base_name)
            new_path = os.path.join(target_dir, f"{name}_{int(time.time())}{ext}")
        shutil.move(sample.image_path, new_path)
        sample.image_path = new_path
        
        # Move embedding file too
        if sample.embedding_path and os.path.exists(sample.embedding_path):
            emb_base = os.path.basename(sample.embedding_path)
            new_emb_path = os.path.join(target_dir, emb_base)
            if os.path.exists(new_emb_path):
                name, ext = os.path.splitext(emb_base)
                new_emb_path = os.path.join(target_dir, f"{name}_{int(time.time())}{ext}")
            shutil.move(sample.embedding_path, new_emb_path)
            sample.embedding_path = new_emb_path
    
    sample.identity = target
    sample.save()
    
    # Update counts and flag for retraining
    source.sample_count = source.samples.filter(is_valid=True).count()
    source.needs_retraining = True
    source.save()
    
    target.sample_count = target.samples.filter(is_valid=True).count()
    target.needs_retraining = True
    target.save()
    
    return True


def invalidate_sample(sample_id: int) -> bool:
    """Mark a sample as invalid (bad quality, wrong person). Flags identity for retraining."""
    from .models import FaceSample
    
    try:
        sample = FaceSample.objects.get(pk=sample_id)
    except FaceSample.DoesNotExist:
        return False
    
    sample.is_valid = False
    sample.save()
    
    identity = sample.identity
    identity.sample_count = identity.samples.filter(is_valid=True).count()
    identity.needs_retraining = True
    identity.save()
    
    return True


def add_sample_to_identity(identity_id: int, image_data: str, source: str = 'manual') -> bool:
    """Add a new face sample (base64 image) to an existing identity."""
    from .models import Identity, FaceSample
    import base64
    
    try:
        identity = Identity.objects.get(pk=identity_id)
    except Identity.DoesNotExist:
        return False
    
    try:
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        
        img_bytes = base64.b64decode(image_data)
        pil_img = Image.open(BytesIO(img_bytes)).convert('RGB')
        
        identity_dir = identity.folder_path
        os.makedirs(identity_dir, exist_ok=True)
        
        timestamp = int(time.time() * 1000)
        img_hash = hashlib.md5(img_bytes[:1000]).hexdigest()[:6]
        filename = f'sample_{source}_{timestamp}_{img_hash}.jpg'
        img_path = os.path.join(identity_dir, filename)
        pil_img.save(img_path, 'JPEG', quality=95)
        
        # Generate embedding
        crop_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        emb = generate_embedding(crop_bgr)
        emb_path = ''
        if emb is not None:
            emb_path = img_path.rsplit('.', 1)[0] + '.npy'
            np.save(emb_path, emb)
        
        FaceSample.objects.create(
            identity=identity,
            image_path=img_path,
            embedding_path=emb_path,
            quality_score=50.0,  # default for manual adds
            source_frame=source,
            is_valid=True,
        )
        
        identity.sample_count = identity.samples.filter(is_valid=True).count()
        identity.needs_retraining = True
        identity.save()
        
        return True
    
    except Exception as e:
        logger.error(f"Add sample failed: {e}")
        return False


def enroll_student_from_photo(student_id: int, class_id: int, image_data: str) -> Dict[str, Any]:
    """Create/update an identity for a specific student from an uploaded photo.
    
    This handles students who were never captured by CCTV:
    1. Detect face in the uploaded photo
    2. Create an Identity linked to the student (or add sample to existing)
    3. Generate embedding with TTA + L2 normalization
    4. Copy embedding to the student's .npy file
    5. Reload global embeddings
    
    Returns: {'success': bool, 'message': str, 'identity_id': int or None}
    """
    from .models import Identity, FaceSample, Student, Class
    import base64
    import shutil
    
    try:
        student = Student.objects.get(pk=student_id)
        class_instance = Class.objects.get(pk=class_id)
    except (Student.DoesNotExist, Class.DoesNotExist):
        return {'success': False, 'message': 'Student or class not found', 'identity_id': None}
    
    # Decode image
    try:
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        img_bytes = base64.b64decode(image_data)
        pil_img = Image.open(BytesIO(img_bytes)).convert('RGB')
    except Exception as e:
        return {'success': False, 'message': f'Invalid image: {e}', 'identity_id': None}
    
    # Detect faces in the photo
    cv2_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    faces = extract_faces_from_frame(cv2_bgr, detection_threshold=0.4)  # lower threshold for close-ups
    
    if not faces:
        return {'success': False, 'message': 'No face detected in the photo. Please upload a clear photo showing the face.', 'identity_id': None}
    
    # Use the best quality face (for close-up photos there should be just one)
    best_face = max(faces, key=lambda f: f['quality_score'])
    
    # Check if this student already has an identity in this class
    existing_identity = Identity.objects.filter(
        class_instance=class_instance, student=student, is_active=True
    ).first()
    
    if existing_identity:
        identity = existing_identity
    else:
        # Create new identity
        from django.db.models import Max
        max_result = Identity.objects.filter(class_instance=class_instance).aggregate(
            max_num=Max('auto_label')
        )
        existing_max = 0
        if max_result['max_num']:
            try:
                existing_max = int(max_result['max_num'].split('_')[-1])
            except (ValueError, IndexError):
                existing_max = Identity.objects.filter(class_instance=class_instance).count()
        
        auto_label = f'identity_{existing_max + 1:03d}'
        identity = Identity.objects.create(
            class_instance=class_instance,
            student=student,
            label=student.get_full_name(),
            auto_label=auto_label,
            is_labeled=True,
            is_active=True,
            needs_retraining=False,
        )
    
    # Save sample image
    identity_dir = identity.folder_path
    os.makedirs(identity_dir, exist_ok=True)
    
    timestamp = int(time.time() * 1000)
    img_hash = hashlib.md5(best_face['crop_bgr'].tobytes()[:1000]).hexdigest()[:6]
    img_filename = f'sample_enroll_{timestamp}_{img_hash}.jpg'
    img_path = os.path.join(identity_dir, img_filename)
    best_face['crop_pil'].save(img_path, 'JPEG', quality=95)
    
    # Generate embedding
    emb = generate_embedding(best_face['crop_bgr'])
    emb_path = ''
    if emb is not None:
        emb_path = os.path.join(identity_dir, f'emb_enroll_{timestamp}_{img_hash}.npy')
        np.save(emb_path, emb)
    
    FaceSample.objects.create(
        identity=identity,
        image_path=img_path,
        embedding_path=emb_path,
        quality_score=best_face['quality_score'],
        source_frame='student_enrollment_photo',
        is_valid=True,
    )
    
    # Update identity
    identity.sample_count = identity.samples.filter(is_valid=True).count()
    if not identity.representative_image or best_face['quality_score'] > 70:
        identity.representative_image = img_path
    identity.save()
    
    # Retrain this identity (recompute aggregated embedding from all samples)
    retrain_result = retrain_identity(identity.pk)
    
    if retrain_result.get('success'):
        # Reload global embeddings
        from . import views as v
        v.load_embeddings_from_db()
        
        return {
            'success': True,
            'message': f'Enrolled {student.get_full_name()} with {retrain_result["samples_used"]} sample(s). Embedding updated.',
            'identity_id': identity.pk,
        }
    else:
        return {
            'success': False,
            'message': f'Face saved but embedding generation failed: {retrain_result.get("error", "unknown")}',
            'identity_id': identity.pk,
        }


def process_manual_crop(image_data: str, crop_box: Dict[str, int], student_id: int, class_id: int) -> Dict[str, Any]:
    """Process a manually selected face region from a frame.
    
    The teacher draws a box around a face that RetinaFace missed.
    This function:
    1. Crops the selected region with intelligent margin expansion
    2. Applies CLAHE for lighting normalization
    3. Runs InsightFace to extract embedding (the manual box gives it enough context)
    4. Creates/updates the student's Identity and embedding
    
    Args:
        image_data: base64 encoded full frame
        crop_box: {'x': int, 'y': int, 'w': int, 'h': int} from the UI selection
        student_id: which student this face belongs to
        class_id: which class
    
    Returns: {'success': bool, 'message': str, ...}
    """
    from .models import Identity, FaceSample, Student, Class
    import base64
    import shutil
    
    try:
        student = Student.objects.get(pk=student_id)
        class_instance = Class.objects.get(pk=class_id)
    except (Student.DoesNotExist, Class.DoesNotExist):
        return {'success': False, 'message': 'Student or class not found'}
    
    # Decode full frame
    try:
        if image_data.startswith('data:image'):
            image_data = image_data.split(',')[1]
        img_bytes = base64.b64decode(image_data)
        pil_img = Image.open(BytesIO(img_bytes)).convert('RGB')
    except Exception as e:
        return {'success': False, 'message': f'Invalid image: {e}'}
    
    cv2_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    img_h, img_w = cv2_bgr.shape[:2]
    
    # Parse crop box
    x = int(crop_box.get('x', 0))
    y = int(crop_box.get('y', 0))
    w = int(crop_box.get('w', 0))
    h = int(crop_box.get('h', 0))
    
    if w < 10 or h < 10:
        return {'success': False, 'message': 'Selection too small. Please draw a larger box around the face.'}
    
    # Expand the selection with 30% margin for better context
    margin_x = int(w * 0.3)
    margin_y = int(h * 0.3)
    
    crop_x1 = max(0, x - margin_x)
    crop_y1 = max(0, y - margin_y)
    crop_x2 = min(img_w, x + w + margin_x)
    crop_y2 = min(img_h, y + h + margin_y)
    
    face_crop_bgr = cv2_bgr[crop_y1:crop_y2, crop_x1:crop_x2]
    if face_crop_bgr.size == 0:
        return {'success': False, 'message': 'Invalid crop region'}
    
    # Ensure minimum size
    crop_h, crop_w = face_crop_bgr.shape[:2]
    if crop_w < 150 or crop_h < 150:
        scale = max(150.0 / crop_w, 150.0 / crop_h)
        face_crop_bgr = cv2.resize(face_crop_bgr, 
            (int(crop_w * scale), int(crop_h * scale)), 
            interpolation=cv2.INTER_CUBIC)
    
    # Apply CLAHE
    lab = cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_ch)
    enhanced_bgr = cv2.cvtColor(cv2.merge([l_enhanced, a_ch, b_ch]), cv2.COLOR_LAB2BGR)
    
    # Generate embedding (with TTA for best quality since this is enrollment)
    emb = generate_embedding(enhanced_bgr)
    
    if emb is None:
        # Try without CLAHE
        emb = generate_embedding(face_crop_bgr)
    
    if emb is None:
        return {
            'success': False, 
            'message': 'Could not extract face features from the selected region. Try selecting a slightly larger area that includes forehead and chin.'
        }
    
    # Create/find identity
    existing_identity = Identity.objects.filter(
        class_instance=class_instance, student=student, is_active=True
    ).first()
    
    if existing_identity:
        identity = existing_identity
    else:
        from django.db.models import Max
        max_result = Identity.objects.filter(class_instance=class_instance).aggregate(
            max_num=Max('auto_label')
        )
        existing_max = 0
        if max_result['max_num']:
            try:
                existing_max = int(max_result['max_num'].split('_')[-1])
            except (ValueError, IndexError):
                existing_max = Identity.objects.filter(class_instance=class_instance).count()
        
        auto_label = f'identity_{existing_max + 1:03d}'
        identity = Identity.objects.create(
            class_instance=class_instance,
            student=student,
            label=student.get_full_name(),
            auto_label=auto_label,
            is_labeled=True,
            is_active=True,
        )
    
    # Save sample
    identity_dir = identity.folder_path
    os.makedirs(identity_dir, exist_ok=True)
    
    timestamp = int(time.time() * 1000)
    img_hash = hashlib.md5(face_crop_bgr.tobytes()[:1000]).hexdigest()[:6]
    
    # Save the crop image
    crop_pil = Image.fromarray(cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2RGB))
    img_path = os.path.join(identity_dir, f'sample_manual_{timestamp}_{img_hash}.jpg')
    crop_pil.save(img_path, 'JPEG', quality=95)
    
    # Save embedding
    emb_path = os.path.join(identity_dir, f'emb_manual_{timestamp}_{img_hash}.npy')
    np.save(emb_path, emb)
    
    FaceSample.objects.create(
        identity=identity,
        image_path=img_path,
        embedding_path=emb_path,
        quality_score=60.0,  # manual selections are typically decent quality
        source_frame='manual_stream_capture',
        is_valid=True,
    )
    
    # Update identity
    identity.sample_count = identity.samples.filter(is_valid=True).count()
    if not identity.representative_image:
        identity.representative_image = img_path
    identity.needs_retraining = True
    identity.save()
    
    # Retrain and reload
    retrain_result = retrain_identity(identity.pk)
    
    if retrain_result.get('success'):
        from . import views as v
        v.load_embeddings_from_db()
        
        return {
            'success': True,
            'message': f'Captured face for {student.get_full_name()} ({retrain_result["samples_used"]} total samples). Embedding updated.',
            'identity_id': identity.pk,
            'samples_used': retrain_result['samples_used'],
        }
    
    return {
        'success': True,
        'message': f'Face saved for {student.get_full_name()} but needs retraining.',
        'identity_id': identity.pk,
    }
