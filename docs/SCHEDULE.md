# الجدولة — التشغيل

قرار خالد ٩ نوفمبر ٢٠٢٦، وتشغيلها ٢١ نوفمبر («شغّل الجدولة»). ثلاثة مقاطع يوميًا في البوتين بتوقيت الرياض،
تُصنع وتُرسل لخالد نفسه (هو ينشر): الصباح ٠٦:٣٠ أذكار الصباح (الجمعة: سورة الكهف)، الظهر ١٣:٠٠ تدوير
المنوعات والأخبار، الليل ٢١:٣٠ ليلة طيبة / أذكار المساء بالتناوب. كل مقطع يمر بنفس فحوص «أنتج» و«اعتماد» في
اللوحة. «إيقاف كامل» يوقفها في البوت المعني. واتساب يرسل فقط ضمن نافذة ٢٤ ساعة من آخر رسالة لخالد.

## التفعيل
```bash
cp /opt/madar/current/config/systemd/madar-v3-schedule* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now madar-v3-schedule-morning.timer madar-v3-schedule-noon.timer madar-v3-schedule-night.timer
```

## الإيقاف
```bash
systemctl disable --now madar-v3-schedule-morning.timer madar-v3-schedule-noon.timer madar-v3-schedule-night.timer
```
