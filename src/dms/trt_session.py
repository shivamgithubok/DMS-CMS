"""Run the DMS TensorRT engine with the same call shape as ONNX Runtime."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import numpy as np

_CUDA_HOST_TO_DEVICE = 1
_CUDA_DEVICE_TO_HOST = 2


def _import_tensorrt():
    try:
        import tensorrt as trt
    except ModuleNotFoundError:
        site = "/usr/lib/python3.12/dist-packages"
        if site not in sys.path:
            sys.path.append(site)
        import tensorrt as trt
    return trt


def _load_cudart():
    for name in ("libcudart.so.13", "libcudart.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("Could not load libcudart")


class EngineSession:
    def __init__(self, path: Path) -> None:
        self.trt = _import_tensorrt()
        self.cudart = _load_cudart()
        self.cudart.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        self.cudart.cudaMalloc.restype = ctypes.c_int
        self.cudart.cudaFree.argtypes = [ctypes.c_void_p]
        self.cudart.cudaFree.restype = ctypes.c_int
        self.cudart.cudaMemcpy.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        self.cudart.cudaMemcpy.restype = ctypes.c_int
        self.cudart.cudaStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.cudart.cudaStreamCreate.restype = ctypes.c_int
        self.cudart.cudaStreamSynchronize.argtypes = [ctypes.c_void_p]
        self.cudart.cudaStreamSynchronize.restype = ctypes.c_int
        self.cudart.cudaGetErrorString.argtypes = [ctypes.c_int]
        self.cudart.cudaGetErrorString.restype = ctypes.c_char_p

        logger = self.trt.Logger(self.trt.Logger.WARNING)
        runtime = self.trt.Runtime(logger)
        blob = Path(path).read_bytes()
        self.engine = runtime.deserialize_cuda_engine(blob)
        if self.engine is None:
            raise RuntimeError(f"Could not load TensorRT engine {path}")
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise RuntimeError("Could not create TensorRT execution context")

        self.input_name = None
        self.output_names: list[str] = []
        self.device: dict[str, int] = {}
        self.host: dict[str, np.ndarray] = {}
        for index in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(index)
            if self.engine.get_tensor_mode(name) == self.trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                self.output_names.append(name)

        if self.input_name != "image":
            raise RuntimeError(f"Unexpected engine input: {self.input_name}")

        self.context.set_input_shape("image", (1, 3, 224, 224))
        self._alloc("image", (1, 3, 224, 224), np.float32)
        for name in self.output_names:
            shape = tuple(self.context.get_tensor_shape(name))
            dtype = self._numpy_dtype(self.engine.get_tensor_dtype(name))
            self._alloc(name, shape, dtype)

        stream = ctypes.c_void_p()
        self._check(self.cudart.cudaStreamCreate(ctypes.byref(stream)), "cudaStreamCreate")
        self.stream = stream

    def _numpy_dtype(self, trt_dtype):
        if trt_dtype == self.trt.DataType.FLOAT:
            return np.float32
        if trt_dtype == self.trt.DataType.HALF:
            return np.float16
        raise RuntimeError(f"Unsupported TensorRT dtype {trt_dtype}")

    def _alloc(self, name: str, shape: tuple[int, ...], dtype) -> None:
        host = np.empty(shape, dtype=dtype)
        nbytes = host.nbytes
        ptr = ctypes.c_void_p()
        self._check(self.cudart.cudaMalloc(ctypes.byref(ptr), nbytes), "cudaMalloc")
        self.device[name] = ptr.value
        self.host[name] = host
        self.context.set_tensor_address(name, ptr.value)

    def _check(self, code: int, what: str) -> None:
        if code == 0:
            return
        message = self.cudart.cudaGetErrorString(code)
        text = message.decode() if message else str(code)
        raise RuntimeError(f"{what} failed: {text}")

    def run(self, _output_names, feeds: dict) -> list[np.ndarray]:
        image = np.ascontiguousarray(feeds["image"], dtype=np.float32)
        if image.shape != (1, 3, 224, 224):
            raise RuntimeError(f"Expected image shape (1, 3, 224, 224), got {image.shape}")
        self._check(
            self.cudart.cudaMemcpy(
                ctypes.c_void_p(self.device["image"]),
                ctypes.c_void_p(image.ctypes.data),
                image.nbytes,
                _CUDA_HOST_TO_DEVICE,
            ),
            "cudaMemcpy H2D",
        )
        if not self.context.execute_async_v3(self.stream.value):
            raise RuntimeError("TensorRT execute_async_v3 failed")
        self._check(self.cudart.cudaStreamSynchronize(self.stream), "cudaStreamSynchronize")
        outputs = []
        for name in self.output_names:
            host = self.host[name]
            self._check(
                self.cudart.cudaMemcpy(
                    ctypes.c_void_p(host.ctypes.data),
                    ctypes.c_void_p(self.device[name]),
                    host.nbytes,
                    _CUDA_DEVICE_TO_HOST,
                ),
                "cudaMemcpy D2H",
            )
            array = np.array(host, dtype=np.float32, copy=True)
            outputs.append(array)
        return outputs
