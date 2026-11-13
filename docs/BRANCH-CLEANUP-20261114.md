# جرد تنظيف الفروع — ١٤ نوفمبر ٢٠٢٦

يعتمد على طلب المحطات #142 وبعده في ترتيب التنفيذ. هذا جرد قبل التنفيذ؛ نتيجة الحذف الفعلية تُسجّل في المسألة #103 بعد اكتمال الفحوص والدمج. لا يُحذف فرع أثناء اعتماد طلب مفتوح عليه.

فُحصت كل الفروع الظاهرة (٤٤ فرعًا، صفحة واحدة دون اقتطاع) مقابل main على `1e773a250b60766ddc8d6d750a601044ee0dfa16`. شرط الحذف: رأس الفرع نفسه هو merge_base بينه وبين main (أي داخل تاريخ main)، مع عدم وجود عمل مفتوح عليه واستبعاد main والأسماء المحمية. يعاد الفحص بالقراءة فقط قبل التنفيذ؛ أي رأس متغير يبقى حتى تدقيق جديد. يحتفظ هذا الجرد بأسماء الفروع ورؤوسها للاستعادة المعتادة بإنشاء فرع على الإيداع المحفوظ. لا تُحذف إيداعات أو سجلات أو أدلة.

## الفروع المحمية صراحة

- `shehana/scheduler`
- `shehana/quran-extra`
- `shehana/tafsir-recitation`
- `shehana/stop-v3`
- `codex/spotlight-ai-disclosure-proposal-20261109`
- `codex/hadith-stored-voice-20261111`
- `codex/hadith-daily-calendar-20261111`

## فروع مدموجة مرشحة للحذف

| الفرع | الرأس الموجود داخل main |
|---|---|
| `codex/aawsat-fashion-section-20261111` | `5104fa1f2e3c01ab97ae3f275cb5668601da405d` |
| `codex/approved-source-recovery-20261111` | `926ed9a5d9cb14962745c2a20602a48a102176d5` |
| `codex/ci-main-mutations-20261111` | `f0a0d1da0d6772bed9b483e6f458d9cefa9174ad` |
| `codex/clip-publication-routing-20261111` | `ac588236f4ca8d92e09a07a10cd98ecc4bdf1720` |
| `codex/configured-state-root-20261113` | `2e260e6233782be3cebb8a9c912e92e523b90b39` |
| `codex/design-failure-diagnostics-20261111` | `1e1c04c573f31380743056fcd62df8f872c5de7e` |
| `codex/football-editorial-focus-20261111` | `47dfa06e339bc69e855a2085b0b2492b3e7255a0` |
| `codex/football-journalist-focus-20261112` | `4a7e357d91c01aa2a3c7f91e14e2557b9021251a` |
| `codex/hadith-source-diacritics-20261111` | `c80c5615a3325bfbc80f06f507f13a4c75577eef` |
| `codex/news-publisher-diversity-20261111` | `ecefe9fc04d4c99088dd3d44be8be1a84a8a37a2` |
| `codex/no-fresh-content-20261111` | `3ccc8a1b9f88a81f4a5a9440bab5996d1959787c` |
| `codex/repository-layout-20261111` | `830de4421cd5fab1c7fe801c6dda0786a96f2c3e` |
| `codex/split-production-prepare-20261111` | `c089cc1f8c49ff3c3f20ac32644f4b3ec3f1aadc` |
| `codex/switch-safety-20261112` | `97c478ed7e1051bf7debe54f6758242d15325f65` |
| `codex/world-no-sports-20261112` | `0ec7336a2c430402385472ef0c9908337fe95f40` |
| `shehana/bbc-article-body` | `91f9e4b5a5269bf6cba3ba5eb168a5b163628f7b` |
| `shehana/codex-takeover-2` | `60a24b804af6cdd790ac22627a8a4834ec5b708d` |
| `shehana/date-only` | `4a7bb82ba014bace646ab2a07ba23b587c84be10` |
| `shehana/dead-code` | `762031174b40b5221be39dec7f6d805fe1dec263` |
| `shehana/desk-badge` | `9b69d18133277a3d2b55383ab53c66329a605739` |
| `shehana/fashion-plus-119` | `d0b8b5da2ca391091e09c014915a1705b877a7ad` |
| `shehana/fashion-section` | `a89b16615244f6296f875a1b83c2ba5c547ce000` |
| `shehana/football-filter` | `860a10f8b00d4d75b34292cdf35fb2adc9175afc` |
| `shehana/markets-filter` | `875156bc022cb8412dfbb25767250fa4708dec54` |
| `shehana/markets-headline-2` | `41409eb08d9c8b042de843dcb60a07acec7b586c` |
| `shehana/sources-2` | `ff0244c6119b36161fec2e26184afa796e250b84` |
| `shehana/translated-note` | `6ce3d3e123b82376c7f159fb7a734247683446ad` |

عدد المرشحين في هذه اللقطة: 27. فروع هذه الدفعة (`codex/dead-code-cleanup-20261114` و`codex/seven-agent-stations-20261114` وفرع هذا الجرد) لا تُحذف قبل دمجها وتغيير قاعدة كل طلب يعتمد عليها، ثم تخضع لشرط الاحتواء نفسه.

## فروع غير مدموجة تبقى

- `claude/codex-brief-20261028` — لا يثبت احتواء رأسه في main.
- `codex/lenses` — لا يثبت احتواء رأسه في main.
- `codex/private-publication-notice-20261113` — لا يثبت احتواء رأسه في main.
- `codex/switch-runbook-20261112` — لا يثبت احتواء رأسه في main.
- `review/all-open` — لا يثبت احتواء رأسه في main.
- `shehana/codex-takeover` — لا يثبت احتواء رأسه في main.
- `shehana/lenses-base` — لا يثبت احتواء رأسه في main.
- `shehana/markets-headline` — لا يثبت احتواء رأسه في main.

## كيف يُتحقق منه على الجهاز

المهمة على GitHub فقط؛ لا نشر ولا خدمات ولا نص ديني ولا جدولة. تراجع شيهانة المسألة #103 وقائمة الفروع: الاستثناءات السبعة موجودة، والفروع غير المدموجة باقية، والطلبان #128 و#129 موقوفان. تقارن كل اسم محذوف ورأسه بهذا الجرد، ويمكن استعادة الفرع على رأسه المحفوظ دون استرجاع أي بيانات تشغيل.
