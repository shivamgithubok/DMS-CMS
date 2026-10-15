# CMS

Cabin monitoring on the vehicle. A driver model reads the face camera, the cabin camera counts seats, and an LVGL screen shows the live state. A high risk score or the SOS button publishes a guardian page over MQTT. An optional local vision-language model can confirm a cabin still.

This repository is licensed under the MIT License. See [LICENSE](LICENSE). Code under `third_party/` keeps its own licenses.

## Setup

Python 3.12, a working `DISPLAY`, and the NVIDIA TensorRT and CUDA runtime that match `models/edge_driver_model_distilled_fp16.engine`. The cabin screen also needs `gcc` and `libx11-dev`.

```bash
chmod +x setup.sh
./setup.sh
source .venv/bin/activate
```

`setup.sh` creates `.venv`, installs [requirements.txt](requirements.txt), installs this project from `src/`, and builds `src/ui/cms_ui` when the compiler and LVGL tree are present.

Rebuild the screen on its own:

```bash
make -C src/ui
```

## Run

Start the cabin screen and the pipeline together. Demo videos loop. The screen listens on `127.0.0.1:9750`.

```bash
python src/pipeline/run_lvgl_demo.py
```

Use the bundled clips explicitly:

```bash
python src/pipeline/run_lvgl_demo.py videos/cabin_passenger.mp4 --driver videos/driver_face.mp4
```

Live camera (V4L2 index):

```bash
python src/pipeline/run_lvgl_demo.py --camera 0
```

Ask the local Qwen3.5-0.8B engine when the cabin gate opens. That process uses `third_party/edgellm-venv`, not the dashboard virtualenv.

```bash
python src/pipeline/run_lvgl_demo.py --vlm
```

Check the driver model and the cabin gate without loading the vision-language model. The report is written to `results/pipeline_report.json`.

```bash
python src/pipeline/check_pipeline.py videos/driver_face.mp4 videos/driver_face_1.mp4
```

One cabin still through the Edge-LLM interpreter:

```bash
third_party/edgellm-venv/bin/python src/pipeline/cabin_vlm.py path/to/still.jpg
```

Guardian phone page. Leave this running, then start the cabin demo. When a ride sends SOS, the link prints as `http://127.0.0.1:8080/t/<token>`. MQTT is `127.0.0.1:1883`.

```bash
python src/pipeline/guardian_page.py
```

## Tests

These checks do not open a camera or load model weights. Run them from the project root.

```bash
python -m unittest tests.test_risk tests.test_journey tests.test_guardian
```

## Layout

Code lives under `src/`. Models, videos, and generated output stay at the project root. `src/pipeline/` is the only place you start the system, and `run_lvgl_demo.py` calls the other packages on each frame.

| Path | Role |
| --- | --- |
| `src/pipeline/run_lvgl_demo.py` | Main loop: cameras, risk, screen, optional VLM, guardian publish |
| `src/pipeline/check_pipeline.py` | Starts the driver-model pass |
| `src/pipeline/guardian_page.py` | Starts the phone page |
| `src/pipeline/cabin_vlm.py` | Starts one Qwen still, using the Edge-LLM interpreter |
| `src/pipeline/risk.py` | Score that joins the driver hold and the cabin event |
| `src/dms/` | Driver face model, TensorRT session, trigger rules |
| `src/cms/` | Cabin gate, seat count, cabin record, Qwen client |
| `src/guardian/` | Ride, phone page, journey database |
| `src/ui/` | LVGL cabin screen (`make` produces `src/ui/cms_ui`) |
| `tests/` | Risk, journey, and guardian checks |
| `models/` | Driver ONNX, TensorRT engine, and BlazeFace weights |
| `videos/` | Demo clips |
| `fleet/roster.txt` | Sample driver and passenger records |
| `results/` | Logs, journey database, incident clips |
