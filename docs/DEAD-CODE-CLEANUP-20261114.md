# حذف الكود غير المستخدم — ١٤ نوفمبر ٢٠٢٦

الأساس: `071aae5bf7030afdcca8b8ae6111705d37731003`. تدقيق استيرادات Python المطلقة والنسبية، ونقاط التشغيل في ملفات shell وsystemd وJavaScript ومسارات CI. لم يظهر مستهلك تشغيل للوحدات المحذوفة خارج المجموعة المحذوفة نفسها؛ استيراداتها المتبقية كانت في اختبارات الأدوات المتقاعدة. مراجع أسماء الجداول مثل `n1_segments` ليست استيرادات وتبقى كما هي.

كان ١١ ملفًا من القائمة القديمة محذوفًا بالفعل في main؛ لم تُعَد إضافتها أو حذفها. يحذف هذا الطلب ١٩ ملف تنفيذ متبقيًا: خمس وحدات متقاعدة (`n1_coordinator` و`n1_segments` و`editorial_agents` و`oversight_agents` و`storage/backup.py`) و١٤ ملف تحويل إلى النواة. من ملفات التحويل الـ٢٢ بقيت الثمانية المستخدمة في التشغيل: `arabic_agent` و`brand_frame` و`command_cards` و`content_agents` و`did_you_know` و`footage` و`render` و`sacred_cards`. تنفيذ النواة واختباراته باقية، وتقرأ اختبارات ملفات التحويل المحذوفة التنفيذ المشترك مباشرة.

أُزيلت خمسة إعلانات ربط محلي تشير إلى أدوات محذوفة من `agent_contract_map`؛ تظهر أدوارها `NOT_BOUND` دون ادعاء تشغيل. بقي `content_repair` وربط السلسلة الحية. لا تغيير للبيانات الدينية أو سجلات التشغيل أو بيانات الحالة أو سكربتات التحويل بين الخادمين.

حُذفت 4 ملفات اختبار للأدوات المتقاعدة، و76 دالة اختبار متعلقة بها. أُزيلت 61 حالة طفرة للأدوات أو الاختبارات المحذوفة فقط؛ بقيت 197 حالة للتطبيق، وأُزيلت حالة طفرة واحدة للنواة تخص النسخ الاحتياطي المتقاعد. لا تخفيض لحد التغطية، ولا skip لإخفاء فشل.

## الملفات المحذوفة

- `apps/snap-bot/snap_bot/catalogs.py`
- `apps/snap-bot/snap_bot/civil_calendar.py`
- `apps/snap-bot/snap_bot/editorial_agents.py`
- `apps/snap-bot/snap_bot/fact_audio.py`
- `apps/snap-bot/snap_bot/fact_layout.py`
- `apps/snap-bot/snap_bot/fact_motion.py`
- `apps/snap-bot/snap_bot/faith.py`
- `apps/snap-bot/snap_bot/font_admission.py`
- `apps/snap-bot/snap_bot/n1_coordinator.py`
- `apps/snap-bot/snap_bot/n1_segments.py`
- `apps/snap-bot/snap_bot/oversight_agents.py`
- `apps/snap-bot/snap_bot/quran_font.py`
- `apps/snap-bot/snap_bot/riddle_card.py`
- `apps/snap-bot/snap_bot/sacred_kinds.py`
- `apps/snap-bot/snap_bot/sacred_text.py`
- `apps/snap-bot/snap_bot/scene_output.py`
- `apps/snap-bot/snap_bot/variety_kinds.py`
- `apps/snap-bot/snap_bot/video_backgrounds.py`
- `apps/snap-bot/tests/test_n1_control.py`
- `apps/snap-bot/tests/test_n1_partial_tail.py`
- `apps/snap-bot/tests/test_n1_segments.py`
- `apps/snap-bot/tests/test_phase4_agents.py`
- `packages/studio-core/studio_core/storage/backup.py`

## كيف يُتحقق منه على الجهاز

تشغّل شيهانة `scripts/lint.sh` و`scripts/test.sh all` و`uv run --frozen python scripts/check_app_mutations.py --jobs 4`، ثم تصنع معاينة بلا إرسال لأخبار وديني ومنوعات في البوتين. النص والمصدر والصوت والتاريخ لا تتغير، وسناب ≥٦٥ ثانية وواتساب ≤٥٨ ثانية. يُراجع ظهور الأدوار الخمسة غير المربوطة بصدق في أدوات الوكلاء. النشر والتجربة الحية عند شيهانة.
