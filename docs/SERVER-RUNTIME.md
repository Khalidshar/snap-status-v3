# تشغيل v3 على السيرفر (Snap) — 23 أكتوبر 2026

مسار يدوي مطابق لمسار المختبر (V2)، ومستقل عن GitHub Actions. التثبيت لا يشغّل أي خدمة ولا يفعّلها، ولا ينشئ مؤقتات ولا مفاتيح ولا sudoers. Status في القسم الأخير: معاينة فقط بلا تسليم.

## التصميم

| العنصر | المختبر (V2) | v3 |
| --- | --- | --- |
| الإصدار | `/opt/madar/releases/<commit>/` | نفس المجلد، بإصدار جديد لكل التزام |
| الإصدار المفعّل | الرابط الذري `/opt/madar/current` | نفس الرابط |
| الحالة والسجل | `/var/lib/madar/snap` | **نفس المجلد ونفس السجل** |
| الوحدات | `madar-snap-render.service` و`madar-snap-send@.service` | `madar-v3-snap-render.service` و`madar-v3-snap-send@.service` |
| الوحدة البرمجية | `snap_app.server_preview` | `snap_bot.server_preview` |
| الاعتماديات | Wheel للنواة مثبّت بالـhash | لا اعتماديات خارجية: venv بلا pip، وملف `.pth` يضيف `apps/snap-bot` و`packages/studio-core` من الإصدار نفسه |

- **منع إعادة الإرسال بين الإصدارين:** السجل والقفل وملفات النية مشتركة. أي SHA استُهلك في إصدار يبقى مستهلكًا في الآخر.
- **لا خلط بين الإصدارين:** المعاينة مربوطة بالتزام الإصدار المفعّل. وحدة المختبر تفشل قبل أي عمل على إصدار v3، لأن `snap_app` غير موجودة. ووحدة v3 تفشل على إصدار المختبر للسبب نفسه.
- **التثبيت لا يفعّل:** نقل `current` خطوة مستقلة (الانتقال)، والأمر نفسه يرجع إلى إصدار المختبر (الرجوع).
- **ملف بيئة Telegram:** يُمرَّر مساره للمثبّت (`--telegram-env`)، ويُكتب في وحدة الإرسال فقط. المثبّت يتحقق أنه موجود ومملوك لـroot. لا يفتحه ولا يطبعه.
- `check_env` يعمل بحساب المالك غير root قبل كتابة `madar-release.json`. بدون نجاحه لا يقبل التفعيل الإصدار.

## الفحوص قبل السيرفر

- اختبارات `apps/snap-bot/tests/test_release.py`.
- وظيفة CI «Snap release install» (`scripts/check_release_install.py`)، على Runner مؤقت فيه systemd:
  - تثبيت حقيقي داخل جذر مؤقت، مع `check_env` حقيقي بحساب غير root.
  - التأكد أن الاستيراد يأتي من الإصدار وحده.
  - رفض إعادة تثبيت نفس الإصدار، ورفض وحدة مُعدّلة.
  - الانتقال إلى v3، ثم الرجوع إلى إصدار مختبر، ثم العودة إلى v3.
  - التأكد أن وحدة المختبر تفشل على إصدار v3.

## الأوامر (بموافقة نشر مستقلة لكل خطوة)

```sh
# 1) معاينة التثبيت (لا تغيّر شيئًا)
sudo python3 apps/snap-bot/snap_bot/release.py --source <checkout نظيف> --commit <40-hex> \
    --node-bin <node 22> --telegram-env <ملف البيئة الموجود> --dry-run
# 2) التثبيت: بنفس الأمر بدون --dry-run (ينشئ الإصدار والوحدتين، ولا يفعّل)
# 3) الانتقال: معاينة ثم تنفيذ
sudo python3 apps/snap-bot/snap_bot/release.py --activate-only --commit <v3-commit> --dry-run
sudo python3 apps/snap-bot/snap_bot/release.py --activate-only --commit <v3-commit>
# 4) الرجوع إلى المختبر عند الحاجة
sudo python3 apps/snap-bot/snap_bot/release.py --activate-only --commit <lab-commit>
```

**بعد الانتقال**، الدورة اليدوية تمر بخطوات، وكل خطوة بقرار المالك:

```sh
sudo systemctl start madar-v3-snap-render.service
# شاهد الفيديو وpreview.png، وسجّل الـSHA
sudo /opt/madar/current/.venv/bin/python -m snap_bot.server_preview approve --video-sha <sha>
sudo systemctl start madar-v3-snap-send@<sha>.service
```

لا تُعِد أمر الإرسال بعد خطأ أو نتيجة غير معروفة.

## الرجوع

- الرجوع لا يحذف أي إصدار ولا أي سجل.
- لا يلمس `/opt/studio-v2` ولا البوتات القديمة.
- وحدتا المختبر تبقيان كما هما.

## Status: معاينة الطقس على السيرفر (بلا تسليم)

الوحدة `madar-v3-status-render.service` تشغّل متحكمًا واحدًا (`status_bot.server_preview render`). وكل تشغيل:
1. ينسخ ملف التعريف المثبّت إلى `/var/lib/madar/status/runs/<id>/`، بحالة وSocket خاصين بالتشغيل.
2. يشغّل وحدة egress مؤقتة. هي العملية الوحيدة التي تفتح IP، وتقتصر على `api.open-meteo.com` و`api.met.no` (مجانيان وبلا مفتاح). وبلا صلاحيات.
3. يجلب الطقس الحقيقي، ويمرّ بوكلاء المصدر والعربية.
4. يرندر في وحدة مؤقتة بلا شبكة، وsocket الـegress محجوب عنها. والخطوط والأدوات مثبّتة بالـhash والمسار.
5. يسجل التحقق، وينسخ `weather.mp4` و`preview.png` وغيرهما إلى `/var/lib/madar/status/previews/<id>/`، ثم يضيف حدث `RENDERED` إلى سجل مستقل عن Snap.
6. يوقف الوحدتين المؤقتتين دائمًا، حتى عند الفشل. لا يحذف أي تشغيل أو سجل.

- لا يوجد مسار WhatsApp أو Telegram في هذه الوحدة. التسليم قرار لاحق من المالك، ويحتاج حساب WhatsApp Cloud API ورابط Webhook عامًا.
- `--state` بديل للاختبار فقط، تحت `/var/lib`. والوحدة تستخدم المسار الثابت دائمًا.
- يثبت ذلك في CI: وظيفة «Release install» تشغّل المتحكم المثبّت فعلًا، بطقس حقيقي ورندر بلا شبكة.

```sh
sudo systemctl start madar-v3-status-render.service
sudo journalctl -u madar-v3-status-render.service -n 5 --no-pager
```
