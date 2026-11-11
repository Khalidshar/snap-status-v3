# استئناف: الدفعة 1 من المختبر إلى الإنتاج (13 أكتوبر 2026)

الفرع: `claude/batch1-foundation-20261013` من `main`. طلب دمج مسودة، دون دمج.

## الطلب
وافق المالك على دمج #49 و#50 في `snap-bot-v2`، و#16 في `snap-bot`، و#3 هنا، وطلب بدء نقل الدفعة الأولى. المرجع `GRADUATION.md` و`ARCHITECTURE_V3_AGREED.md` (§2 و§18).

## ما انتقل (REFACTOR: السلوك نفسه بمعايير v3)
النواة من `snap-bot-v2/packages/studio-core` (الإصدار 0.9.1) إلى شجرة المخطط:

| الوحدة هنا | المصدر في المختبر |
|---|---|
| `studio_core/jobs/` | `repository.py` ← `jobs.py`، و`gateway.py` و`component_lock.py`. أما `model.py` فهو `Job` الهيكلي القديم، وبقي لأن `runtime` والتطبيقين يستعملونه |
| `studio_core/storage/` | `workspace.py` ← `storage.py`، و`backup.py` |
| `studio_core/observability/` | `redaction.py` ← `observability.py`، و`watchdog.py` ← `runtime_watchdog.py`، و`agent_runs.py` و`agent_inventory.py` |
| `studio_core/network/` | `client.py` ← `network.py`، و`egress.py` |
| `studio_core/config.py` | فئة `Config` من المختبر، بجانب دوال الإعداد الهيكلية الموجودة |
| `studio_core/cli.py` | من المختبر |

بقيت الوحدات الهيكلية `agents` و`content` و`delivery` و`quality` و`runtime` و`security` حتى دفعاتها.

## كيف حُفظ السلوك
1. **نسخ حرفي أولًا**، مع تعديل مسارات الاستيراد فقط: نجح 107 اختبارات (91 من المختبر و16 قديمة).
2. **`ruff format`**، ثم تقسيم جمل SQL الطويلة **بشرط تطابق شجرة AST** قبل التقسيم وبعده. أي ملف تغيّرت شجرته يُرفض.
3. جمل المخطط والـSQL في `repository.py` بقيت كما هي بالبايت، لأن نص المخطط محفوظ في `sqlite_master` لكل قاعدة قائمة. استثناء E501 لهذا الملف وحده موثّق في `pyproject.toml`.
4. **تعريف الأنواع لـ`mypy --strict`**، وهي تعليقات لا تغيّر التنفيذ. حالات تحتاج انتباهًا:
   - `host is None or host not in hosts`: يعطي النتيجة نفسها، لأن `None` لا يكون في قائمة نصوص؛
   - `cast` في `cli.demo`؛
   - `type: ignore[attr-defined]` لخاصية `_context` الداخلية في `http.client`.
5. تغييران صغيران في السلوك، كلاهما خارج المسار الفعلي:
   - `assert` في أمر `demo` صار `RuntimeError`، لأن قاعدة S101 تمنع `assert`، ولأن `python -O` كان سيحذفه؛
   - `zip` صار `itertools.pairwise`، وهو مكافئ تمامًا.

## اختبار التخريب المتعمد: `scripts/check_core_mutations.py` (14/14، في CI)
كشف ثغرتين حقيقيتين **موجودتين في المختبر أيضًا**، وأُضيف لكل منهما اختبار:
1. **حذف فحص التوقيع (HMAC) لم يكشفه أي اختبار.** الاختبار القديم يرسل أمرًا فارغًا، فيُرفض بسبب المرسل قبل التوقيع. الاختبار الجديد `test_forged_signature_from_allowed_owner_does_not_enqueue` يرسل أمرًا من مالك مسموح بتوقيع مزوّر.
2. **حذف حماية GC للتسليم المعلّق لم يكشفه أي اختبار**، لأن طبقة سابقة تمنع الوصول إليها. الاختبار الجديد `test_gc_keeps_assets_when_delivery_evidence_appears_after_terminal_mark` يضيف دليل تسليم بعد العلامة النهائية.

يجب نقل الاختبارين إلى المختبر في `snap-bot-v2/tests/test_foundation.py`.

## التحقق
- `scripts/lint.sh`: ruff وتنسيقه نظيفان، و`mypy --strict` نظيف لـ33 ملفًا.
- `scripts/test.sh all`: 124 اختبارًا، والتغطية 84% (الحد 80%)، على Python 3.11 و3.12.
- أداة التخريب: 14/14.

## لم يُنفّذ
- لا تحقق على السيرفر لنسخة v3 بعد. هذا شرط التخرّج 2، ويكون عن طريق Codex.
- لا نشر ولا Docker ولا جدولة. `cd.yml` يدوي فقط.
- إصدار `studio-core` هنا بقي 0.1.0، لتجنّب تغيير `uv.lock` في هذه الدفعة.
