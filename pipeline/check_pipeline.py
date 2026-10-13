"""Run the DMS TensorRT engine on a video and check the cabin pipeline gate.

The vision path matches Driver-Monitoring-System-v1 vision.py:
face crop, ImageNet normalize, 224 input, eight softmax heads.
The VLM is not loaded. This script only counts when a frame would call it.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, deque
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
import onnxruntime as ort
from mediapipe.tasks.python.core.base_options import BaseOptions
from mediapipe.tasks.python.vision import FaceDetector, FaceDetectorOptions
from mediapipe.tasks.python.vision.core.vision_task_running_mode import (
    VisionTaskRunningMode,
)

ROOT = Path(__file__).resolve().parents[1]
ONNX_PATH = ROOT / "models" / "edge_driver_model_distilled.onnx"
ENGINE_PATH = ROOT / "models" / "edge_driver_model_distilled_fp16.engine"
MODEL_PATH = ENGINE_PATH
FACE_MODEL_PATH = ROOT / "models" / "blaze_face_short_range.tflite"
RESULTS = ROOT / "results"

IMG_SIZE = 224
FACE_MARGIN = 0.20
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Names and class maps are the ONNX contract from the DMS repo config.py.
TASKS = {
    "drowsiness": {0: "Alert", 1: "Drowsy"},
    "gaze": {
        0: "Bottom-Left",
        1: "Middle-Left",
        2: "Top-Left",
        3: "Bottom-Right",
        4: "Middle-Right",
        5: "Top-Right",
        6: "Top-Center",
        7: "Bottom-Center",
    },
    "yawn": {0: "No Yawn", 1: "Yawning"},
    "emotion": {
        0: "Angry",
        1: "Disgust",
        2: "Fear",
        3: "Happy",
        4: "Sad",
        5: "Surprise",
        6: "Neutral",
    },
    "eye_state": {0: "Open", 1: "Closed"},
    "distraction": {0: "Safe", 1: "Distracted"},
    "activity": {
        0: "Phone Use",
        1: "Drinking",
        2: "Cigarette",
        3: "Seatbelt",
        4: "None",
    },
    "age": {0: "Minor", 1: "Young Adult", 2: "Adult"},
}
OUTPUT_ORDER = [
    "drowsiness",
    "gaze",
    "yawn",
    "emotion",
    "eye_state",
    "distraction",
    "activity",
    "age",
]
# Same alert thresholds as vision.py / config.py. Age and emotion do not trigger.
TRIGGER_RULES = {
    "drowsiness": ("Drowsy", 0.65),
    "eye_state": ("Closed", 0.80),
    "yawn": ("Yawning", 0.70),
    "distraction": ("Distracted", 0.60),
}
ACTIVITY_TRIGGERS = {"Phone Use": 0.50, "Drinking": 0.50, "Cigarette": 0.50}
GAZE_AWAY = {
    "Bottom-Left",
    "Top-Left",
    "Bottom-Right",
    "Top-Right",
    "Top-Center",
    "Bottom-Center",
}
SMOOTH_WINDOW = 5
TRIGGER_HOLD = 3
VLM_COOLDOWN = 10


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max()
    exp = np.exp(shifted)
    return exp / exp.sum()


def load_session(model_path: Path):
    if Path(model_path).suffix == ".engine":
        from trt_session import EngineSession

        session = EngineSession(model_path)
        if session.output_names != OUTPUT_ORDER:
            raise RuntimeError(f"Unexpected engine outputs: {session.output_names}")
        probe = np.zeros((1, 3, IMG_SIZE, IMG_SIZE), dtype=np.float32)
        probed = session.run(None, {"image": probe})
        if len(probed) != len(OUTPUT_ORDER):
            raise RuntimeError("Probe run returned the wrong number of heads")
        contract = {
            "path": str(model_path),
            "bytes": Path(model_path).stat().st_size,
            "input": {"name": "image", "shape": [1, 3, IMG_SIZE, IMG_SIZE]},
            "outputs": [
                {"name": name, "classes": list(TASKS[name].values())}
                for name in OUTPUT_ORDER
            ],
            "provider": "TensorRT-FP16",
        }
        return session, contract

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if len(inputs) != 1 or inputs[0].name != "image":
        raise RuntimeError(f"Unexpected ONNX inputs: {[i.name for i in inputs]}")
    names = [output.name for output in outputs]
    if names != OUTPUT_ORDER:
        raise RuntimeError(f"Unexpected ONNX outputs: {names}")
    for output in outputs:
        classes = TASKS[output.name]
        width = output.shape[-1] if isinstance(output.shape[-1], int) else None
        if width is not None and width != len(classes):
            raise RuntimeError(
                f"{output.name} has {width} logits, expected {len(classes)}"
            )
    probe = np.zeros((1, 3, IMG_SIZE, IMG_SIZE), dtype=np.float32)
    probed = session.run(None, {"image": probe})
    if len(probed) != len(OUTPUT_ORDER):
        raise RuntimeError("Probe run returned the wrong number of heads")
    contract = {
        "path": str(model_path),
        "bytes": model_path.stat().st_size,
        "input": {"name": "image", "shape": [1, 3, IMG_SIZE, IMG_SIZE]},
        "outputs": [
            {"name": name, "classes": list(TASKS[name].values())}
            for name in OUTPUT_ORDER
        ],
        "provider": session.get_providers()[0],
    }
    return session, contract


def load_face_detector() -> FaceDetector:
    options = FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(FACE_MODEL_PATH)),
        running_mode=VisionTaskRunningMode.IMAGE,
        min_detection_confidence=0.5,
    )
    return FaceDetector.create_from_options(options)


def crop_face(frame_bgr: np.ndarray, detector: FaceDetector):
    height, width = frame_bgr.shape[:2]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = detector.detect(image)
    if not result.detections:
        return cv2.resize(frame_bgr, (IMG_SIZE, IMG_SIZE)), None

    def area(detection) -> int:
        box = detection.bounding_box
        return box.width * box.height

    best = max(result.detections, key=area)
    box = best.bounding_box
    margin_x = FACE_MARGIN * box.width
    margin_y = FACE_MARGIN * box.height
    x1 = max(0, int(box.origin_x - margin_x))
    y1 = max(0, int(box.origin_y - margin_y))
    x2 = min(width, int(box.origin_x + box.width + margin_x))
    y2 = min(height, int(box.origin_y + box.height + margin_y))
    face = frame_bgr[y1:y2, x1:x2]
    if face.size == 0:
        return cv2.resize(frame_bgr, (IMG_SIZE, IMG_SIZE)), None
    score = float(best.categories[0].score) if best.categories else 0.0
    return cv2.resize(face, (IMG_SIZE, IMG_SIZE)), {
        "bbox": [x1, y1, x2, y2],
        "score": round(score, 3),
        "faces": len(result.detections),
    }


def preprocess(face_bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2RGB)
    normalized = (rgb.astype(np.float32) / 255.0 - MEAN) / STD
    return normalized.transpose(2, 0, 1)[np.newaxis]


def decode(outputs) -> dict:
    decoded = {}
    for name, logits in zip(OUTPUT_ORDER, outputs):
        probs = softmax(np.asarray(logits)[0].astype(np.float32))
        pred = int(np.argmax(probs))
        classes = TASKS[name]
        decoded[name] = {
            "label": classes[pred],
            "confidence": float(probs[pred]),
            "probs": probs,
        }
    return decoded


def smooth(history: dict[str, deque], raw: dict) -> dict:
    smoothed = {}
    for name, result in raw.items():
        history[name].append(result["probs"])
        average = np.mean(history[name], axis=0)
        pred = int(np.argmax(average))
        smoothed[name] = {
            "label": TASKS[name][pred],
            "confidence": round(float(average[pred]), 4),
        }
    return smoothed


def trigger_reasons(preds: dict, face_found: bool) -> list[str]:
    if not face_found:
        return []
    reasons = []
    for task, (label, threshold) in TRIGGER_RULES.items():
        item = preds[task]
        if item["label"] == label and item["confidence"] >= threshold:
            reasons.append(f"{task}:{label}@{item['confidence']:.2f}")
    activity = preds["activity"]
    activity_threshold = ACTIVITY_TRIGGERS.get(activity["label"])
    if activity_threshold and activity["confidence"] >= activity_threshold:
        reasons.append(f"activity:{activity['label']}@{activity['confidence']:.2f}")
    gaze = preds["gaze"]
    if gaze["label"] in GAZE_AWAY and gaze["confidence"] >= 0.55:
        reasons.append(f"gaze:{gaze['label']}@{gaze['confidence']:.2f}")
    return reasons


def draw_overlay(frame, face, preds, reasons, vlm_called: bool) -> np.ndarray:
    canvas = frame.copy()
    if face and face.get("bbox"):
        x1, y1, x2, y2 = face["bbox"]
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 229, 160), 2)
    lines = [
        f"drow {preds['drowsiness']['label']} {preds['drowsiness']['confidence']:.2f}",
        f"eye {preds['eye_state']['label']} {preds['eye_state']['confidence']:.2f}",
        f"gaze {preds['gaze']['label']} {preds['gaze']['confidence']:.2f}",
        f"act {preds['activity']['label']} {preds['activity']['confidence']:.2f}",
        "VLM call" if vlm_called else ("trigger" if reasons else "dms only"),
    ]
    y = 22
    for line in lines:
        cv2.putText(
            canvas, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1
        )
        y += 18
    return canvas


def run_video(session, detector, video_path: Path, sample_every: int) -> dict:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open {video_path}")

    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    history = {name: deque(maxlen=SMOOTH_WINDOW) for name in OUTPUT_ORDER}
    labels = {name: Counter() for name in OUTPUT_ORDER}
    latencies = []
    face_hits = 0
    sampled = 0
    hold = 0
    cooldown = 0
    vlm_planned = 0
    trigger_frames = 0
    reason_counts = Counter()
    snapshots = []
    estimated = max(1, frame_count // sample_every)
    save_at = {1, max(1, estimated // 2), estimated}

    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % sample_every != 0:
            index += 1
            continue

        face_img, face = crop_face(frame, detector)
        tensor = preprocess(face_img)
        started = time.perf_counter()
        outputs = session.run(None, {"image": tensor})
        latencies.append((time.perf_counter() - started) * 1000.0)
        preds = smooth(history, decode(outputs))
        reasons = trigger_reasons(preds, face is not None)
        if face is not None:
            face_hits += 1
        for name, item in preds.items():
            labels[name][item["label"]] += 1

        vlm_called = False
        if reasons:
            trigger_frames += 1
            hold += 1
            for reason in reasons:
                reason_counts[reason.split("@", 1)[0]] += 1
        else:
            hold = 0
        if cooldown > 0:
            cooldown -= 1
        if hold >= TRIGGER_HOLD and cooldown == 0:
            vlm_planned += 1
            vlm_called = True
            cooldown = VLM_COOLDOWN
            hold = 0

        sampled += 1
        if sampled in save_at or index + sample_every >= frame_count and sampled not in save_at:
            # Keep the last sample as well when the frame count does not divide evenly.
            if sampled in save_at or sampled == estimated or index + sample_every >= frame_count:
                if not any(item["frame"] == index for item in snapshots):
                    shot = draw_overlay(frame, face, preds, reasons, vlm_called)
                    shot_name = f"{video_path.stem}_f{index:04d}.jpg"
                    cv2.imwrite(str(RESULTS / shot_name), shot)
                    snapshots.append(
                        {
                            "frame": index,
                            "image": shot_name,
                            "face": face,
                            "predictions": preds,
                            "trigger_reasons": reasons,
                            "vlm_would_run": vlm_called,
                        }
                    )
        index += 1

    capture.release()
    lat = np.asarray(latencies, dtype=np.float32)
    return {
        "video": video_path.name,
        "source": "Pexels, free to use",
        "frames": frame_count,
        "fps": round(fps, 2),
        "sampled_every": sample_every,
        "sampled_frames": sampled,
        "face_detected_frames": face_hits,
        "face_miss_frames": sampled - face_hits,
        "dms_latency_ms": {
            "mean": round(float(lat.mean()), 2) if len(lat) else None,
            "p50": round(float(np.percentile(lat, 50)), 2) if len(lat) else None,
            "p95": round(float(np.percentile(lat, 95)), 2) if len(lat) else None,
        },
        "label_counts": {name: dict(counter) for name, counter in labels.items()},
        "trigger_frames": trigger_frames,
        "trigger_reason_counts": dict(reason_counts),
        "vlm_calls_planned": vlm_planned,
        "vlm_executed": 0,
        "snapshots": snapshots,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "videos",
        nargs="*",
        type=Path,
        default=[
            ROOT / "videos" / "driver_wheel.mp4",
            ROOT / "videos" / "driver_close.mp4",
        ],
    )
    parser.add_argument("--sample-every", type=int, default=5)
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    session, contract = load_session(MODEL_PATH)
    detector = load_face_detector()
    videos = []
    for video in args.videos:
        print(f"[RUN] {video.name}")
        videos.append(run_video(session, detector, video, args.sample_every))
        last = videos[-1]
        print(
            f"  sampled={last['sampled_frames']} face={last['face_detected_frames']} "
            f"triggers={last['trigger_frames']} vlm_planned={last['vlm_calls_planned']} "
            f"dms_ms={last['dms_latency_ms']}"
        )

    report = {
        "model_contract": contract,
        "vlm": {
            "loaded": False,
            "model": "Qwen3-VL-2B-Instruct",
            "note": "Gate counted only. No VLM engine is on this machine yet.",
        },
        "excluded_from_trigger": ["emotion", "age"],
        "videos": videos,
    }
    out = RESULTS / "pipeline_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
