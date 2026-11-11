# استئناف: نقل اختبارات التخريب الخاصة بتطبيق Snap (2026-10-22)

## ما الذي انتقل
- `scripts/snap_app_mutations.json` يحوي **261 حالة** مأخوذة من أدوات المختبر `tools/check_*_mutations.py` في snap-bot-v2. سُجّلت الحالات من الأدوات نفسها وأعيدت صياغتها على مصادر v3 بعد التنسيق، ولم تضع أي حالة.
- `scripts/check_app_mutations.py` يأخذ كل حالة وينسخ `snap_bot` إلى مجلد مؤقت بنفس عمق المستودع، مع روابط لـ`assets` و`config`. ثم يحذف الحارس ويشغّل اختباره المسمّى. لا تنجح الحالة إلا إذا فشل ذلك الاختبار (`FAILED` أو `ERROR` أو `SUBFAILED`) وظهر وسم الحارس عندما يكون له وسم.
- في CI أُضيفت مهمة مستقلة اسمها `App guard mutations` تعمل بالتوازي مع الاختبارات، وتشترطها `CI passed`.

## النتيجة
- **261/261**: 255 حالة تحت root محلياً، والـ6 حالات `n1_journal` تحت مستخدم عادي. اختبارات `n1_journal` تتخطى نفسها تحت root، أما مشغّل CI فليس root.
- الحالات التي لم تُنقل: maintenance وdelivery_core وnetwork_resilience وmain_policy. حراسها صارت في النواة ويغطيها `check_core_mutations.py`.

## التشغيل
```
uv run --frozen python scripts/check_app_mutations.py --jobs 4 [--only TOOL]
```

## الباقي (دين موثّق)
- تطبيق Status ليست له أدوات تخريب في المختبر.
- ~~تنميط وحدات التطبيقين~~ سُدِّد (`RESUME-APP-TYPING-20261023.md`).
- إزالة استثناءات ruff.
