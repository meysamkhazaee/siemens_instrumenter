# C / C++ Basic-Block Instrumenter & Siemens Benchmark Suite Runner

[![Version](https://img.shields.io/badge/version-1.2.0-blue.svg)](file:///D:/personal/instrumenter/VERSION)
[![Changelog](https://img.shields.io/badge/changelog-available-green.svg)](file:///D:/personal/instrumenter/CHANGELOG.md)

این ریپازیتوری شامل ابزار اینسترومنت کردن کدهای C/C++ بر پایه نقاط پایه‌ای کنترل جریان (Basic Blocks) و ابزارهای اجرای دسته‌ای، آزمون و استخراج دیتاست‌های ردگیری مسیر (Execution Traces) برای مجموعه بنچ‌مارک‌های زیمنس (**Siemens Benchmark Suite**) است.

---

## ۱. ساختار دایرکتوری در `output/siemens`

پس از اجرای اینسترومنت دسته‌ای، ساختار هر بنچ‌مارک در `output/siemens/<repo>/` به‌صورت زیر سازمان‌دهی می‌شود:

- **`source/`**: نسخه سالم و بدون خطا (Error-Free) اینسترومنت‌شده به همراه فایل باینری کامپایل‌شده (`<source>.exe`) و فایل‌های هدر (`*.h`).
- **`source.alt/v1..vN/`**: تمامی نسخه‌های خطادار (Buggy Versions) اینسترومنت‌شده به همراه فایل‌های باینری کامپایل‌شده (`<source>.exe`).
- **`source.alt/source.orig/`**: نسخه مبنای سالم اینسترومنت‌شده.
- **`versions.alt/versions.orig/v1..vN/`**: ساختار سازگار با ابزارهای استاندارد زیمنس.
- **`inputs/`**, **`scripts/`**, **`testplans.alt/`**: فایل‌های ورودی آزمون، اسکریپت‌ها و استخر تست (`universe`).

---

## ۲. دستورات اجرا

### الف) اینسترومنت دسته‌ای تمام برنامه‌ها:
```bash
python batch_instrument.py
```
تمام ۱۳۹ نسخه اینسترومنت شده و با کامپایلر GCC کامپایل می‌شوند. گزارش خلاصه در `output/siemens/instrumentation_summary.json` ذخیره می‌گردد.

### ب) اجرای سناریوهای تست و تولید دیتاست:
```bash
# اجرای یک نسخه مشخص (مثلاً tcas نسخه v1 روی تمام تست‌های universe):
python run_test_scenarios.py --repo tcas --version v1

# اجرای با محدودیت تعداد تست (مثلاً ۵۰ تست اول):
python run_test_scenarios.py --repo schedule --version v3 --max-tests 50

# اجرای تمام نسخه‌های یک برنامه:
python run_test_scenarios.py --repo schedule --version all

# اجرای تمام ۷ ماژول و تمام ۱۳۹ نسخه به‌صورت موازی با ۸ پردازه:
python run_test_scenarios.py --repo all --version all -j 8
```

---

## ۳. تفاوت فایل‌های `_dataset.txt` و `_tagged.txt`

به ازای هر نسخه تست‌شده (مانند `tcas` نسخه `v1`) در پوشه `output/datasets/<repo>/` دو فایل متنی دیتاست تولید می‌شود.

در هر دو فایل، **خطوط ۱ تا $N$ کاملاً یکسان هستند**: هر خط نشان‌دهنده توالی نقاط P پیموده‌شده توسط آن تست‌کیس است:
```text
P21,P23,P1,P14,P15,P4,P3,P5,P12,P12,P2,P7,P12,P8,P3,P9,P12,P2,P11,P13,P17,P20
P21,P23,P1,P14,P20
P21,P23,P1,P14,P20
...
```

تنها تفاوت این دو فایل در **خط آخر (بردار نتیجه تست‌ها)** است:

| ویژگی | فایل `<version>_dataset.txt` | فایل `<version>_tagged.txt` |
| :--- | :--- | :--- |
| **نوع برچسب‌گذاری** | دودویی (Binary: `T` و `F`) | سه‌حالته با تفکیک باگ (`T`، `CC` و `F`) |
| **معنی `F`** | مردود (خروجی تست با نسخه سالم تفاوت دارد) | مردود (خطا اجرا شده و خروجی را تغییر داده است) |
| **معنی `T`** | موفق (خروجی یکسان، بدون توجه به اجرای باگ) | موفق تمیز (خروجی یکسان و مسیر اصلاً از باگ عبور نکرده) |
| **معنی `CC`** | وجود ندارد (در قالب `T` ادغام شده است) | **تصادفاً موفق (Coincidental Correctness)**: خطا در مسیر اجرا شد، اما خروجی تصادفاً یکسان شد |
| **کاربرد اصلی** | الگوریتم‌های سنتی SBFL (Tarantula, Ochiai) و مدل‌های ML دودویی | پژوهش‌های تحلیل نویز و پالایش تست‌های تصادفاً موفق |

### مقایسه خط آخر در یک نمونه فرضی:
```text
# در فایل _dataset.txt:
T,T,T,F,T,F,F,T,T,T

# در فایل _tagged.txt (تست‌های ۲ و ۵ باگ را اجرا کردند اما پاس شدند):
T,CC,T,F,CC,F,F,T,T,T
```

علاوه بر این دو فایل، فایل `<version>_detailed.json` نیز شامل تمام جزئیات فنی، زمان‌های اجرا، وضعیت دقیق هر تست و مکان خطاست.

---

## ۴. ساختار خروجی استاندارد EFL و همگام‌سازی

برای سازگاری کامل با خط لوله ارزیابی EFL، اسکریپت `run_test_scenarios.py` به‌صورت خودکار (و یا اسکریپت `sync_efl_datasets.py` برای دیتاست‌های موجود) خروجی‌های استاندارد زیر را در مسیر `output/results/<repo>/<version>/full/` تولید و آینه‌سازی می‌کنند:

- **`cmp.v0.v<id>.<repo>.csv`**: ساختار سه‌ستونه استاندارد EFL شامل شناسه تست (`T1`)، برچسب دقیق (`T`, `CC`, `F`) و دنباله گره‌های اجرایی (`P1,P5,...`).
- **`source_map.csv`**: نگاشت هر نقطه P به نام فایل، شماره خط، ستون و تابع مربوطه در کد مبنا.
- **`source_changes.json`**: خطوط تغییریافته و مکان‌های تزریق خطا.
- **`metadata.json`**: متادیتای بنچ‌مارک (نام برنامه، نسخه، مجموعه تست).
- **`cache_manifest.json`**: هشدارهای تغییر و هشدارهای تمامیت فایل‌ها با کدهای SHA-256.

```bash
# همگام‌سازی سریع تمام دیتاست‌های زیمنس به فرمت استاندارد EFL:
python sync_efl_datasets.py
```

---

## ۵. نسخه و تاریخچه تغییرات (Versioning & Changelog)

شماره نسخه این ابزار به‌صورت متمرکز در فایل [VERSION](file:///D:/personal/instrumenter/VERSION) نگهداری می‌شود و تمامی ابزارها (`instrumenter.py`, `batch_instrument.py`, `run_test_scenarios.py`) و بیلد باینری PyInstaller مستقیماً از آن رفرنس می‌گیرند.

- **نسخه فعلی:** `1.2.0`
- **تاریخچه کامل تغییرات:** [CHANGELOG.md](file:///D:/personal/instrumenter/CHANGELOG.md)

