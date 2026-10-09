# Majd Studio 3D
## الرؤية العامة للمشروع وخطة التطوير

**اسم الوثيقة:** Project Overview & Development Roadmap  
**الحالة:** Living Document  
**الإصدار المرجعي الحالي:** V9 Phase 2.1 Beta  
**الغرض:** تعريف الفكرة العامة، حدود النظام، المعمارية المستهدفة، ومراحل التحسين والتطوير القادمة.

---

# 1. الفكرة العامة

Majd Studio 3D هو نظام إنتاج أصول ثلاثية الأبعاد متعدد المشاريع والتوجهات الفنية، وليس مجرد واجهة لتشغيل نموذج Image-to-3D.

الهدف النهائي هو بناء بيئة إنتاج موحدة تستطيع إدارة دورة حياة الأصل كاملة:

```text
Project
  ↓
Style Profile
  ↓
Reference Set
  ↓
Input Images / Character Sheet
  ↓
Preflight
  ↓
Calibration
  ↓
3D Generation
  ↓
Candidate Comparison
  ↓
Style / Geometry QA
  ↓
Human Approval
  ↓
Texture / Materials
  ↓
Topology / Rig / Animation Readiness
  ↓
Blender Production Asset
  ↓
Project / Global Library
  ↓
Export to Majd Studio / Unity / Unreal / Web
```

النظام مصمم ليستوعب:

- شخصيات.
- Props.
- عناصر بيئة.
- مباني.
- مجسمات عضوية.
- مركبات.
- أصول خاصة بالألعاب أو الأفلام أو الفيديوهات.
- أكثر من مشروع في نفس البيئة.
- أكثر من Style داخل المشروع الواحد.
- مكتبة أصول مشتركة بين المشاريع.

---

# 2. المشكلة التي يحلها المشروع

الأدوات التقليدية لتحويل الصور إلى 3D تنتج Mesh فقط، لكنها لا تحل المشاكل الإنتاجية التالية:

- الحفاظ على هوية الشخصية بين الأصول.
- توحيد التوجه الفني عبر مشروع كامل.
- إدارة أكثر من مشروع.
- إدارة الإصدارات Versions.
- مقارنة عدة Candidates.
- التحقق من النسب والـSilhouette.
- توحيد صور Front / Back / Side.
- معرفة مصدر الأصل وإعدادات توليده.
- التأكد من أن الأصل مناسب لاحقًا للـRigging والتحريك.
- تتبع Texture وTopology وRig status.
- إعادة استخدام الأصول بين المشاريع بدون تخريب الأصل الأصلي.

Majd Studio 3D يبني طبقة إنتاج كاملة فوق نماذج التوليد بدل الاعتماد عليها كنهاية للـPipeline.

---

# 3. المبادئ الأساسية

## 3.1 Project First

كل أصل يجب أن ينتمي إلى:

```text
Workspace
  └─ Project
      └─ Style Profile
          └─ Asset
              └─ Version
```

لا يوجد افتراض أن جميع مشاريع Majd Studio تستخدم نفس الشكل أو نفس الإعدادات.

---

## 3.2 Style Profile مستقل عن Project

المشروع يمكن أن يمتلك أكثر من توجه فني:

```text
Sami Stories
├─ Majd Soft 3D
├─ Majd 2.5D
├─ Cinematic Stylized
└─ Experimental
```

كما يمكن إعادة استخدام Style Profile عالمي في أكثر من مشروع.

---

## 3.3 Human Approval هو القرار النهائي

أي Score آلي هو **إشارة QA** فقط.

لا يجوز اعتبار:

- Silhouette Score.
- Geometry Style Score.
- Preflight Score.
- Automated Conformance Score.

بديلًا عن المراجعة البصرية البشرية.

التدفق الصحيح:

```text
Automation
   ↓
Evidence
   ↓
Human Review
   ↓
Approve / Reject
```

---

## 3.4 عدم تخريب الأصل

الأصل الأصلي والنسخ المعتمدة لا يتم الكتابة فوقها.

مثال:

```text
Sami
├─ v001
├─ v002
├─ v003
└─ v004
```

كل نسخة تحتفظ بـ:

- Source images.
- Style.
- Generation engine.
- Seed.
- Resolution.
- Settings.
- Candidates.
- Scores.
- QA evidence.
- Approved candidate.
- GLB.
- Blender file.
- Texture status.
- Rig status.

---

## 3.5 فصل مراحل الإنتاج

لا يتم خلط كل شيء في خطوة واحدة.

المراحل مستقلة:

```text
Shape
Texture
Topology
Rig
Animation Readiness
Export
```

يمكن إعادة تنفيذ مرحلة بدون إعادة بناء المراحل السابقة إذا لم يكن ذلك ضروريًا.

---

# 4. المرجعية الفنية والأخلاقية

Majd Studio يجب أن ينتج أعمالًا أصلية وغير منتهكة لحقوق النشر.

القواعد الأساسية:

- لا يتم نسخ هوية شخصية محمية.
- لا يتم استنساخ تصميم فنان أو استوديو بعينه.
- المراجع الخارجية تستخدم تقنيًا لدراسة:
  - topology
  - deformation
  - rigging
  - facial systems
  - volume construction
  - production practices
- المرجع الفني المعتمد للمشروع هو المصدر الأول لهوية الأصل.
- يجب تسجيل provenance ومصدر أي Reference خارجي.
- يجب تسجيل License عندما يكون الأصل أو المرجع من مصدر خارجي.

بالنسبة لإنتاج Majd Studio المرئي والمحتوى النهائي، يبقى التوجه متوافقًا مع المرجعية الإسلامية للمشروع، ويتم تجنب العناصر غير المناسبة لهذا التوجه.

---

# 5. المعمارية المستهدفة

```text
Majd Studio 3D
│
├── Project Manager
│   ├── Projects
│   ├── Project Settings
│   └── Project Libraries
│
├── Style System
│   ├── Style Profiles
│   ├── Style References
│   ├── Style Rules
│   └── Style Lock
│
├── Asset Manager
│   ├── Asset Identity
│   ├── Versions
│   ├── Variants
│   ├── Dependencies
│   └── Provenance
│
├── Input Pipeline
│   ├── Preflight
│   ├── Background Cleanup
│   ├── Multi-View Validation
│   ├── Calibration
│   └── Landmark Alignment
│
├── Shape Generation
│   ├── Hunyuan3D-2.1
│   ├── Hunyuan3D-2mv
│   └── Future Engines
│
├── Review System
│   ├── Three.js Viewer
│   ├── Candidate Compare
│   ├── Reference Overlay
│   ├── Silhouette View
│   ├── Wireframe
│   └── QA Evidence
│
├── Texture / Material Pipeline
│
├── Blender Pipeline
│   ├── Scale
│   ├── Grounding
│   ├── QA
│   ├── Retopo
│   ├── UV
│   ├── Rig
│   └── Packaging
│
├── Libraries
│   ├── Global Library
│   └── Project Library
│
└── Export
    ├── Majd Studio
    ├── Blender
    ├── Unity
    ├── Unreal
    ├── GLB
    └── Archive Master
```

---

# 6. Project Library و Global Library

## Project Library

تحتوي الأصول الخاصة بالمشروع:

```text
Sami
Bibi
Sami House
School
Story-specific Props
Project-specific Clothes
```

## Global Library

تحتوي الأصول العامة القابلة لإعادة الاستخدام:

```text
chairs
cups
trees
rocks
generic buildings
generic props
```

يمكن إنشاء Variant خاص بالمشروع:

```text
Global Asset
wooden_chair_v1
       ↓
Project Variant
wooden_chair_sami_style_v1
```

مع الاحتفاظ بعلاقة الأصل:

```text
parent_asset_id
```

---

# 7. Style Profile

Style Profile يجب أن يصبح لاحقًا مواصفة إنتاج فعلية وليس مجرد اسم.

يحتوي على:

```text
Identity
Visual References
Shape Language
Proportions
Head / Body Ratio
Eye Rules
Hair Construction
Edge Softness
Silhouette Rules
Symmetry Policy
Poly Budget
Texture Style
Material Rules
Color Palette
Lighting Reference
Camera Defaults
Generation Defaults
QA Thresholds
Export Defaults
```

---

# 8. Style Lock

Style Lock يمنع خروج الأصل عن قواعد المشروع بدون قرار واعٍ.

مثال:

```text
Style Lock
├─ Silhouette >= 92%
├─ Preflight >= 90%
├─ Geometry Style >= 88%
├─ Faces <= 50,000
├─ Resolution = 256
├─ Guidance = 5.0
└─ Approved Style Profile required
```

النتيجة:

```text
PASS
WARN
FAIL
```

في حالة FAIL:

```text
Generation Gate
أو
Approval Gate
```

حسب نوع القاعدة.

Override يجب أن يكون:

- صريحًا.
- مسجلًا.
- مرتبطًا بسبب.

---

# 9. الوضع الحالي — V9 Phase 2.1 Beta

النسخة الحالية تستهدف الأساس التالي:

## إدارة المشاريع

- Multiple Projects.
- Multiple Style Profiles.
- Project Library.
- Global Library.
- Project Variants.
- Asset Versioning.

## Generation

- Hunyuan3D-2.1.
- Hunyuan3D-2mv.
- Auto engine selection.
- Multiple Candidates.
- Batch queue.
- Resume.
- OOM retry strategy.

## Review

Three.js Viewer يدعم:

- Orbit.
- Zoom.
- Pan.
- Front.
- Back.
- Left.
- Right.
- 3/4.
- Top.
- Perspective.
- Orthographic.
- Material.
- Clay.
- Wireframe.
- Silhouette.
- Normals.
- Grid.
- Auto Rotate.
- Fullscreen.
- Reference Overlay.
- Candidate Compare.
- Synchronized cameras.

## Phase 2.1 Input QA

- Style References.
- Image Preflight.
- Basic Multi-View consistency checks.
- Silhouette-based calibration.
- Generation Gate.
- Style Conformance evidence.

### ملاحظة

Phase 2.1 ما زالت Beta.

تمت إضافة تنزيل الأوزان عند الحاجة مع تقدم فعلي واستكمال الملفات الناقصة، وشاشة لتقسيم الأصول عبر P3-SAM وإعادة بناء الأجزاء عبر XPart وحفظ الجزء المعتمد كأصل مستقل. كود الأدوات يُنزّل من النسخة الرسمية المثبتة وتُجهّز اعتمادياته في بيئة منفصلة. تشغيل CUDA والتقسيم وإعادة البناء على جهاز Windows الحقيقي يحتاج اختبارًا إنتاجيًا؛ وجود الأوزان على القرص لا يُعد دليلًا على جاهزية التنفيذ.

تنظيم الكود الحالي: `majd_studio_3d/` للتطبيق والتخزين وفحص المدخلات والتحديث، `viewer/` للعارض، `scripts/` للتثبيت وبناء الإصدارات، و`tests/` لاختبارات الرجوع. تبقى نقاط التشغيل القديمة كواجهات توافق مؤقتة.

تمت إضافة آلية تحديث ملفات التطبيق عبر GitHub Releases عند تشغيل النسخة المثبتة.
تتحقق من SHA-256، تحفظ نسخة احتياطية، وتعود لملفات البرنامج السابقة إذا فشل الإقلاع، مع إبقاء قاعدة البيانات الحالية ونسخة احتياطية منفصلة لها.
تحتاج هذه الآلية أيضًا إلى اختبار فعلي على جهاز Windows قبل اعتبارها جاهزة للإنتاج.
التحديثات التي تغيّر اعتماديات Python أو المشغّل تتطلب تثبيت حزمة Bootstrap جديدة.

يجب عدم اعتبار أي ميزة Production-Ready قبل اختبارها فعليًا على جهاز الإنتاج مع:

- GPU.
- Hunyuan weights.
- Blender.
- ملفات حقيقية.
- Batch طويل.

---

# 10. خطة التطوير

## Phase 2.2 — Landmark Calibration

### الهدف

تحويل Calibration من Bounding Box / Silhouette فقط إلى Alignment دلالي.

### Character Landmarks

```text
Top of Head
Chin
Eye Centers
Shoulders
Elbows
Hands
Pelvis
Knees
Feet
```

### المطلوب

- Landmark detector.
- Manual correction UI.
- Landmark confidence.
- Cross-view consistency.
- Proportion extraction.
- Calibration report.
- Before / After overlay.

### معيار الإنجاز

عند إدخال Character Sheet متعدد الزوايا يستطيع النظام قياس محاذاة الجسم والنسب وإظهار الأخطاء بوضوح قبل التوليد.

---

## Phase 2.3 — Advanced Preflight

### المطلوب

- Crop validation.
- Background quality.
- Occlusion detection.
- Missing-body detection.
- Pose consistency.
- Scale consistency.
- View classification.
- Duplicate-view detection.
- Wrong-side detection.
- Character completeness.
- Image quality warnings.

### Gate

لا يبدأ Generation عند وجود خطأ حرج.

---

## Phase 2.4 — Advanced Style Conformance

تحسين Style Match ليشمل:

```text
Silhouette
Proportions
Volume Distribution
Head / Body Ratio
Limb Ratios
Shape Language
Symmetry
Surface Complexity
Poly Distribution
```

بدل الاعتماد على Score واحد.

---

# 11. Phase 3 — Texture & Materials

## الهدف

إضافة Pipeline مستقل للخامات.

```text
Shape Approved
      ↓
Texture Input
      ↓
Texture Generation
      ↓
PBR Material
      ↓
Texture QA
      ↓
Texture Approved
```

### المطلوب

- Texture engine abstraction.
- Hunyuan Paint أو بديل مناسب.
- Base Color.
- Roughness.
- Normal.
- Metallic عند الحاجة.
- UV validation.
- Texture resolution profiles.
- Texture variants.
- Texture versioning.
- Reference comparison.

### قاعدة مهمة

```text
ORIGINAL_MESH
```

يبقى الأصل الهندسي المعتمد.

أي عملية UV أو Texture لا يجوز أن تغيّر الـTopology بصمت.

---

# 12. Phase 4 — Production Geometry

بعد Shape + Texture:

```text
Retopology
Topology QA
UV QA
Normals
Manifold
Disconnected Components
Scale
Origin
Grounding
LOD
```

### Character-specific

- Deformation loops.
- Shoulder topology.
- Elbow topology.
- Knee topology.
- Face topology.
- Mouth loops.
- Eye loops.
- Finger topology.

الهدف ليس فقط Mesh جميل، بل:

```text
Production-Ready Asset
```

---

# 13. Phase 5 — Character Production System

هذه المرحلة تربط Majd Studio 3D مع نظام الشخصيات الكامل.

```text
Character Sheet
      ↓
Calibration
      ↓
Base Mesh
      ↓
Retopo
      ↓
Rigify
      ↓
Face Rig
      ↓
Shape Keys
      ↓
Animation Ready
```

### المطلوب

- Character Base Mesh Standard.
- Character Asset Specification.
- Rig binding.
- Rigify integration.
- Facial controls.
- Expression set.
- Hand standard.
- Hair standard.
- Clothes attachment.
- Accessories.
- Character generator parameters.

الهدف النهائي:

```text
Majd Character Generator
```

---

# 14. Phase 6 — Environment Production System

إضافة منطق خاص للبيئات بدل معاملتها كشخصية كبيرة.

### المطلوب

- Building mode.
- Modular environment mode.
- Terrain assets.
- Props grouping.
- World scale.
- Ground planes.
- Collision-ready variants.
- LODs.
- Scene assembly.
- Environment Style Profiles.

---

# 15. Phase 7 — Export & Integration

Export Profiles:

```text
Majd Studio Production
Blender Master
Unity
Unreal
Web GLB
Archive Master
```

كل Profile يحدد:

- Scale.
- Coordinate system.
- Materials.
- Textures.
- Rig.
- Animation.
- Compression.
- LOD.
- Naming.
- Metadata.

---

# 16. Phase 8 — Automation & Reliability

قبل اعتبار النظام Production Tool يجب إضافة:

## Reliability

- Crash-safe jobs.
- Atomic writes.
- Job retry policy.
- Checksums.
- Database backup.
- Asset recovery.
- Missing-file repair.
- Queue recovery.

## Testing

```text
Unit Tests
Integration Tests
Pipeline Tests
Regression Tests
Golden Assets
Viewer Tests
Blender Validation
```

## Observability

- Logs.
- GPU VRAM.
- RAM.
- Generation time.
- Disk usage.
- Error classification.
- Model load time.
- Asset throughput.

---

# 17. Phase 9 — Smart Planner

لاحقًا لا يختار المستخدم كل إعداد يدويًا.

النظام يقرأ:

```text
Project
Style
Asset Type
Input Quality
Hardware
Target Output
```

ثم يقرر:

```text
Engine
Resolution
Candidate Count
Steps
QA Plan
Texture Plan
Blender Plan
Export Plan
```

الهدف:

```text
Intent
  ↓
Planner
  ↓
Execution Graph
  ↓
Validated Asset
```

---

# 18. مراحل نضج الأصل

يجب استخدام State Machine واضحة:

```text
DRAFT
↓
INPUT_READY
↓
PREFLIGHT_PASSED
↓
CALIBRATED
↓
SHAPE_GENERATING
↓
SHAPE_REVIEW
↓
SHAPE_APPROVED
↓
TEXTURE_PENDING
↓
TEXTURE_REVIEW
↓
TEXTURE_APPROVED
↓
TOPOLOGY_REVIEW
↓
RIG_PENDING
↓
RIG_READY
↓
PRODUCTION_READY
↓
PUBLISHED
```

لا يتم اختصار المراحل بصمت.

---

# 19. Asset Manifest المستهدف

كل Version يجب أن يمتلك Manifest مشابهًا لـ:

```json
{
  "asset_id": "sami",
  "version": "v004",
  "project": "sami-stories",
  "style": "majd-soft-3d",
  "asset_type": "character",

  "sources": [],
  "references": [],

  "shape": {
    "engine": "hunyuan3d-2mv",
    "seed": 1234,
    "settings": {}
  },

  "qa": {
    "preflight": {},
    "calibration": {},
    "silhouette": {},
    "style": {}
  },

  "texture": {
    "status": "pending"
  },

  "topology": {
    "status": "pending"
  },

  "rig": {
    "status": "pending"
  },

  "approval": {
    "status": "approved",
    "approved_by": "human"
  }
}
```

هذا هو السجل المرجعي للأصل.

---

# 20. أولويات التطوير الحالية

الترتيب المقترح من الحالة الحالية:

```text
1. Validate V9 Phase 2.1 على جهاز Windows الحقيقي
2. تثبيت المشاكل الناتجة من الاختبار
3. Landmark Calibration
4. Advanced Multi-View Preflight
5. Style Conformance V2
6. Texture Pipeline
7. Blender Geometry QA
8. Production Topology
9. Character/Rig integration
10. Environment production tools
11. Export profiles
12. Planner / Execution Graph
```

---

# 21. ما لا يجب فعله الآن

لتجنب تضخم المشروع:

- لا نبني Animation Engine داخل Asset Factory الآن.
- لا نبني Game Engine.
- لا ندمج Texture قبل تثبيت Shape pipeline.
- لا نعتمد AI Score كبديل للمراجع البشرية.
- لا نعمل Retopo آلي كامل قبل تعريف معايير الـTopology.
- لا نسمح لكل Project بتغيير Core architecture.
- لا نربط Core مباشرة بأسماء Rigify أو Blender-specific identifiers.
- لا نحول كل ميزة تجريبية إلى Production feature بدون Validation.

---

# 22. معايير اعتبار المرحلة Production-Ready

أي مرحلة لا تعتبر Production-Ready إلا إذا:

1. تعمل على Assets حقيقية.
2. لديها اختبارات.
3. لديها failure handling.
4. لا تخرب النسخة السابقة.
5. Evidence محفوظ.
6. يمكن إعادة النتيجة reproducibly عندما يكون ذلك ممكنًا.
7. يمكن الرجوع إلى Version سابق.
8. يوجد Human Review عند القرارات الفنية.
9. لا توجد تغييرات صامتة في Mesh أو Style أو Metadata.
10. تم اختبارها في Batch وليس Asset واحد فقط.

---

# 23. الرؤية النهائية

الهدف النهائي ليس:

```text
Image → Mesh
```

بل:

```text
Creative Intent
      ↓
Project Rules
      ↓
Style System
      ↓
References
      ↓
Validated Inputs
      ↓
Generation / Construction
      ↓
Automated QA
      ↓
Human Art Direction
      ↓
Texture / Topology / Rig
      ↓
Production Asset
      ↓
Animation / Film / Game
```

Majd Studio 3D يجب أن يصبح طبقة إدارة وإنتاج مستقلة عن نموذج AI معين.

Hunyuan اليوم مجرد Backend.

غدًا يمكن إضافة:

```text
Another Image-to-3D Model
Procedural Blender Generator
Manual Blender Asset
Scanned Asset
Custom Majd Generator
```

بدون إعادة تصميم الـProject / Style / Asset / Version / QA architecture.

---

# 24. القرار المعماري الأهم

يجب الحفاظ على الفصل التالي:

```text
Majd Core
    لا يعرف Hunyuan
    لا يعرف Rigify
    لا يعرف أسماء Bones
    لا يعتمد على Blender UI

Adapters
    Hunyuan Adapter
    Blender Adapter
    Texture Adapter
    Rig Adapter
    Export Adapter
```

وبذلك يبقى النظام قابلًا للتطوير لسنوات بدون إعادة بناء الـCore كلما تغيرت أداة.

---

# 25. تعريف النجاح

يعتبر المشروع ناجحًا عندما يستطيع المستخدم:

1. إنشاء Project.
2. تعريف Style.
3. رفع Character Sheet أو References.
4. إضافة عشرات الأصول.
5. ترك النظام يقوم بالـPreflight والـCalibration والـGeneration.
6. مقارنة Candidates بصريًا.
7. رفض أو اعتماد النتائج.
8. إنتاج Blender Assets منظمة ومثبتة الإصدارات.
9. تطبيق Texture وTopology وRig عند الحاجة.
10. استخدام نفس المكتبة في أفلام وفيديوهات وألعاب بدون فقدان المصدر أو الهوية أو التاريخ الإنتاجي.

---

**هذه الوثيقة يجب تحديثها مع كل Milestone معماري رئيسي، ولا تعتبر مواصفة تنفيذ تفصيلية لكل Feature.  
المواصفات التفصيلية يجب أن توضع في ADRs وFeature Specs مستقلة.**
