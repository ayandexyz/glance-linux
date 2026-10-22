# Test faces

Used by `tests/test_landmarker_onnx.py` and `tests/test_landmarker_backend.py`.
`../mediapipe_reference.npz` is MediaPipe 0.10.35's FaceLandmarker output
(VIDEO mode, 4 frames at 50 ms, with facial transformation matrices) on
exactly these files, plain and mirrored.

| file | source | license |
|---|---|---|
| `astronaut.jpg` | scikit-image sample data `astronaut.png` (NASA photo of Eileen Collins), re-encoded as JPEG | public domain |
| `einstein.jpg` | Wikimedia Commons, "Albert Einstein Head" | public domain |
| `mona_lisa.jpg` | Wikimedia Commons, Mona Lisa by Leonardo da Vinci | public domain |
| `portrait.jpg` | MediaPipe test assets (`mediapipe/tasks/testdata/vision/portrait.jpg`) | MediaPipe repository, Apache-2.0 |
| `noface_cats_and_dogs.jpg` | MediaPipe test assets (`cats_and_dogs.jpg`) | MediaPipe repository, Apache-2.0 |

Please double-check these before merging; replace any you would rather not
carry, and regenerate the reference with MediaPipe on the replacements.
