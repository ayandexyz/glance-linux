# The onnxruntime landmarker's two networks

`glancectl fetch-model` downloads these from the `models-v1` release of this
repository (or `$GLANCE_ONNX_MODELS_URL`) and installs them only if they match
the SHA-256 pinned in `src/glanced/models.py`:

| file | sha256 | size |
|---|---|---|
| `face_detector.onnx` | `e3e5ec1d977e912482b4770aac7a5b4562c01630aaec72fcfab9171c8a5b0786` | 418,459 |
| `face_landmarks.onnx` | `0bc321f8fe70ad861e99fe878a7990dd52c89db902ea7d5bfb613898408244af` | 4,921,000 |

**Release step for the maintainer:** create a release tagged `models-v1` and
attach exactly these two files (they are in the pull request description's
attachment, and reproducible as below). Until that release exists, the onnx
backend installs with `glancectl fetch-model --from <dir with the two files>`.

## Where they come from

Both are the networks inside Google's MediaPipe `face_landmarker.task`
(float16/1, sha256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`,
the file the MediaPipe backend already downloads), converted unchanged from
TFLite to ONNX:

| inside the .task | sha256 | converted to |
|---|---|---|
| `face_detector.tflite` (BlazeFace short-range) | `b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f` | `face_detector.onnx` |
| `face_landmarks_detector.tflite` (Face Mesh V2, 478 points with iris) | `c7d54204ce0448474c7f3fa9af494787c0965cbdd6f20fc72867e43046bd43d5` | `face_landmarks.onnx` |
| `geometry_pipeline_metadata_landmarks.binarypb` | `bdbcda96dfcb7da883da124aaa2c55dee49770d934f0fcc71747f8c21bdc75b4` | shipped as is in `glanced/_data/face_geometry/` |

```bash
unzip face_landmarker.task -d task          # the .task file is a zip
pip install tf2onnx==1.17.0 tensorflow==2.20.0   # on x86_64
python -m tf2onnx.convert --tflite task/face_detector.tflite --opset 17 --output face_detector.onnx
python -m tf2onnx.convert --tflite task/face_landmarks_detector.tflite --opset 17 --output face_landmarks.onnx
```

tf2onnx output is **not byte-reproducible** (graph node names differ between
runs; weights and outputs do not), which is why one conversion is published
and pinned rather than converted on each machine. A re-conversion should give
identical outputs; `tests/test_landmarker_onnx.py` checks any pair of files
against MediaPipe's own landmarks (`GLANCE_TEST_MODEL_DIR=<dir> pytest
tests/test_landmarker_onnx.py`). The Face Mesh graph is fp16 in TFLite and fp32
after conversion, hence 4.9 MB instead of 2.5 MB.

License: Apache-2.0 (Google MediaPipe), see NOTICE.
