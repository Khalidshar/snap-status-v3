# استئناف: الدفعة 2 إلى v3، الأدوات المشتركة للرندر ومرونة الشبكة (21 أكتوبر 2026)

الفرع `claude/batch2-renderer-network-20261021`. طلب دمج **مسودة**؛ لا يُدمج قبل تحقق السيرفر وموافقة المالك.

## المصدر
- النواة 0.10.0 في المختبر:
  - snap-bot-v2 #51، الالتزام `35d3a07`.
  - status-bot-v2 #12، الالتزام `6ff4e7c`.
- السلوك منقول دون تغيير. أُضيفت الأنواع لـmypy strict، والصياغة وفق ruff.

## ما انتقل
- `studio_core/network/resilience.py`:
  - إعادة المحاولة: 3 محاولات، 1ث ثم 2ث مع Jitter، وسقف انتظار 10ث، للأعطال العابرة و429 و5xx فقط.
  - قاطع دائرة لكل (تطبيق، مضيف): يُفتح بعد 5 فشلات لمدة 300ث، ثم محاولة تجريبية واحدة.
  - ذاكرة TTL، معطّلة افتراضيًا.
  - يُصدَّر من `studio_core.network`.
- `studio_core/renderer/primitives.py`:
  - `run` و`ensure_glyphs` و`proof`.
  - `write_once` و`verify_once` و`cleanup_attempt_temps` و`sync_dir` و`attempt_workspace` و`file_sha256` و`publish_once`.
  - `fcntl` يبقى استيرادًا محليًا كما في المختبر.
- التخطيطات الخاصة بكل تطبيق لم تنتقل؛ مكانها `apps/` لاحقًا.

## الفروق الوحيدة عن المختبر
- `2 ** retry` صار `2.0 ** retry`: القيمة نفسها، والنوع float لـmypy.
- `shared_breaker` بلا `**policy`: لم يُستدعَ بها في أي مكان.
- `except (FileNotFoundError, OSError)` صار `except OSError`: متكافئان، لأن الأولى فرع من الثانية.

## التحقق (محليًا، Python 3.11 و3.12)
- `ruff check` و`ruff format --check` و`mypy --strict`: نظيفة.
- الاختبارات: 157 ناجحة (33 منها جديدة)، والتغطية 86.35% (الحد 80%).
- `scripts/check_core_mutations.py`: 40/40 (14 من الدفعة 1، و26 جديدة: 14 للشبكة و12 للرندر).

## بوابات التخرّج (`docs/GRADUATION.md`)
1. ✅ الاختبارات والتخريب المتعمد.
2. ⏳ السيرفر: متوقف بقرار المالك حتى النشر. يُفحص حينها عبر المتصفح المدمج في Codex ← EC2 Instance Connect.
   - ملاحظة: منفذ 22 مقيّد بعنوان IP واحد، وInstance Connect يُرفض حاليًا.
   - تفاصيل الوصول في `snap-bot-v2/docs/RESUME-SERVER-VERIFY-20261021.md` على الفرع `claude/determined-bardeen-6qsg0w`.
3. ✅ معيار المرحلة: الوحدات موحّدة في النواة ومستخدمة من التطبيقين في المختبر.
4. ⏳ موافقة المالك.
