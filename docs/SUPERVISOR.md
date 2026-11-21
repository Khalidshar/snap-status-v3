# مشرف روّق — التشغيل

قرار خالد ٢١ نوفمبر ٢٠٢٦. الكود: `scripts/ops/supervisor.py`. التفاصيل في `DECISIONS.md`.

## تجربة بلا إرسال ولا إعادة تشغيل
```bash
cd /opt/madar/current
.venv/bin/python scripts/ops/supervisor.py run --home /root/svdemo --dry-run --print --since-hours 24 --report
```

## التفعيل (بموافقة خالد فقط)
```bash
cp /opt/madar/current/config/systemd/madar-v3-supervisor* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now madar-v3-supervisor.timer madar-v3-supervisor-watchdog.timer
```
أول فحص يبدأ من آخر ساعة فقط، فلا يرسل تاريخًا قديمًا.

## الإيقاف
```bash
systemctl disable --now madar-v3-supervisor.timer madar-v3-supervisor-watchdog.timer
```
السجل وطلبات الإصلاح تبقى في `/var/lib/madar/supervisor/` بلا حذف.
