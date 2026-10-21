# جدول التخرّج: من المختبر إلى الإنتاج

المرجع: [`ARCHITECTURE_V3_AGREED.md`](ARCHITECTURE_V3_AGREED.md)، القسمان §2 (الشجرة) و§18 (الترحيل وحدة وحدة).

## القاعدة
- البناء والتجارب في المختبر: `snap-bot-v2` و`status-bot-v2`.
- هذا المستودع نسخة التشغيل الفعلية.
- لا يُنسخ المختبر كاملًا. تنتقل الوحدة الناجحة وحدها إلى مكانها في الشجرة.
- الكود المكرر بين التطبيقين يدخل النواة مرة واحدة.

## شروط التخرّج، ويجب أن تجتمع كلها
1. تنجح اختبارات الوحدة واختبارات التخريب المتعمد الخاصة بها.
2. تنجح على **سيرفر المالك** (Codex) بمستخدم غير root، مع systemd.
3. تحقق معيار الخروج لمرحلتها في المخطط (§19).
4. تُدمج في المختبر، ثم يوافق **المالك صراحةً** على نقلها.

## الحالات
- **مختبر**: قيد البناء.
- **مرشّح**: نجح محليًا وفي CI، وينتظر السيرفر أو الموافقة.
- **جاهز**: اجتمعت الشروط الأربعة.
- **منقول**: صار هنا.
- **لا يُنقل**: تجربة ليس لها مكان في الإنتاج.

## النواة `packages/studio-core/` (شجرة المخطط §2)

| وحدة المخطط | مصدرها في المختبر | القرار (§18) | الحالة | الدفعة |
|---|---|---|---|---|
| `jobs/` | `studio_core/jobs.py`، `gateway.py`، `component_lock.py`، `config.py` | REFACTOR | منقول (مسودة) | 1 |
| `storage/` | `studio_core/storage.py`، `backup.py` | REFACTOR | منقول (مسودة) | 1 |
| `observability/` | `studio_core/observability.py`، `runtime_watchdog.py`، `agent_runs.py`، `agent_inventory.py` | REFACTOR | منقول (مسودة) | 1 |
| `network/` | `studio_core/network.py`، `egress.py`، `resilience.py` | REFACTOR + EXTEND | منقول. أُضيف `resilience.py` (Retry/Backoff/Circuit Breaker/Cache، §8) في الدفعة 2 (مسودة) | 1 ثم 2 |
| `delivery/` | `snap_app/telegram_egress.py`، `send_evidence.py`، `send_reconciliation.py`، و`status_app/telegram_egress.py` | REFACTOR (توحيد النسختين) | مختبر | 3 |
| `renderer/` | الأدوات المشتركة: `studio_core/renderer.py` في المختبر 0.10.0 (`run` و`ensure_glyphs` و`proof` والكتابة الذرية للمحاولات). تخطيطات كل تطبيق (`fact_layout` و`fact_motion` و`fact_audio` والطقس) تبقى في تطبيقها | REFACTOR (توحيد النسختين) | الأدوات المشتركة: منقولة (مسودة، الدفعة 2). التخطيطات: مع التطبيقين | 2 |
| `quality/` | `snap_app/content_agents.py`، `render_agents.py`، `final_agent.py`، `owned_contract_adapter.py`، `agent_contract_map.py` | REFACTOR | مرشّح، مع شرط قرار الوكلاء أدناه | 3 |
| `repair/` | `snap_app/maintenance.py`، `adoption_pending.py` | REFACTOR (ينقصه R2 إلى R4 وDLQ/Replay، §11) | مرشّح | 3 |
| `religious/` | لا يوجد في V2. المرجع في V1: `religious_sources.py` و`quran_source.py` | WRAP (§7) | **لم يُبنَ** | 4 |
| سجل التشغيل N1 | `snap_app/n1_*.py` (12 ملفًا) | قرار مفتوح: إضافة على المخطط، يُنظر هل تدخل `observability/` | مرشّح | — |

## التطبيقان

| المكان | مصدره في المختبر | الحالة | الدفعة |
|---|---|---|---|
| `apps/snap-bot/` | `did_you_know.py`، `weather.py`، `arabic_agent.py`، `editorial_agents.py`، `oversight_agents.py`، `service.py`، `command_worker.py`، `command_transport.py`، `schedule_once.py`، والبيانات والخطوط | مختبر | 2 و3 |
| `apps/status-bot/` | `status_app/` (WhatsApp outbox، المراجعة، الوضع اليدوي) | مختبر | 3 |
| لا يُنقل | `actions_preview.py` في التطبيقين (تشغيل مؤقت على Actions) | لا يُنقل | — |

## قرارات مفتوحة قبل التخرّج
1. **الوكلاء (§5 و§21):** المخطط ينص على ربط وكلاء V1 الأصلية بـAdapters وGolden Tests. المختبر بنى بدائل محلية حتمية بنفس الأسماء الثمانية عشر. يحتاج قرار المالك: ربط الأصلية، أو اعتماد البدائل في ADR.
2. **المستودع:** المخطط يوصي بمستودع واحد (§2). هذا المستودع هو البيت، والمختبر يبقى مستودعين حتى يكتمل النقل.

## سجل النقل
| التاريخ | الدفعة | الوحدات | طلب الدمج | تحقق السيرفر | موافقة المالك |
|---|---|---|---|---|---|
| 2026-10-13 | 1 | jobs، storage، observability، network، config، cli | طلب دمج مسودة (batch1-foundation) | النواة نفسها نجحت على السيرفر ضمن 647/647 في المختبر؛ نسخة v3 تنتظر Codex | موافقة على البدء؛ الدمج ينتظر |
| 2026-10-21 | 2 | `renderer/` (الأدوات المشتركة)، `network/resilience.py` | طلب دمج مسودة (batch2-renderer-network) | المختبر: CI أخضر، و656/659 محليًا. السيرفر متوقف بقرار المالك، فيُفحص عند النشر | تنتظر |
