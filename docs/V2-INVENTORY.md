# جرد V2 مقابل v3 (2026-11-01)

مكان عمل V2: المستودعان الخاصّان `Khalidshar/snap-bot-v2` (152 commit، آخر دمج 2026-10-21) و`Khalidshar/status-bot-v2` (52 commit، آخر دمج 2026-10-21). لم يضِع شيء منهما؛ كلاهما محفوظ على GitHub.
`/opt/studio-v2` على السيرفر ليس V2 نفسه، بل نشرات مختبر صغيرة (1–3 أكتوبر): شريحة الطقس، مرشّح Status، واختبارات عقود الوكلاء.

## ما انتقل إلى v3

| | وحدات V2 (بدون سكربتات الفحص) | موجودة في v3 باسمها |
|---|---|---|
| Snap | 69 | 48 |
| Status | 21 | 19 |

الوحدات المنقولة أُعيد ترتيبها ووُسّعت (مثلاً `render.py` من 156 سطراً إلى 615). وحوالي ربع أسطر V2 موجود في v3 كما هو حرفياً، والباقي أُعيدت كتابته.

## وحدات Snap V2 غير الموجودة في v3

- **استُبدلت:** `install_*` و`run_test_delivery` (حلّ محلّها `release.py`)، و`jobs`/`storage`/`delivery`/`renderer`/`network` (انتقلت أدوارها إلى `studio-core`).
- **تستحق المراجعة للنقل:** `runtime_watchdog` (المراقبة الذاتية)، و`observability`، و`test_live_fallback` (بديل الطقس عند انقطاع المصدر)، و`main_policy`.
- **أدوات داخلية:** `project_inventory` و`mutation_evidence` و`agent_contract_snapshot` و`adoption_pending` و`render_did_you_know_preview`.

Status: الناقص فقط `check.py` و`project_inventory.py`.

## فروع V2 التي لم تُدمج حتى في V2

Snap: 23 فرعاً. أهمها:

| الفرع | آخر تعديل | الموضوع | في v3؟ |
|---|---|---|---|
| licensed-font-profile | 10-04 | خلفية زمردية مخزّنة وضبط مدة الفيديو (+10) | جزئياً (اختبارات الخطوط) |
| fact-motion | 10-04 | تنعيم الخلفية وحفظ تباين النص | جزئياً في `render.py` |
| fact-ambient-audio | 10-05 | رفع طبقة الصوت وحدّ أدنى للصوت على الجوال | جزئياً في `fact_audio.py` |
| artifact-retention | 10-12 | حدّ احتفاظ المعاينات بثلاثة أيام | لا |
| server-runtime | 10-12 | انتظار خانة الختم قبل إعادة المحاولة | لا |
| send-reconciliation | 10-05 | تدقيق إرسال Telegram | جزئياً |
| agents-phase1 | 10-13 | ترتيب مهام المدير | لا |
| main-protection-plan | 10-04 | حماية فرع main | توثيق فقط |

Status: 3 فروع. أهمها `status-safe-send-diagnostics` (حفظ دليل الاستلام ACK)، وهو جزئياً في v3.

## التوصية

1. مراجعة الفروع غير المدموجة أعلاه، ونقل ما يفيد منها مباشرة من كودها بدل إعادة كتابته.
2. نقل `runtime_watchdog` وبديل الطقس `test_live_fallback` من V2.
3. حفظ كل عمل على GitHub فور انتهائه؛ لا إصلاحات على السيرفر أو الجهاز فقط.
