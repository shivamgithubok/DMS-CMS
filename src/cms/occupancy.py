"""Cabin occupancy from the cabin camera.

A face whose center is in the upper half of the frame is counted as a rear
seat. A face in the lower half is counted as a front seat. That matches a
camera looking back from the front of the cabin: people farther away sit
higher in the picture. There are no seat sensors.
"""

from __future__ import annotations

import cv2
import mediapipe as mp
import numpy as np

REAR_MAX_CENTER = 0.50


def count(frame_bgr: np.ndarray, detector) -> dict:
    height = frame_bgr.shape[0]
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = detector.detect(image)
    front = 0
    rear = 0
    if result.detections:
        for detection in result.detections:
            box = detection.bounding_box
            center_y = (box.origin_y + box.height / 2) / height
            if center_y < REAR_MAX_CENTER:
                rear += 1
            else:
                front += 1
    return {"front": front, "rear": rear}
