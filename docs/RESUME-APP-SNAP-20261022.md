# استئناف: نقل تطبيق Snap إلى v3 (22 أكتوبر 2026)

الفرع `claude/app-snap-20261022`؛ طلب دمج مسودة.

## المصدر
- `snap-bot-v2/apps/snap-bot/snap_app` في الالتزام `964b2bb`، وهو نفسه المثبّت على khalid-bots بعد نجاح 670/670.

## ما انتقل
- **`apps/snap-bot/snap_bot/`:** 40 وحدة من المختبر بأسمائها، ومعها `madar/*.mjs` و`data/`.
- **الهيكل:** بقي هيكل v3 (`cli.py` و`main.py` و`__main__.py`) كما هو.
- **انتقلت أيضًا:** `actions_preview.py`، لأن `server_preview` (مسار السيرفر) يستخدم `render_bundle`. أما مسارات GitHub Actions نفسها فلم تنتقل.
- **من النواة الآن، لا نسخ محلية:**
  - `adoption_pending`: صار `studio_core.repair.adoption`.
  - محرك الصيانة: صار `studio_core.repair`.
  - `maintenance.py` في التطبيق بقي واجهة سطر الأوامر فقط.
- **`config/snap/`:** `weather.toml` و`did_you_know.toml` و`foundation.toml` و`send-reconciliations.json`.
- **`assets/fonts/`:** الخطوط المرخصة وmanifest بلا تغيير، والبصمات كما هي.
- **مسارات الاستيراد:** حُوّلت إلى شجرة v3، مثل `studio_core.jobs.gateway` و`network.egress` و`observability.watchdog` و`network.resilience`.

## الاختبارات
- **المنقول:** 41 ملف اختبار من المختبر في `apps/snap-bot/tests/`، ومعها `run_agent_runtime.mjs`.
- **ما لم ينتقل:**
  - اختبارات النواة: موجودة أصلًا في `packages/studio-core/tests`.
  - اختبارات بنية المختبر: سير العمل، وwheel، وسياسة الفرع.
- **تعديلات لازمة، بلا أثر سلوكي:**
  - مسارات الإعداد صارت `config/snap/` بجذر المستودع.
  - هدفا العملية الفرعية لاختبارات N1 نُقلا إلى `n1_spawn_targets.py`، لأن v3 يستورد ملفات الاختبار بنمط importlib.
  - حراس «شكل الكود» صارت تقارن بلا مسافات أو علامات تنصيص، بعد `ruff format`.
  - حُذف اختبار ملفات سير عمل المختبر وحده.
- **النتيجة بمستخدم غير root (Python 3.12):**
  - **550 ناجحة**، و3 تحتاج sudo وsystemd. هي نفسها في المختبر، وتنجح في CI وعلى السيرفر.
  - مع النواة: 725 ناجحة.
- **Python 3.11:** النتيجة نفسها.
- **`ruff format`** طُبّق مع الإصلاحات الآمنة فقط (ترتيب الاستيراد وتقسيمه)، وأُعيد تشغيل كل الاختبارات بعده.

## CI
- **في مهمة الاختبار:**
  - Ubuntu 24.04 مثبّت، لأن ملف الرندر يتطلب FFmpeg 6.1.1-3ubuntu5.
  - Node 22، وFFmpeg، والخطوط.
  - `run_agent_runtime.mjs`.
  - `MADAR_REQUIRE_REAL_RENDER=1`.

## الدَّين الموثّق (يُسدَّد وحدة وحدة، مثل النواة)
- **mypy:** 38 وحدة من `snap_bot` في `[[tool.mypy.overrides]]` بقيمة `ignore_errors = true`. تخرج الوحدة من القائمة حين تُنمَّط.
- **ruff:** قواعد مستثناة لملفات التطبيق واختباراته في `per-file-ignores`.
- **أدوات التخريب الخاصة بالتطبيق** (`tools/check_*_mutations.py` في المختبر، 25 أداة): لم تنتقل بعد. أهدافها نصوص بصياغة المختبر قبل التنسيق، وتُعاد كتابتها في دفعة لاحقة.
