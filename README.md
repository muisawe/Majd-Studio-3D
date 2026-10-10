# Majd Studio 3D

حزمة Windows لنسخة V9 Phase 2.1 Beta. يوضح [roadmap.md](roadmap.md) الرؤية والمراحل القادمة.

## ترتيب المشروع

```text
majd_studio_3d/
  app.py          واجهة Gradio وتدفق التوليد والمراجعة
  store.py        قاعدة SQLite والمشاريع والأصول والإصدارات
  input_qa.py     فحص الصور ومعايرة الزوايا وتقييم الشكل
  updater.py      تنزيل وتطبيق تحديثات GitHub Releases
  model_manager.py تنزيل أوزان النماذج والتحقق منها واستكمالها
  parts.py        تجهيز بيئة الأجزاء وتشغيل المهام
  parts_worker.py تكامل P3-SAM وXPart داخل عملية منفصلة
  generation.py   محرك توليد Hunyuan وتنفيذ الدفعات (بدون Gradio)
  candidate_qa.py مقارنة Silhouette للمرشحين مع المراجع
  viewer_publisher.py نشر حالة العارض وخادمه المحلي
  blender_finalize.py اكتشاف Blender وإنهاء الأصل المعتمد
  asset_intake.py إنشاء الأصول والاستيراد من مجلد (بدون Gradio)
  constants.py    ثوابت مشتركة (VIEW_KEYS وSTATUS_AR)
viewer/           واجهة Three.js المستقلة
scripts/          بناء الحزم وتثبيت Windows وتشغيله
tests/            اختبارات التخزين والتحديث
previews/         معاينات واجهة غير متصلة بالنموذج
version.json      رقم إصدار التطبيق
update_config.json مستودع التحديثات والقناة
install.cmd       نقطة تثبيت Windows
```

الملفات `majd_studio_3d_v9.py` و`install_v9.ps1` و`launch_majd_studio_3d_v9.ps1` واجهات توافق صغيرة للمسارات القديمة. أصول المستخدم وقاعدة البيانات تبقى في `majd_v9/` على جهاز Windows، خارج حزمة كود التطبيق.

## ما تدعمه النسخة الحالية

- مراجع معتمدة لكل Style Profile، مع نوع المرجع والزاوية والوزن.
- Preflight للصور: الدقة، فصل العنصر عن الخلفية، القص، الحجم، التمركز، واتساق الارتفاع بين الزوايا.
- معايرة Front/Back/Left/Right إلى مساحة موحدة مع إبقاء الصور الأصلية.
- منع التوليد عند فشل Preflight وفق قواعد Style Profile.
- مقارنة مرشحي Hunyuan ومراجعة Silhouette وPoly Budget وStyle Lock بصريًا.
- عرض مرجع الصورة فوق النموذج في واجهة Three.js، ثم اعتماد المرشح بشريًا كإصدار محفوظ.

تقييم Geometry Style الحالي تجريبي ويقارن خصائص عامة مثل النسب والامتلاء والتناظر؛ لا يكتشف ملامح الجسم والوجه الدلالية. قرار الاعتماد الفني يبقى للمراجع البشري.

## التثبيت الأول على Windows

1. فك ضغط `majd-studio-3d-bootstrap.zip`.
2. أغلق الاستديو إن كان مفتوحًا، ثم شغّل `install.cmd`.
3. يتوقع المثبّت وجود Hunyuan3D-2.1 في `E:\AI\Hunyuan3D-2.1` (أو المسار في متغير البيئة `MAJD_STUDIO_ROOT`)، ويُبقي قاعدة مشاريع V9 والاختصار المكتبي.

إذا كانت نسخة Beta 1 مثبتة مسبقًا، يلزم تشغيل Bootstrap الجديد مرة واحدة لأن بنية الحزمة تغيّرت؛ المحدّث القديم لا يفهم مسارات الملفات الجديدة.

الواجهة الرئيسية: `http://127.0.0.1:7864` · العارض: `http://127.0.0.1:7865/viewer.html`.

## التحديثات العامة عبر GitHub

المستودع المضبوط حاليًا هو `muisawe/Majd3D` في [update_config.json](update_config.json). غيّر المالك إذا كان حساب GitHub مختلفًا. بعد تثبيت Bootstrap مرة واحدة، يفحص المشغّل الإصدارات العامة عند فتح الاستديو، ويتحقق من SHA-256 للحزمة، ثم يثبت ملفات التطبيق الجديدة. عند فشل الإقلاع يعود إلى ملفات التطبيق السابقة مع إبقاء قاعدة البيانات الحالية؛ ويحتفظ بنسخة SQLite السابقة للاستعادة اليدوية داخل `majd_v9/updates/backup-*/`.

إذا تعذر الإنترنت، يفتح الإصدار المثبت. يُسجل المشغّل الحالة في `majd_v9/logs/launcher.log`. الإصدار الذي فشل إقلاعه لا يُعاد تنزيله حتى يُنشر إصدار أحدث.

التحديث التلقائي يشمل كود التطبيق والعارض فقط. تغيير اعتماديات Python أو سكربت تشغيل Windows يتطلب Bootstrap جديدًا. يجب أن تبقى ترحيلات قاعدة البيانات متوافقة مع الإصدار السابق؛ التغييرات التي تحذف أو تعيد تسمية البيانات تحتاج خطة ترحيل مستقلة.

ينشئ سكربت البناء قائمة ملفات وبصمات داخل حزمة التحديث، لذلك يمكن إضافة وحدات Python جديدة تحت `majd_studio_3d/` أو ملفات واجهة جديدة تحت `viewer/` دون تعديل قائمة ثابتة في المحدّث. مجلدات `viewer/data/` و`viewer/vendor/` تبقى خارج هذه التحديثات.

الحزمة لا تحذف تلقائيًا ملفًا أزيل أو أُعيدت تسميته من المصدر؛ عالج إزالة الملفات القديمة ضمن خطة ترحيل للإصدار المعني.

## نشر إصدار جديد

الكود في `muisawe/Majd-Studio-3D`، والإصدارات تُنشر إلى `muisawe/Majd3D` الذي تقرأ منه الأجهزة. النشر آلي عبر [release.yml](.github/workflows/release.yml):

1. ارفع `version.json` إلى رقم أعلى بصيغة `MAJOR.MINOR.PATCH`، ويمكن إضافة `-beta.N` أو `-rc.N`، ثم commit وpush إلى `main`.
2. اختبر الحزمة من CI أولًا: نزّل `release-packages` من تبويب Actions وثبّت `majd-studio-3d-bootstrap.zip` على جهاز واحد.
3. انشر بوسم مطابق: `git tag v<الإصدار>` ثم `git push origin v<الإصدار>`.

يشغّل الـWorkflow الاختبارات، ويرفض رقمًا موجودًا أو أقدم من آخر إصدار، ويرفع الحزمتين كـdraft، ويطابق SHA-256 الذي يحسبه GitHub مع البناء قبل النشر. يحتاج secret باسم `MAJD3D_RELEASE_TOKEN`: Fine-grained token على `muisawe/Majd3D` فقط بصلاحية Contents: Read and write. تشغيله يدويًا من Actions يجري تحققًا كاملًا دون نشر.

يتجاهل المحدّث أي Release لا يقدم SHA-256 للحزمة في GitHub API أو يحتوي حزمة ببنية غير متوقعة. الحزمة التي تفشل في التحقق تُعاد محاولتها 3 مرات ثم تُتخطى حتى يصدر رقم أحدث.

## ملف التسليم لمساعد آخر

[ASSET_MANAGEMENT_CHATGPT_HANDOFF.md](ASSET_MANAGEMENT_CHATGPT_HANDOFF.md) شرح كامل للمشروع موجّه لمساعد ذكاء اصطناعي لم يرَ الكود (مثل ChatGPT). لتحديثه بعد أي تغيير اكتب في Claude Code الأمر `/update-handoff` (أو `/update-handoff full` لإعادة الفحص كاملًا)؛ يحدّث الأقسام المتأثرة فقط ثم يرفع الملف.

## فحص أول أصل

أنشئ Style Profile ومرجع Front معتمدًا، ثم أضف صور Front/Back/Left/Right. شغّل «فحص الصور ومعاينة Calibration»، وتحقق من التقرير والصورة المعايرة. أضف أصلًا واحدًا للدفعة، ولّد المرشحين، ثم راجعهم بالمرجع قبل اعتماد نسخة.

للشخصيات: افتح «نقاط الجسم للشخصيات» في تبويب الإنتاج، واختر الأصل والزاوية والنقطة ثم انقر على مكانها في الصورة. عند تحديد أعلى الرأس والقدمين في كل الزوايا تُعاير الصور على طول الجسم بدل حدود الشكل، ويظهر في تقرير Preflight أي اختلاف في مواضع النقاط بين الزوايا.

إذا أُغلق البرنامج أثناء التوليد، يظهر الأصل عند التشغيل التالي بحالة «فشل» مع سبب «انقطع التوليد»، وتشغيل الدفعة يستأنف من آخر Candidate محفوظ. السجل في `majd_v9/logs/v9.log`، ونسخ قاعدة البيانات اليومية في `majd_v9/backups/db/`.

للاستيراد الجماعي استخدم أسماء مثل `chair__front.png` و`chair__back.png` و`chair__left.png` و`chair__right.png` و`chair__3q.png` و`chair__detail.png`.

`previews/mac_ui.html` معاينة واجهة فقط؛ لا تشغّل النموذج أو Blender ولا تحفظ بيانات المستخدم.

## النماذج وتقسيم الأصول

تتيح شاشة «النماذج والتنزيل» تنزيل Hunyuan3D-2.1 و2mv وP3-SAM وXPart من Hugging Face. يعرض شريط التقدم حجم البيانات التي وصلت فعلًا. يُحفظ الملف الناقص بامتداد `.part` ويُستكمل بعد الإيقاف أو انقطاع الاتصال؛ ولا يصبح جاهزًا حتى يكتمل التحقق من الحجم والبصمة. يعيد المدير استخدام أوزان Hunyuan وHugging Face الموجودة بعد فحصها. النموذج الأساسي يُنزّل تلقائيًا عند بدء التوليد إذا كان ناقصًا.

يتضمن التطبيق بيانات تحقق مثبتة من المستودعات الرسمية لفحص الأوزان الموجودة حتى عند تعذر الإنترنت. تظل الملفات الناقصة بحاجة إلى اتصال لتنزيلها.

في شاشة «الأجزاء»، اختر أصلًا معتمدًا أو ارفع GLB/PLY/OBJ، ثم شغّل التقسيم. P3-SAM يحدد القطع ويصدر كل جزء كملف GLB. خيار XPart يعيد بناء الأجزاء ويصدر المجسم المجمع والعرض المنفصل. يمكن تنزيل النتائج ZIP، أو تسمية جزء واعتماده كأصل مستقل مرتبط بالمصدر. يظهر سجل النتائج عند إعادة فتح المشروع.

الصور والنموذج الأصلي لا يُستبدلان. P3-SAM ينظف المجسم قبل التقسيم؛ لذلك `face_ids.npy` يطابق ملف `segmented.glb` الناتج، وليس فهرس وجوه الأصل. اعتماد الجزء قرار بشري، ولا يرث نتيجة Style Lock للمجسم الكامل.

زر «تنزيل وتجهيز أدوات الأجزاء» ينزّل نسخة كود رسمية مثبتة المراجعة، ويجهّز بيئة Python منفصلة تحت `majd_v9/models/parts-runtime/`. تستخدم هذه البيئة PyTorch CUDA الموجود وتحافظ عليه عبر قيود الإصدار. تُستخدم آلية الانتباه العادية التي يدعمها Sonata، دون الحاجة إلى بناء Flash Attention. تشغيل الأجزاء يحتاج GPU وPyTorch CUDA 12 ومكتبات spconv وtorch-scatter وtorch-cluster متوافقة. إذا لم تتوفر Wheel متوافقة على الجهاز، يظهر سبب الفشل وسجل `install.log`؛ تنزيل الأوزان وحده لا يعني جاهزية التشغيل.

المصادر: [الكود الرسمي](https://github.com/Tencent-Hunyuan/Hunyuan3D-Part)، [نسخة العرض الرسمية المثبتة](https://huggingface.co/spaces/tencent/Hunyuan3D-Part/tree/27cacbd069110b5fdeb85e928e6f9433d5487c37)، [الأوزان](https://huggingface.co/tencent/Hunyuan3D-Part).

## Geometry cleanup — Phase E

Cleanup has a geometry-only Blender worker, a post-generation service, persistent configuration,
and run history. A minimal generation handoff enables processing when `auto_cleanup` is true.
The existing review tab now exposes selected-candidate processing controls and history.
The existing Blender finalization step retains scaling, grounding, shading, and thumbnails.

Install Blender **4.x** from [Blender's official downloads](https://www.blender.org/download/).
Set `BLENDER_PATH` or `blender_path` in the runtime configuration. `BLENDER_PATH` takes
precedence; otherwise the resolver checks PATH and standard Mac/Windows locations.
On macOS the standard executable is `/Applications/Blender.app/Contents/MacOS/Blender`.
No additional Python dependencies are needed for the service or database layer.

`majd_v9/cleanup_config.json` is created with these defaults when configuration is first loaded:

```json
{
  "blender_path": null,
  "auto_cleanup": false,
  "cleanup_scope": "all_candidates",
  "top_k": 3,
  "decimate_ratio": 0.3,
  "target_faces": null,
  "merge_distance": 0.00001,
  "min_island_percent": 0.5,
  "max_island_removal_percent": 5,
  "hole_max_edges": 8,
  "hole_max_perimeter": 0.01,
  "output_format": "glb",
  "timeout_seconds": 300,
  "hunyuan_floater_remover": true,
  "hunyuan_degenerate_face_remover": true,
  "hunyuan_face_reducer": true
}
```

Invalid values fall back individually to defaults with warnings. Malformed JSON falls
back to defaults without overwriting the file. Environment overrides are not written
back to disk. `save_cleanup_config` uses the existing atomic JSON writer.

- `target_faces`, when set, overrides the ratio. `shared_face_budget(raw_faces, settings)`
  freezes an absolute target for both `FaceReducer(max_facenum=target)` and Blender.
  For example, 1,000 raw faces at 0.3 gives 300; Blender does not reduce 300 again to 90.
  The processing service calls the engine-specific native FaceReducer in an isolated
  Python subprocess, using that target. Native floaters/degenerate processors are not
  invoked in this phase; Blender performs those repairs with its existing safety cap.
- Island thresholds and the deletion cap are percentages of triangular face counts.
  If the selected islands exceed the cap, all island removal is skipped for that run.
  Stats include removed island faces, proposed removal, and the safety warning.
- `hole_max_perimeter` is a fraction of the bounding-box diagonal; a hole must also
  meet `hole_max_edges`. Zero for either hole limit disables filling.
- `cleanup_scope` accepts `all_candidates` or `top_k_after_scoring`; `top_k` must be a
  positive integer. Generation exports and ranks raw candidates first; the handoff then
  processes all candidates or the selected top k. Existing QA scores explicitly retain
  `score_basis: raw`; generation/model invocation and scoring rules are unchanged.
- Collapse reduction protects UV/material boundaries. It may retain extra faces and
  report a warning when the requested reduction would change those boundaries.

Manual CLI usage (`out` is a directory):

```sh
python -m majd_studio_3d.cleanup input.glb output-directory
python -m majd_studio_3d.cleanup input.obj output-directory --app-dir /path/to/majd_v9
```

Each run creates a unique output subfolder with a canonical embedded-texture GLB,
optional glTF/OBJ/FBX export and sidecars, request, log, and report. Originals are never
overwritten. The service defaults to an adjacent `cleanup_runs/<run-id>` directory.
CLI history goes to `majd_v9.sqlite3` in the selected app directory.

Results explicitly distinguish `success`, `failed` (raw fallback), and `cancelled`.
Only success exposes `cleaned_glb_path`; cancellation kills/reaps Blender and removes
the partial folder. CLI exit codes are 0, 1, and 130 respectively. Batch processing
continues after individual failures and stops on cancellation.

Schema 94 adds `cleanup_runs` without changing existing records. Run history stores
source asset/version references, stats, errors, artifact paths, and immutable effective
config snapshots. Re-recording identical data is idempotent; changing an existing run
requires a new run ID. Recording a cleanup does not approve it or modify an existing
approved version. Processing does not automatically approve a replacement version.

### Post-generation FaceReducer pipeline

The processing service and CLI are independent of Gradio:

```sh
python -m majd_studio_3d.processing generated.glb output-directory --engine 2.1
python -m majd_studio_3d.processing generated.glb output-directory --engine 2mv --app-dir /path/to/majd_v9
```

The explicit flow is: preserve and inspect raw GLB → decide one absolute face budget →
native FaceReducer → Blender cleanup with that same budget → validate exported geometry →
record processed asset. FaceReducer uses `hy3dshape.postprocessors` for 2.1 and
`hy3dgen.shapegen.postprocessors` for 2mv. Both expose `max_facenum`:
[official 2.1 source](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/hy3dshape/hy3dshape/postprocessors.py),
[official 2mv source](https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/hy3dgen/shapegen/postprocessors.py).
Existing installed Hunyuan dependencies are reused; no weights are loaded by this stage.

Only `hunyuan_face_reducer` controls a native processor in this phase. Textured/UV inputs
skip the lossy PLY stage explicitly and use Blender. Native floaters/degenerate toggles
remain reserved; geometry cleanup already handles them through Blender.

Each processing run keeps an immutable raw copy, hash, stage statuses, counts, target,
warnings, errors, model/version references, and effective configuration. Schema **95**
adds `processing_runs` and preserves all earlier records. A processing failure retains
the raw snapshot; reducer failure is recorded, and Blender may still be attempted on raw,
but that run remains failed with raw fallback. Cleanup/validation failures never publish
the intermediate reduced model. Cancellation removes the entire partial processing folder.

Successful repeats reuse the existing output only when raw hash, configuration, engine,
pipeline revision, model/version context, raw snapshot, and final artifact all match.
Changed settings restart from the preserved raw model. Adjacent `processing.json`
provenance prevents double reduction even with another app directory. Recognizable
stage outputs lacking recoverable provenance are refused rather than treated as raw.
Database and file persistence are attempted independently; a successful output requires
durable local or database provenance. Historic config snapshots are frozen, including
Blender selection; current environment overrides affect new runs, not replayed jobs.

### Processing in the review UI

Select an asset and candidate in **Review**. The processing card shows the generation
state, actual processing stage, raw/target/reduced/final face counts, stage-specific
errors, warning count, fallback/reuse flags, and the configuration used. Missing
metadata displays `—`; no processing percentages are invented.

- **Start processing** uses the Phase C service with the current JSON settings.
- **Retry** is available after failure. **Cancel processing** signals that selected
  operation; it remains active until the backend confirms cancellation.
- **Reprocess** starts from backend-resolved immutable raw data. Unchanged compatible
  settings reuse the verified output without invoking FaceReducer or Blender again.
- **View raw** and **View processed** switch the existing viewer's preview only.
  Approval continues to use the normal stored candidate, not the preview override.
- **Open output folder** opens the processing directory on Windows/macOS; other
  platforms display its path.

Successful output becomes the review candidate. Failed processing uses the Phase C
raw fallback. Cancellation does not publish a partial output. Processing never changes
an approved version; approving a candidate still requires the existing explicit action.

Live stages come from processing-service callbacks and are refreshed every two seconds.
Terminal history is restored from SQLite, with a small UI activity journal preserving
reuse and interrupted-session display. Regenerated candidates cannot receive stale
processing updates, and switching selections cannot overwrite the new selection's review.
The selected-model controls retain the same processing service and approval workflow.

### Batch processing, retry, and recovery

Open **Review → Batch processing**. Select one or several visible generated assets,
or use **Select all visible** / **Clear selection**. Each asset uses its first ranked
review candidate; batch selection does not approve an asset or choose a version.
**Process Selected** and **Reprocess Selected** create a persisted batch. Neither starts
workers automatically: choose **Start Batch** explicitly.

The executor runs **one item at a time**, in recorded selection order. It reserves the
existing single-model controller and waits for the existing model-operation lock, keeping
batch work separate from generation and parts processing. There is no concurrency >1
option or worker pool. The UI only requests operations and reads persisted state.

When a batch is created, raw bytes are pinned under
`majd_v9/processing_batches/<batch>/raw/<item>/`, with a verified raw-source manifest.
The complete effective cleanup configuration is captured in SQLite. Later changes to
the global JSON settings or `BLENDER_PATH` do not change that batch. To use new settings,
create a new batch. Processing retains its original reuse identity, so compatible output
becomes **reused** without rerunning FaceReducer or Blender. Reused counts as completed.

Schema **96** adds `processing_batches` and `processing_batch_items` idempotently.
Batch records contain timestamps, status, counters, frozen settings and execution ownership.
Items preserve order, asset/candidate identity, raw signature/path, run reference, stage,
retry count, timestamps, warnings and errors. Repeated selections of the same asset/raw
within a batch are normalized. Existing projects, processing runs and approved versions
remain readable and unchanged.

- **Retry Failed** / **Retry Item** queue failed items and increment their retry count.
  Choose Start Batch to resume. Retries use pinned raw input and the original batch
  configuration. A verified successful run left by an interrupted attempt is reused.
- **Cancel Item** cancels only that item and lets remaining items continue.
  **Cancel Batch** prevents queued items from starting and stops active work. Successful
  and reused results remain preserved. Repeated cancellation is safe.
- Stage notifications drive live display; no percentage estimates are invented.
  Item/batch cancellation is also polled by the existing subprocess watchdog, so a
  cancellation request from another controller reaches a long-running step.
- On application startup, abandoned execution leases are recovered. Active unfinished
  items become failed with an **interrupted** stage and are retryable. Terminal items
  remain terminal, queued items remain queued, and expensive processing never resumes
  automatically. Completed batches with only an abandoned lease keep their completed status.
- Each attempt has a distinct work directory. Failed/interrupted working files are never
  retry inputs; pinned raw snapshots remain available for recovery. Unexpected process
  termination can leave unverified work directories for inspection.
- **Clear from history** hides a completed batch from the UI without deleting its batch,
  raw snapshots, item records, or processing history.

Logs are written to `majd_v9/logs/batches/<batch>.jsonl` for creation, queueing, starts,
terminal outcomes, cancellation, retry and recovery. Polling does not create log entries.
The panel restores the latest visible batch after reload without starting execution.

Real Blender runtime verification remains **intentionally deferred** and does not block
Phase D UI usage. A completed UI state does not claim production/runtime verification:

```sh
BLENDER_PATH=/Applications/Blender.app/Contents/MacOS/Blender python -m unittest tests.test_cleanup_blender -v
BLENDER_PATH=/Applications/Blender.app/Contents/MacOS/Blender python -m unittest tests.test_processing_blender -v
```

These tests auto-skip when Blender 4.x is unavailable. A skipped class is not successful
geometry verification; they must run and pass on an installed Blender 4.x. The second
suite additionally requires the actual installed Hunyuan3D-2.1 FaceReducer runtime.
It exercises native reduction, real Blender cleanup, validation, raw preservation,
metadata persistence, and repeat processing without mocks.

## حدود التحقق

توجد اختبارات محلية للتخزين والتحديث، وفحوص بناء للحزم. لم يُتحقق في هذه البيئة من استدلال Hunyuan الحقيقي أو تشغيل Blender أو دورة التثبيت والتحديث الكاملة على جهاز Windows مع GPU.

### Human review and approval (Phase F)

Open **Review → Human review & approval**. Processing and review are independent:
`processing_status=success` can coexist with `review_status=NEEDS_REVIEW`.
Ranking recommends a candidate; it never selects or approves one. Select an asset,
compare its candidates, choose a candidate, and click **Select Candidate** to save
that choice. **Approve Selected Candidate** approves the persisted choice. The
older approval button follows the same rule and cannot default to rank 1.

Review states are **NEEDS_REVIEW**, **APPROVED**, **REJECTED**, and
**RETRY_REQUESTED**. Selection is saved independently and survives app restart.
A selection can be cleared before approval. Approved selection is locked until
an explicit retry request. **Reject Asset** records the reason without starting
processing or deleting anything. Failed-processing assets remain accessible through
**Failed Processing**, including assets with no generated candidates.

Approval creates a stable asset version, updates the existing library, and records
an append-only decision. Repeating the same approval returns the existing version.
All candidate snapshots, raw inputs, processing configurations, validation data,
warnings, provenance and previous approved versions remain available. Generated
filenames may be reused, so review registration freezes model artifacts before they
can be overwritten. Candidate cards distinguish current ranking recommendations,
human selections, approved candidates and previous attempts. The existing Blender
finalization step remains available for assets with automatic Blender enabled;
this review layer introduces no additional geometry reduction.

**Request Retry** queues preserved raw inputs through the existing serial Phase E
executor; choose **Start Batch** explicitly. A selected candidate is retried; without
selection, the current candidate set is queued rather than silently choosing rank 1.
Historical settings remain immutable. Candidates with different configurations use
separate serial batches. Repeated requests reuse a pending retry request. Compatible
successful processing may reuse its verified result; failed processing runs again.
This action does not regenerate shape or change inference settings. After successful
processing finishes, the asset returns to NEEDS_REVIEW; approval is still explicit.
Failures and cancellations retain RETRY_REQUESTED and the existing recovery controls.

**Bulk Approve** handles each asset independently. Only NEEDS_REVIEW assets with an
explicit saved selection are eligible. Missing selections and other validation failures
produce a per-asset error while other valid approvals succeed. No bulk action selects
rank 1. Review history shows action, timestamp, candidate, previous/new state and reason.
There is no authenticated reviewer identity in the current local app.

Schema **97** adds `review_assets`, `review_candidates` and `review_events` without
replacing Phase E tables. Migration is additive and idempotent. Legacy successful or
reused batch items initialize as NEEDS_REVIEW; existing approved-version files remain
unchanged, and migration never infers a human review approval. Candidate metadata is
backfilled when the review service initializes or reads an asset. Database writes for
selection, approval/version creation, rejection and retry queue/decision registration
are transactional. Library publication failure is recorded as a warning after durable
approval rather than undoing it.

A completed processing batch is **processing complete**, not approved. The batch panel
separately shows Processed, Needs Review, Approved, Rejected, Retry Requested and Failed.
Processed/Failed batch counts refer to items; review counts refer to distinct assets.
Reloading restores selections, decisions, candidates and batch state without launching
processing or making approvals. Real Blender/native FaceReducer verification remains
required separately; mocked UI verification does not replace it.

### Asset Library and immutable versions (Phase G)

Open **Library → Asset Library**. This is a durable production inventory independent
of generation assets, processing attempts and batches. Phase F approval remains the
sole authority for publishing results.

- **Unassigned Approved Results** contains existing valid Phase F approvals. Select
  one explicitly and **Create New Asset**, or select an existing identity and
  **Add as Version to Existing Asset**. Names never imply asset identity.
- Versions start at **v001** and increase monotonically. Repeated publication returns
  the existing asset/version. The same approved result is assigned to one identity;
  publishing it to a different target reports its existing assignment.
- **View Version** inspects without promotion. **Set Current Version** promotes or
  reverts the pointer without duplicating or altering historical versions.
- **Open Source Review** navigates to the existing review; **Open Source Batch** opens
  its read-only persisted details, including batches hidden from normal history.
- Rename display names and edit type/category/tags while retaining stable IDs and
  artifact paths. Archive hides assets from active views; restore preserves every
  version and event. Restore an archived identity before attaching new versions.
- Search/filter by name, type, source review state, archive state, update date, version
  count and tags. Library counters remain separate from processing counters.

Schema **98** adds `library_assets`, `library_asset_versions`, and append-only
`library_events`. Migration is additive and idempotent; it does not automatically
create production assets from legacy approvals. Existing project/global library and
variant controls remain available. All source candidates and processing history remain
preserved.

Versions freeze source approval/candidate/processing/batch references, raw provenance,
configuration and decision snapshots, validation/warnings, timestamps and artifact
references. They reuse Phase F's immutable approved files without new heavy copies.
SHA256 evidence detects altered artifacts and prevents changed models from opening as
verified versions. A stale UI selection cannot silently publish a newer approval.

See [Asset Library architecture](docs/asset-library.md) for the schema, lineage,
publication transactions, integrity checks, archive semantics and verification limits.
