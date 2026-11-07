# قائمة جرد مدار v3

البند ١ من الخطة الأم (`MASTER-PLAN.md`). كل ما يحتاجه البوتان على أي سيرفر (جهاز خالد WSL أو أمازون).
الفحص الفعلي لكل بند: `sudo bash scripts/ops/madar.sh status` (للقراءة فقط، والمفاتيح بالحجم فقط).

## ١. الكود والأدوات
| البند | المكان | ملاحظة |
|---|---|---|
| الإصدار المفعّل | `/opt/madar/current` ← `/opt/madar/releases/<commit>` | يُثبَّت بـ`release.py --owner <المستخدم>` ثم `--activate-only`، والإصدارات السابقة تبقى للرجوع |
| خدمات المالك | `scripts/install_owner_commands.py --apply` | يكتب الخدمات والإعدادات الافتراضية، ولا يشغّل شيئًا |
| أداة الرسم | `/opt/madar/node-runtime` | Node 22.23.3 + Playwright 1.62.1 + Chromium headless (docs/DEPLOY-V3.md) |
| أدوات النظام | python3.12، ffmpeg، ffprobe، git، cloudflared | cloudflared لنفق واتساب فقط |

## ٢. المستخدمون والصلاحيات
- الخدمات تعمل كـ root بلا صلاحية تجاوز الملفات (CapabilityBoundingSet فارغ)، لذلك مجلدات البيانات يملكها root.
- مستخدم غير root برقم 1000 (ubuntu على أمازون، khalid على جهاز خالد) يرسم مقاطع سناب ويحضّر التلاوة، ويُحدَّد في الإصدار بـ`--owner`.
- `/var/lib/madar` و`snap` و`status`: root، 0711. `snap/production`: المستخدم غير الجذري 0700. `status/production`: root 0700. `quran-snap` و`quran-status`: المستخدم غير الجذري 2770.

## ٣. البيانات (`/var/lib/madar`)
| المجلد | فيه |
|---|---|
| `snap/` | طابور أوامر تيليجرام، المعاينات والمقاطع، سجل التكرار والقرّاء، حالة الأخبار |
| `status/` | طابور أوامر واتساب وسجلات الإرسال والأقفال (`commands/jobs.sqlite`)، المقاطع، الإنتاج |
| `quran-snap/`، `quran-status/` | تلاوات مؤسسة القرآن (تُحذف خلال ٧ أيام) |
| `render-work/` | ملفات رسم مؤقتة |
البيانات تنتقل مع السيرفر حتى لا يتكرر المحتوى ولا يُعاد إرسال شيء.

## ٤. ملفات المفاتيح (الأسماء فقط — لا تُقرأ ولا تُطبع، ينقلها خالد)
| الملف | تستخدمه |
|---|---|
| `/etc/madar/snap-telegram-main.env` | بوت تيليجرام الرئيسي (أوامر سناب والإرسال) |
| `/etc/studio-v2/snap-telegram-test.env` | بوت التجربة (يطلبه المثبّت) |
| `/etc/madar/status-whatsapp.env` | واتساب (الأوامر والويبهوك والإرسال) |
| `/etc/madar/status-command-webhook.json` | إعداد ويبهوك أوامر واتساب |
| `/etc/madar/whatsapp-v3-tunnel.token` | نفق Cloudflare لواتساب |
| `/etc/madar/quran.env` | مؤسسة القرآن (إنتاج) |
| `/etc/madar/media.env` | خلفيات Pexels وPixabay وCoverr |
| `/etc/madar/panel.env` | لوحة القيادة (عند تثبيتها) |
| `/etc/madar/war-alerts.enabled` | مفتاح تنبيهات الحروب (غير مفعّل) |
كلها root، 600.

## ٥. الإعدادات الخاصة (قرارات المالك فوق الافتراضي)
`zz-owner-on.conf` (MADAR_LEGACY_PRODUCTION=1 الشكل المعتمد، MADAR_QURAN_AUDIO=1 التلاوة) على:
`madar-v3-snap-render@`، `madar-v3-snap-render`، `madar-v3-status-commands`، `madar-v3-status-content@`.

## ٦. الخدمات الدائمة (بالترتيب، بأسمائها الكاملة)
| الخدمة | تحتاج |
|---|---|
| `madar-v3-production-egress@snap` و`@status` | — |
| `madar-v3-quran-egress@snap` و`@status` | quran.env |
| `madar-v3-snap-news-egress` و`madar-v3-status-news-egress` | — |
| `madar-v3-snap-video` و`madar-v3-status-video` | media.env |
| `madar-v3-snap-commands` | snap-telegram-main.env |
| `madar-v3-status-command-webhook` | status-whatsapp.env + status-command-webhook.json |
| `madar-v3-status-commands` | status-whatsapp.env |
| `madar-v3-whatsapp-origin` ثم `madar-v3-whatsapp-tunnel` | tunnel token + cloudflared (تُثبَّتان من `config/systemd`) |
خدمات تُشغَّل عند الطلب فقط (يشغّلها البوتان): `snap-render@` و`snap-render` و`snap-send-main@` و`snap-send@` و`status-send@` و`status-content@` و`snap-brief@` و`snap-news-clip@` و`*-quran-prepare@` و`*-quran-render@` و`quran-retention@`.
غير مثبتة/مطفأة بقرار: لوحة القيادة، تنبيهات الحروب. **لا مؤقتات.**

## ٧. خدمات خارجية (لا تتغير بتغيّر السيرفر)
- تيليجرام: @khalid_s2ss_bot (استقبال بالسحب، يعمل من أي سيرفر — سيرفر واحد فقط في وقت واحد).
- واتساب: Meta (تطبيق 1717089069799641) ← عامل Cloudflare `madar-v3-whatsapp` ← نفق `madar-v3-whatsapp` ← السيرفر الذي يشغّل النفق.
- مؤسسة القرآن: تطبيق rawwag_khalid (إنتاج). مصادر التلاوة والتفسير والأخبار: بلا مفاتيح.

## ٨. قائمة النقل بين سيرفرين
١ الكود والأدوات ← ٢ المستخدم والصلاحيات ← ٣ البيانات (نسخة كاملة وهو موقف) ← ٤ المفاتيح (خالد) ← ٥ الإعدادات الخاصة ← ٦ الخدمات (`madar.sh start` تجربة ثم `--apply`) ← ٧ التأكد أن السيرفر الآخر موقف (`madar.sh stop --apply` هناك) ← `madar.sh status` + فحص القبول.
