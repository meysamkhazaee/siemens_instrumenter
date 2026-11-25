# C / C++ Basic-Block Instrumenter & Siemens / SIR Benchmark Suite Runner

[![Version](https://img.shields.io/badge/version-1.2.0-blue.svg)](file:///D:/personal/instrumenter/VERSION)
[![Changelog](https://img.shields.io/badge/changelog-available-green.svg)](file:///D:/personal/instrumenter/CHANGELOG.md)

این ریپازیتوری شامل ابزار اینسترومنت کردن کدهای C/C++ بر پایه نقاط پایه‌ای کنترل جریان (Basic Blocks) و ابزارهای اجرای دسته‌ای، آزمون و استخراج دیتاست‌های ردگیری مسیر (Execution Traces) برای بنچ‌مارک‌های **Siemens** (`printtokens`, `printtokens2`, `replace`, `schedule`, `schedule2`, `tcas`, `totinfo`) و برنامه‌های بزرگ‌تر **SIR** (`space`, `gzip`, `grep`, `sed`, `flex` — شامل پروژه‌های چندفایله) است.

---

## ۱. ساختار دایرکتوری بهینه‌شده در `output/siemens`

پس از اجرای اینسترومنت دسته‌ای، هر برنامه در `output/siemens/<repo>/` بدون کپی‌برداری دوگانه و بدون کپی کردن `inputs/` و `testplans.alt/` سازمان‌دهی می‌شود:

- **`orig/`**: نسخه سالم و بدون خطا (Error-Free) اینسترومنت‌شده به همراه فایل باینری کامپایل‌شده (`<stem>.exe`) و فایل‌های هدر (`*.h`).
- **`v1..vN/`**: نسخه‌های خطادار (Buggy Versions) اینسترومنت‌شده به همراه فایل‌های باینری کامپایل‌شده (`<stem>.exe`).
- فایل‌های `universe` و `inputs/` مستقیماً از `input/siemens/<repo>/` خوانده می‌شوند.

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

# اجرای تمام برنامه‌های Siemens و SIR به‌صورت موازی با ۸ پردازه:
python run_test_scenarios.py --repo all --version all -j 8

# یا اجرای یکی از برنامه‌های SIR (مثل space، gzip، grep، sed، flex):
python batch_instrument.py --repos space gzip grep sed flex
python run_test_scenarios.py --repo grep --version all -j 8
```

---

## ۳. ساختار خروجی استاندارد و کم‌حجم (`efl-dataset-v2`)

برای حذف تکرار ۴ برابری و کاهش چشمگیر حجم، فایل‌های قدیمی (`_dataset.txt`، `_tagged.txt`، `_detailed.json` و `cmp.v0.*.csv`) دیگر تولید نمی‌شوند و هر نسخه مستقیماً در مسیر `output/results/<repo>/<version>/full/` ذخیره می‌شود (جزئیات کامل در `../DATASET_FORMAT_README.md` و `output/results/DATASET_README.md`):

- **`traces.tsv.gz`**: فایل TSV فشرده‌شده با Gzip و دارای هدر `test_id\tstatus\tcc\tpath` که در آن `path` دنباله شناسه‌های عددی گره‌ها (بدون پیشوند `P` و جداشده با فاصله) است.
- **`tests.tsv`**: نگاشت کامل `T1..TN` به دستور دقیق هر تست در `testplans.alt/universe` به همراه وضعیت، `cc`، طول مسیر و زمان اجرا.
- **`source_map.csv`**: نگاشت هر نقطه (`id` / `P<id>`) به فایل، خط، ستون، تابع و نوع گره AST (`kind`).
- **`source_changes.json`**: خطوط تغییریافته و متن تغییر بین نسخه سالم و خطادار.
- **`summary.json`** و **`metadata.json`**: خلاصه آماری نسخه، مدل اینسترومنتیشن و `fault_predicate_ids`.
- **`cache_manifest.json`**: اندازه و کدهای SHA-256 فایل‌ها.

```bash
# تبدیل دیتاست‌های قدیمی (در صورت وجود) به فرمت جدید:
python sync_efl_datasets.py
```

---

## ۵. نسخه و تاریخچه تغییرات (Versioning & Changelog)

شماره نسخه این ابزار به‌صورت متمرکز در فایل [VERSION](file:///D:/personal/instrumenter/VERSION) نگهداری می‌شود و تمامی ابزارها (`instrumenter.py`, `batch_instrument.py`, `run_test_scenarios.py`) و بیلد باینری PyInstaller مستقیماً از آن رفرنس می‌گیرند.

- **نسخه فعلی:** `1.2.0`
- **تاریخچه کامل تغییرات:** [CHANGELOG.md](file:///D:/personal/instrumenter/CHANGELOG.md)

